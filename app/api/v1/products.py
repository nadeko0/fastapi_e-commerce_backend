import inspect
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.dependencies import Cache, DbSession
from app.core.etag import IfNoneMatch, etag_for, if_none_match_hits
from app.core.openapi import DocumentedRoute
from app.core.sorting import order_clauses
from app.core.text_input import first_unusable_text
from app.models.category import Category
from app.models.product import Product
from app.schemas.category import (
    CategoryListResponse,
    CategoryResponse,
    CategoryTreeNode,
    CategoryTreeResponse,
)
from app.schemas.common import Int32, sortable_pagination
from app.schemas.product import (
    ProductFilter,
    ProductListResponse,
    ProductResponse,
    ProductSearch,
)

router = APIRouter(tags=["catalog"], route_class=DocumentedRoute)

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


def _characteristics_filter(raw: str | None) -> dict | None:
    """The `characteristics` query parameter: a JSON object, {"color": "red"}.
    Anything else is a 422 like any other bad query parameter."""
    if raw is None:
        return None
    problem = None
    try:
        parsed = json.loads(raw)
    except ValueError:
        problem = ("json_invalid", "characteristics must be a JSON object, e.g. {\"color\": \"red\"}")
    else:
        if not isinstance(parsed, dict):
            problem = ("dict_type", "characteristics must be a JSON object, e.g. {\"color\": \"red\"}")
    if problem:
        raise RequestValidationError([
            {"type": problem[0], "loc": ("query", "characteristics"), "msg": problem[1]}
        ])
    # The JSON can spell a NUL as the escape \u0000, which the query string
    # check (app/core/text_input.py) does not see.
    unusable = first_unusable_text(parsed, ("query", "characteristics"))
    if unusable:
        raise RequestValidationError([unusable])
    return parsed


def _product_filter(**params) -> ProductFilter:
    """Depends(ProductFilter), except that a check across parameters
    (ProductFilter.validate_price_range) is answered 422 like any other bad
    query parameter. FastAPI validates each parameter on its own and then
    calls the class with the results; a ValidationError raised by that call
    is not request validation to it, and came out as a 500."""
    params["characteristics"] = _characteristics_filter(params.get("characteristics"))
    try:
        return ProductFilter(**params)
    except ValidationError as error:
        raise RequestValidationError([
            {**detail, "loc": ("query", *detail["loc"])}
            for detail in error.errors(include_url=False, include_context=False)
        ]) from None


# The parameters FastAPI reads from the request are the model's fields,
# except `characteristics`: a dict is a request body to FastAPI, and GET has
# no body to speak of (OpenAPI 3.1.0: "SHOULD be avoided"), so it is a query
# parameter holding JSON, parsed above.
_filter_signature = inspect.signature(ProductFilter)
_product_filter.__signature__ = _filter_signature.replace(parameters=[
    parameter.replace(
        annotation=Annotated[str | None, Query(
            max_length=1000,
            description='Match on characteristics: a JSON object of name to value, e.g. {"color": "red"}.',
        )],
        default=None,
    ) if parameter.name == "characteristics" else parameter
    for parameter in _filter_signature.parameters.values()
])


CATEGORY_SORT_COLUMNS = {"id": Category.id, "name": Category.name, "created_at": Category.created_at}
CategoryListParams = sortable_pagination("CategoryListParams", tuple(CATEGORY_SORT_COLUMNS))


@router.get("/categories", response_model=CategoryListResponse)
async def list_categories(
    pagination: Annotated[CategoryListParams, Depends()],
    db: DbSession,
    parent_id: Int32 | None = None,
):
    """List categories, optionally filtered by parent, with pagination.

    sort_by (id, name, created_at) and sort_order (asc, desc) sort the list, ties
    broken by id in the same direction; without sort_by, or with another value,
    the order is by id.
    """

    stmt = select(Category)
    if parent_id is not None:
        stmt = stmt.where(Category.parent_id == parent_id)

    total = await _count(db, stmt)
    # The id last: LIMIT/OFFSET pages only fit together in a unique order.
    categories = (await db.scalars(
        stmt.order_by(*order_clauses(
            pagination.sort_by, pagination.sort_order, CATEGORY_SORT_COLUMNS, Category.id, [Category.id]
        )).offset((pagination.page - 1) * pagination.size).limit(pagination.size)
    )).all()

    return CategoryListResponse(
        items=[CategoryResponse.model_validate(cat) for cat in categories],
        total=total,
        page=pagination.page,
        size=pagination.size,
        has_more=total > pagination.page * pagination.size
    )

@router.get("/categories/tree", response_model=CategoryTreeResponse)
async def get_category_tree(
    db: DbSession,
    redis: Cache,
):
    """Retrieve the full category tree, nested from root categories down."""

    cached_tree = await redis.get_cached_category_tree()
    if cached_tree:
        return cached_tree


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

    return response

@router.get(
    "/categories/{category_id}",
    response_model=CategoryResponse,
    responses={404: {"description": "Category not found"}},
)
async def get_category(
    category_id: Int32,
    db: DbSession,
):
    """Retrieve a single category by ID."""

    category = (await db.scalars(select(Category).where(Category.id == category_id))).first()
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )
    return CategoryResponse.model_validate(category)

@router.get("/products", response_model=ProductListResponse)
async def list_products(
    filter_params: Annotated[ProductFilter, Depends(_product_filter)],
    db: DbSession,
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

    return ProductListResponse(
        items=[_to_product_response(p) for p in products],
        total=total,
        page=filter_params.page,
        size=filter_params.size,
        has_more=total > filter_params.page * filter_params.size
    )

# /products/search must be registered before /products/{product_id}:
# Starlette matches routes in registration order, and {product_id}: int
# would otherwise greedily match the literal path segment "search" first
# (failing int validation -> 422), making this endpoint unreachable.
@router.get("/products/search", response_model=ProductListResponse)
async def search_products(
    search: Annotated[ProductSearch, Query()],
    db: DbSession,
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

    return ProductListResponse(
        items=[_to_product_response(p) for p in products],
        total=total,
        page=search.page,
        size=search.size,
        has_more=total > search.page * search.size
    )

@router.get(
    "/products/{product_id}",
    response_model=ProductResponse,
    responses={
        304: {"description": "Not modified: If-None-Match matches the current ETag"},
        404: {"description": "Product not found"},
    },
)
async def get_product(
    product_id: Int32,
    db: DbSession,
    redis: Cache,
    response: Response,
    if_none_match: IfNoneMatch = None,
):
    """Retrieve a single product by ID.

    The ETag is a hash of the ProductResponse JSON, so the database path and
    the Redis copy of the same representation carry the same tag.
    """

    def _conditional(representation: ProductResponse):
        etag = etag_for(representation)
        if if_none_match_hits(if_none_match, etag):
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
        response.headers["ETag"] = etag
        return None

    cached_product = await redis.get_cached_product(product_id)
    if cached_product:
        try:
            cached_representation = ProductResponse.model_validate(cached_product)
        except ValidationError:
            cached_representation = None  # unreadable cache entry: rebuild below
        if cached_representation is not None:
            return _conditional(cached_representation) or cached_representation

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

    representation = _to_product_response(product)

    await redis.cache_product(product_id, representation.model_dump())

    return _conditional(representation) or representation
