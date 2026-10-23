import inspect
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.database import get_async_db
from app.models.category import Category
from app.models.product import Product
from app.schemas.category import (
    CategoryListResponse,
    CategoryResponse,
    CategoryTreeNode,
    CategoryTreeResponse,
)
from app.schemas.common import APIResponse, Int32, PaginationParams
from app.schemas.product import (
    ProductFilter,
    ProductListResponse,
    ProductResponse,
    ProductSearch,
)
from app.services.redis_async import AsyncRedisService, get_cache_service

router = APIRouter(tags=["catalog"])

# This router runs on AsyncSession (app.core.database.get_async_db): every
# query below is awaited, so the event loop keeps serving other requests
# while Postgres works. Two rules follow from that and are easy to break:
#   - No lazy loading. Anything a response needs from a relationship must be
#     loaded by the query itself (joinedload(Product.category) below);
#     touching an unloaded relationship raises MissingGreenlet under asyncio
#     instead of quietly running one more query.
#   - The cache goes through AsyncRedisService (get_cache_service), awaited
#     like the queries; a call on the synchronous RedisService would block
#     the loop.


async def _count(db: AsyncSession, stmt: Select) -> int:
    # Same SQL the legacy Query.count() produced: SELECT count(*) FROM (<stmt>).
    # order_by(None) because ordering a subquery that is only counted is
    # wasted work for Postgres.
    return await db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))


def _to_product_response(product: Product) -> ProductResponse:
    # ProductResponse requires category_name, which is not a Product
    # column - ProductResponse.model_validate(product) always raised a 500
    # ValidationError ("category_name: Field required") for every route
    # that didn't build the response this way. app/api/v1/admin.py's
    # create/update endpoints already do this; list_products, get_product,
    # and search_products below did not, and always 500'd.
    return ProductResponse(**product.__dict__, category_name=product.category.name)


def _matches_text(text: str):
    """Products whose name or description contains `text`, ignoring case.

    The visitor's text is matched as text: autoescape escapes %, _ and the
    escape character itself, and the ESCAPE clause it adds also stops
    Postgres treating a backslash as one. An f"%{text}%" pattern, as here
    before, made "%" match every product and "_" any character. The length
    is capped by the schemas (ProductFilter.search_query, ProductSearch.query).
    """
    return or_(
        Product.name.icontains(text, autoescape=True),
        Product.description.icontains(text, autoescape=True),
    )


def _product_filter(**params) -> ProductFilter:
    """Depends(ProductFilter), except that a check across parameters
    (ProductFilter.validate_price_range) is answered 422 like any other bad
    query parameter. FastAPI validates each parameter on its own and then
    calls the class with the results; a ValidationError raised by that call
    is not request validation to it, and came out as a 500."""
    try:
        return ProductFilter(**params)
    except ValidationError as error:
        raise RequestValidationError([
            {**detail, "loc": ("query", *detail["loc"])}
            for detail in error.errors(include_url=False, include_context=False)
        ])


# The parameters FastAPI reads from the request are the model's fields.
_product_filter.__signature__ = inspect.signature(ProductFilter)


@router.get("/categories", response_model=APIResponse[CategoryListResponse])
async def list_categories(
    pagination: PaginationParams = Depends(),
    parent_id: Optional[Int32] = None,
    db: AsyncSession = Depends(get_async_db, scope="function"),
):
    """List categories, optionally filtered by parent, with pagination."""

    stmt = select(Category)
    if parent_id is not None:
        stmt = stmt.where(Category.parent_id == parent_id)

    total = await _count(db, stmt)
    # The id last: LIMIT/OFFSET pages only fit together in a unique order.
    categories = (await db.scalars(
        stmt.order_by(Category.id).offset((pagination.page - 1) * pagination.size).limit(pagination.size)
    )).all()

    return APIResponse.success_response(CategoryListResponse(
        items=[CategoryResponse.model_validate(cat) for cat in categories],
        total=total,
        page=pagination.page,
        size=pagination.size,
        has_more=total > pagination.page * pagination.size
    ))

@router.get("/categories/tree", response_model=APIResponse[CategoryTreeResponse])
async def get_category_tree(
    db: AsyncSession = Depends(get_async_db, scope="function"),
    redis: AsyncRedisService = Depends(get_cache_service),
):
    """Retrieve the full category tree, nested from root categories down."""

    cached_tree = await redis.get_cached_category_tree()
    if cached_tree:
        return APIResponse.success_response(cached_tree)


    categories = (await db.scalars(select(Category))).all()

    if not categories:

        response = CategoryTreeResponse(
            tree=[],
            total_categories=0,
            max_depth=0
        )
    else:
        root_categories = [c for c in categories if c.parent_id is None]

        def build_tree(category: Category) -> CategoryTreeNode:
            children = [c for c in categories if c.parent_id == category.id]
            return CategoryTreeNode(
                **CategoryResponse.model_validate(category).model_dump(),
                children=[build_tree(child) for child in children]
            )

        tree = [build_tree(root) for root in root_categories]
        max_depth = max(c.level for c in categories)


        response_data = {
            'tree': tree,
            'total_categories': len(categories),
            'max_depth': max_depth
        }


        response = CategoryTreeResponse(**response_data)


    await redis.cache_category_tree(response)

    return APIResponse.success_response(response)

