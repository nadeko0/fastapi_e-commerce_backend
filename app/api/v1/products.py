import inspect
import json
from collections.abc import Sequence
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import Select, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.dependencies import Cache, DbSession
from app.core.etag import IfNoneMatch, etag_for, if_none_match_hits
from app.core.openapi import ETAG_HEADER, ETAG_RESPONSE, DocumentedRoute
from app.core.sorting import order_clauses
from app.core.text_input import first_unusable_text
from app.models.category import Category
from app.models.product import Product, ProductVariant
from app.schemas.category import (
    CategoryListResponse,
    CategoryResponse,
    CategoryTreeNode,
    CategoryTreeResponse,
)
from app.schemas.common import Int32, PaginationParams, cart_unit_price, sortable_pagination
from app.schemas.product import (
    ProductDetailResponse,
    ProductFilter,
    ProductListItem,
    ProductListResponse,
    ProductResponse,
    ProductSearch,
    ProductVariantPublic,
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


async def _count(db: AsyncSession, stmt: Select[Any]) -> int:
    # Same SQL the legacy Query.count() produced: SELECT count(*) FROM (<stmt>).
    # order_by(None) because ordering a subquery that is only counted is
    # wasted work for Postgres.
    total = await db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    return total or 0


def _to_product_response(product: Product) -> ProductResponse:
    # ProductResponse requires category_name, which is not a Product
    # column - ProductResponse.model_validate(product) always raised a 500
    # ValidationError ("category_name: Field required") for every route
    # that didn't build the response this way. app/api/v1/admin/products.py's
    # create/update endpoints already do this; list_products, get_product,
    # and search_products below did not, and always 500'd.
    return ProductResponse(**product.__dict__, category_name=product.category.name)


async def load_product_detail(
    db: AsyncSession, base: ProductResponse, *, known_without_variants: bool = False
) -> ProductDetailResponse:
    """The product page representation: `base` plus its active variants.

    One query for all the variants, read on every request and not kept in the
    Redis copy: an order for a variant takes that variant's stock without
    dropping the product's entry, so a cached variant list would show stale
    stock.

    known_without_variants skips that query. Only the Redis entry of a product
    that had no active variant when it was cached may say so (get_product):
    a variant is created or activated only through the admin variant
    endpoints, and both drop that entry."""
    variants = [] if known_without_variants else (await db.scalars(
        select(ProductVariant)
        .where(ProductVariant.product_id == base.id, ProductVariant.is_active)
        .order_by(ProductVariant.id)
    )).all()
    return ProductDetailResponse(
        **base.model_dump(),
        has_variants=bool(variants),
        variants=[
            ProductVariantPublic(
                id=variant.id,
                sku=variant.sku,
                attributes=variant.attributes,
                price=cart_unit_price(base, variant),
                currency=base.currency,
                stock_quantity=variant.stock_quantity,
            )
            for variant in variants
        ],
    )


def product_etag(base: ProductResponse, detail: ProductDetailResponse) -> str:
    """The product's ETag, one computation for GET /products/{id} and the admin
    PUT/PATCH If-Match check and response. A product with variants is hashed
    with them (variant stock moves through orders without touching the product
    row); a product without is hashed as the ProductResponse it always was."""
    return etag_for(detail if detail.has_variants else base)


async def _list_items(db: AsyncSession, products: Sequence[Product]) -> list[ProductListItem]:
    """The page of products as list items. has_variants for the whole page comes
    from one query (the ids that have an active variant), not one per product."""
    if not products:
        return []
    with_variants = set((await db.scalars(
        select(ProductVariant.product_id)
        .where(ProductVariant.product_id.in_([p.id for p in products]), ProductVariant.is_active)
        .distinct()
    )).all())
    return [
        ProductListItem(**_to_product_response(p).model_dump(), has_variants=p.id in with_variants)
        for p in products
    ]


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


def _characteristics_filter(raw: str | None) -> dict[str, Any] | None:
    """The `characteristics` query parameter: a JSON object, {"color": "red"}.
    Anything else is a 422 like any other bad query parameter."""
    if raw is None:
        return None
    problem = None
    parsed: Any = None
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
    characteristics: dict[str, Any] = parsed
    return characteristics


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
_product_filter.__signature__ = _filter_signature.replace(parameters=[  # type: ignore[attr-defined]  # FastAPI inspects __signature__ for query parameters
    parameter.replace(
        annotation=Annotated[str | None, Query(
            max_length=1000,
            description='Match on characteristics: a JSON object of name to value, e.g. {"color": "red"}.',
            # JSON Schema 2020-12 (OpenAPI 3.1) 8.3-8.5: the string holds a
            # JSON text, and that text is an object.
            json_schema_extra={"contentMediaType": "application/json", "contentSchema": {"type": "object"}},
        )],
        default=None,
    ) if parameter.name == "characteristics" else (
        # The signature drops a field's description: put it back as the
        # query parameter's.
        parameter.replace(annotation=Annotated[
            parameter.annotation, Query(description=ProductFilter.model_fields[parameter.name].description)
        ]) if ProductFilter.model_fields[parameter.name].description else parameter
    )
    for parameter in _filter_signature.parameters.values()
])


CATEGORY_SORT_COLUMNS = {"id": Category.id, "name": Category.name, "created_at": Category.created_at}
if TYPE_CHECKING:
    CategoryListParams = PaginationParams
else:
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
    """List active products with optional filtering, sorting, and pagination.

    `page` starts at 1; `size` is 1 to 100 (20 by default). `sort_by` is one of
    name_asc, name_desc, price_asc, price_desc, created_at_asc, created_at_desc
    (any other value is 422; without it, by id). `max_price` below `min_price`
    is a 422. A product with has_variants true is sold by its variants: its own
    price and stock are not the purchasable values (see GET /products/{id})."""

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
        # Same rule as app/services/orders.py: a product with an active
        # variant is bought through its variants, so their stock decides and
        # the parent's own stock is ignored.
        active_variants = ProductVariant.product_id == Product.id, ProductVariant.is_active
        stmt = stmt.where(or_(
            ~exists().where(*active_variants) & (Product.stock_quantity > 0),
            exists().where(*active_variants, ProductVariant.stock_quantity > 0),
        ))
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
            #
            # .as_string() renders a JSON boolean as 'true'/'false' on
            # Postgres but as '1'/'0' on SQLite (json_extract yields an
            # integer), and str(True) is 'True' on neither. Numbers and
            # strings come out alike on both.
            if isinstance(value, bool):
                dialect = db.get_bind().dialect.name
                wanted = ("1" if value else "0") if dialect == "sqlite" else ("true" if value else "false")
            else:
                wanted = str(value)
            stmt = stmt.where(Product.characteristics[key]["value"].as_string() == wanted)
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
        # app/api/v1/admin/orders.py's list_orders, which already uses rsplit for
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
        items=await _list_items(db, products),
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
    """Search active products by name/description, optionally scoped to a category.

    `query` is 3 to 100 characters (shorter or longer is 422). Results are
    ordered by id and paginated like the product list. This route has its own
    limit per user or client address, RATE_LIMIT_SEARCH (60/minute by
    default)."""

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
        items=await _list_items(db, products),
        total=total,
        page=search.page,
        size=search.size,
        has_more=total > search.page * search.size
    )

@router.get(
    "/products/{product_id}",
    response_model=ProductDetailResponse,
    responses={
        200: ETAG_RESPONSE,
        304: {"description": "Not modified: If-None-Match matches the current ETag", "headers": ETAG_HEADER},
        404: {"description": "Product not found, or not active"},
    },
)
async def get_product(
    product_id: Int32,
    db: DbSession,
    redis: Cache,
    response: Response,
    if_none_match: IfNoneMatch = None,
):
    """Retrieve a single product by ID, with its active variants.

    A product with has_variants true is sold by its variants: each carries its
    own price and stock, and the cart needs a variant_id for it.

    The ETag is a hash of the response JSON, so the database path and the Redis
    copy carry the same tag. A product with variants is hashed with them
    (variant stock moves through orders without touching the product row, so
    the product's own tag would not see it); a product without is hashed as the
    ProductResponse it always was, the tag the admin PUT/PATCH If-Match checks.
    """

    def _conditional(base: ProductResponse, representation: ProductDetailResponse):
        etag = product_etag(base, representation)
        if if_none_match_hits(if_none_match, etag):
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
        response.headers["ETag"] = etag
        return None

    _detail = partial(load_product_detail, db)

    cached_product = await redis.get_cached_product(product_id)
    if cached_product:
        # The entry is the product plus, for a product that had no active
        # variant when it was cached, has_variants false: that is what lets a
        # hit skip the variants query. An entry without the key (written
        # before it existed) or with true still asks the database.
        known_without_variants = cached_product.pop("has_variants", None) is False
        try:
            cached_representation = ProductResponse.model_validate(cached_product)
        except ValidationError:
            cached_representation = None  # unreadable cache entry: rebuild below
        if cached_representation is not None:
            detail = await _detail(
                cached_representation, known_without_variants=known_without_variants
            )
            return _conditional(cached_representation, detail) or detail

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

    detail = await _detail(representation)

    # The Redis copy is the product alone; the variants are added per request.
    # Only "no active variant" is kept with it (see the hit above).
    cached = representation.model_dump()
    if not detail.has_variants:
        cached["has_variants"] = False
    await redis.cache_product(product_id, cached)

    return _conditional(representation, detail) or detail