@router.get(
    "/categories/{category_id}",
    response_model=APIResponse[CategoryResponse],
    responses={404: {"description": "Category not found"}},
)
async def get_category(
    category_id: Int32,
    db: AsyncSession = Depends(get_async_db, scope="function"),
):
    """Retrieve a single category by ID."""

    category = (await db.scalars(select(Category).where(Category.id == category_id))).first()
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )
    return APIResponse.success_response(CategoryResponse.model_validate(category))

@router.get("/products", response_model=APIResponse[ProductListResponse])
async def list_products(
    filter_params: ProductFilter = Depends(_product_filter),
    db: AsyncSession = Depends(get_async_db, scope="function"),
):
    """List products with optional filtering, sorting, and pagination."""

    stmt = select(Product).options(joinedload(Product.category)).where(
        Product.is_active
    )


    if filter_params.category_id:
        stmt = stmt.where(Product.category_id == filter_params.category_id)
    if filter_params.min_price is not None:
        stmt = stmt.where(Product.price >= filter_params.min_price)
    if filter_params.max_price is not None:
        stmt = stmt.where(Product.price <= filter_params.max_price)
    if filter_params.in_stock:
        stmt = stmt.where(Product.stock_quantity > 0)
    if filter_params.characteristics:
        for key, value in filter_params.characteristics.items():
            # Stored characteristics are structured ProductCharacteristic
            # objects ({"color": {"name": "Color", "value": "red", ...}}),
            # not flat key->value pairs - must index into ["value"], not
            # compare the whole nested object's string form.
            # .as_string() is the cross-dialect equivalent of Postgres's
            # ->> operator (.astext) - .astext only exists on the
            # Postgres-specific JSON/JSONB comparator, and Product.
            # characteristics is a generic JSON column (with a Postgres
            # JSONB variant for storage, but with_variant doesn't swap the
            # Python-side indexing Comparator used to build this
            # expression) - using .astext here raised AttributeError on
            # every request that passed a characteristics filter.
            stmt = stmt.where(Product.characteristics[key]["value"].as_string() == str(value))
    if filter_params.search_query:
        stmt = stmt.where(_matches_text(filter_params.search_query))


    if filter_params.sort_by:
        # rsplit(..., 1), not split('_'): ProductFilter.sort_by's own regex
        # ("^(name|price|created_at)_(asc|desc)$") allows "created_at_desc",
        # whose field name itself contains an underscore. split('_') on that
        # yields 3 pieces ('created', 'at', 'desc'), and unpacking into
        # (field, order) raised an unhandled ValueError - a 500 for a value
        # the schema explicitly documents as valid. rsplit from the right
        # with maxsplit=1 always yields exactly (field, order) regardless of
        # how many underscores the field name contains. Mirrors
        # app/api/v1/admin.py's list_orders, which already uses rsplit for
        # the same reason.
        field, order = filter_params.sort_by.rsplit('_', 1)
        column = getattr(Product, field)
        # The id breaks ties in the direction of the sort, so that rows
        # sharing a price or a name keep one place from page to page.
        if order == 'desc':
            stmt = stmt.order_by(column.desc(), Product.id.desc())
        else:
            stmt = stmt.order_by(column.asc(), Product.id.asc())
    else:
        stmt = stmt.order_by(Product.id)

    total = await _count(db, stmt)
    products = (await db.scalars(
        stmt.offset((filter_params.page - 1) * filter_params.size).limit(filter_params.size)
    )).all()

    return APIResponse.success_response(ProductListResponse(
        items=[_to_product_response(p) for p in products],
        total=total,
        page=filter_params.page,
        size=filter_params.size,
        has_more=total > filter_params.page * filter_params.size
    ))

# /products/search must be registered before /products/{product_id}:
# Starlette matches routes in registration order, and {product_id}: int
# would otherwise greedily match the literal path segment "search" first
# (failing int validation -> 422), making this endpoint unreachable.
@router.get("/products/search", response_model=APIResponse[ProductListResponse])
async def search_products(
    search: ProductSearch = Depends(),
    db: AsyncSession = Depends(get_async_db, scope="function"),
):
    """Search products by name/description, optionally scoped to a category."""

    stmt = select(Product).options(joinedload(Product.category)).where(
        Product.is_active,
        _matches_text(search.query),
    )

    if search.category_id:
        stmt = stmt.where(Product.category_id == search.category_id)

    total = await _count(db, stmt)
    products = (await db.scalars(
        stmt.order_by(Product.id).offset((search.page - 1) * search.size).limit(search.size)
    )).all()

    return APIResponse.success_response(ProductListResponse(
        items=[_to_product_response(p) for p in products],
        total=total,
        page=search.page,
        size=search.size,
        has_more=total > search.page * search.size
    ))

@router.get(
    "/products/{product_id}",
    response_model=APIResponse[ProductResponse],
    responses={404: {"description": "Product not found"}},
)
async def get_product(
    product_id: Int32,
    db: AsyncSession = Depends(get_async_db, scope="function"),
    redis: AsyncRedisService = Depends(get_cache_service),
):
    """Retrieve a single product by ID."""

    cached_product = await redis.get_cached_product(product_id)
    if cached_product:
        return APIResponse.success_response(cached_product)

    product = (await db.scalars(
        select(Product).options(joinedload(Product.category)).where(
            Product.id == product_id,
            Product.is_active,
        )
    )).first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )

    response = _to_product_response(product)


    await redis.cache_product(product_id, response.model_dump())

    return APIResponse.success_response(response)
