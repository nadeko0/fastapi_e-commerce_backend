import logging
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi import status as http_status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.api.merge_patch import PATCH_DESCRIPTION, merge_patch_body
from app.core import database
from app.core.clock import as_utc, utc_now
from app.core.config import settings
from app.core.dependencies import AdminUser, Cache, DbSession, Provider
from app.core.etag import IfMatch, IfNoneMatch, etag_for, if_none_match_hits, require_if_match
from app.core.location import set_location
from app.core.openapi import FORBIDDEN, LOCATION_HEADER, UNAUTHORIZED, DocumentedRoute
from app.core.sorting import order_clauses
from app.models.category import Category
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.payment import (
    WEBHOOK_EVENT_FAILED,
    WEBHOOK_EVENT_RECEIVED,
    WEBHOOK_EVENT_STATUSES,
    WebhookEvent,
)
from app.models.payment_discrepancy import PaymentDiscrepancy
from app.models.product import Product, ProductVariant
from app.schemas.admin import (
    DiscrepancyListResponse,
    DiscrepancyResponse,
    OrderStatistics,
    ReconciliationQueued,
    ReconciliationRunResult,
)
from app.schemas.category import (
    CategoryCreate,
    CategoryResponse,
)
from app.schemas.common import Int32, PaginationParams, sortable_pagination
from app.schemas.order import (
    OrderListResponse,
    OrderResponse,
    OrderStatus,
    PaymentStatus,
)
from app.schemas.product import (
    ProductCreate,
    ProductInDB,
    ProductResponse,
    ProductUpdate,
    ProductVariantCreate,
    ProductVariantListResponse,
    ProductVariantResponse,
    ProductVariantUpdate,
)
from app.schemas.webhook_event import WebhookEventListResponse, WebhookEventResponse
from app.services.reconciliation import open_discrepancies, reconcile_payments
from app.services.stripe_events import enqueue_event
from app.tasks import check_payment_reconciliation

logger = logging.getLogger(__name__)


def _request_datetime(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        logger.debug("%s has no UTC offset, taking it as UTC", name)
    return as_utc(value)


# The filters stay `str`, not an Enum: an Enum parameter would answer a value
# it does not know with FastAPI's list-shaped 422, not the message these
# routes send today (and would stop accepting "PENDING" for "pending"). The
# schema lists the values the routes accept.
OrderStatusFilter = Annotated[
    Annotated[str, Field(json_schema_extra={"enum": [s.value for s in OrderStatus]})] | None,
    Query(description="Order status (case-insensitive)."),
]
PaymentStatusFilter = Annotated[
    Annotated[str, Field(json_schema_extra={"enum": [s.value for s in PaymentStatus]})] | None,
    Query(description="Payment status (case-insensitive)."),
]
WebhookEventStatusFilter = Annotated[
    Annotated[str, Field(json_schema_extra={"enum": list(WEBHOOK_EVENT_STATUSES)})] | None,
    Query(description="Webhook event status."),
]

# Every admin operation needs a signed-in administrator. The other error
# responses (413, 422, 500, 503) are added per operation by DocumentedRoute.
router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    route_class=DocumentedRoute,
    responses={401: UNAUTHORIZED, 403: FORBIDDEN},
)
@router.post(
    "/categories",
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Category created", "headers": LOCATION_HEADER},
        403: {"description": "Admin privileges required"},
        404: {"description": "Parent category not found"},
    },
)
async def create_category(
    category: CategoryCreate,
    request: Request,
    response: Response,
    current_user: AdminUser,
    db: DbSession,
    redis: Cache,
):
    """Create a new category, optionally nested under a parent category."""

    if category.parent_id:
        parent = (
            await db.execute(select(Category).where(Category.id == category.parent_id).limit(1))
        ).scalars().first()
        if not parent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Parent category not found"
            )

    db_category = Category(**category.model_dump())
    if category.parent_id:
        db_category.level = parent.level + 1
        db_category.path = [*parent.path, parent.id]
    else:
        db_category.level = 0
        db_category.path = []

    db.add(db_category)
    await db.commit()
    await db.refresh(db_category)


    await redis.delete("category:tree")

    set_location(request, response, "get_category", category_id=db_category.id)
    return CategoryResponse.model_validate(db_category)

@router.post(
    "/products",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Product created", "headers": LOCATION_HEADER},
        403: {"description": "Admin privileges required"},
        404: {"description": "Category not found"},
    },
)
async def create_product(
    product: ProductCreate,
    request: Request,
    response: Response,
    current_user: AdminUser,
    db: DbSession,
    redis: Cache,
):
    """Create a new product under an existing category."""

    category = (
        await db.execute(select(Category).where(Category.id == product.category_id).limit(1))
    ).scalars().first()
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )


    db_product = Product(**product.model_dump())
    db.add(db_product)
    await db.commit()
    await db.refresh(db_product)


    # populate_existing: the product is already in the session, and its
    # category must be loaded by this query (no lazy load under asyncio).
    db_product = (
        await db.execute(
            select(Product)
            .options(joinedload(Product.category))
            .where(Product.id == db_product.id)
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).scalars().first()


    await redis.invalidate_category_cache()


    # ProductResponse requires category_name, which is not a Product column,
    # so from_attributes alone can't populate it. Validate the ORM-mapped
    # fields first (ProductInDB has no such requirement), then build the
    # response from that clean field set plus category_name - avoids
    # spreading Product.__dict__ (which includes SQLAlchemy's internal
    # _sa_instance_state) into the Pydantic constructor.
    base = ProductInDB.model_validate(db_product, from_attributes=True)
    set_location(request, response, "get_product", product_id=db_product.id)
    return ProductResponse(**base.model_dump(), category_name=db_product.category.name)

# PATCH shares the PUT handler: same schema, checks, side effects, response.
@router.patch(
    "/products/{product_id}",
    response_model=ProductResponse,
    summary="Patch Product",
    description=PATCH_DESCRIPTION,
    openapi_extra=merge_patch_body("ProductUpdate"),
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Product or category not found"},
    },
)
@router.put(
    "/products/{product_id}",
    response_model=ProductResponse,
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Product or category not found"},
        412: {"description": "If-Match does not match the product's current ETag"},
    },
)
async def update_product(
    product_id: Int32,
    product: ProductUpdate,
    current_user: AdminUser,
    db: DbSession,
    redis: Cache,
    response: Response,
    if_match: IfMatch = None,
):
    """Update fields on an existing product.

    With If-Match (an ETag from GET /products/{id} or a previous update, or
    *), the update is applied only if the product is unchanged since (412
    otherwise). Without it the update is unconditional, as before.
    """

    stmt = select(Product).options(joinedload(Product.category)).where(Product.id == product_id).limit(1)
    if if_match is not None:
        # Hold the row until commit so the check and the write are atomic
        # (a no-op on SQLite).
        stmt = stmt.with_for_update(of=Product)
    db_product = (await db.execute(stmt)).scalars().first()
    if not db_product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )
    if if_match is not None:
        current = ProductResponse(
            **ProductInDB.model_validate(db_product, from_attributes=True).model_dump(),
            category_name=db_product.category.name,
        )
        require_if_match(if_match, etag_for(current))

    update_data = product.model_dump(exclude_unset=True)
    # Same check as create_product. Without it a missing category_id was
    # written to the row: a generic 500 from the foreign key on Postgres,
    # and on a database that does not enforce it a committed product whose
    # category does not exist.
    if "category_id" in update_data and not (
        await db.execute(
            select(Category.id).where(Category.id == update_data["category_id"]).limit(1)
        )
    ).first():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )

    for field, value in update_data.items():
        setattr(db_product, field, value)

    db_product.updated_at = utc_now()
    await db.commit()
    await db.refresh(db_product)


    db_product = (
        await db.execute(
            select(Product)
            .options(joinedload(Product.category))
            .where(Product.id == db_product.id)
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).scalars().first()


    await redis.invalidate_product_cache(product_id)
    await redis.invalidate_category_cache()


    base = ProductInDB.model_validate(db_product, from_attributes=True)
    result = ProductResponse(**base.model_dump(), category_name=db_product.category.name)
    response.headers["ETag"] = etag_for(result)
    return result

@router.delete(
    "/products/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Product not found"},
    },
)
async def delete_product(
    product_id: Int32,
    current_user: AdminUser,
    db: DbSession,
    redis: Cache,
):
    """Delete a product, or soft-delete (deactivate) it if it has existing orders."""

    db_product = (
        await db.execute(select(Product).where(Product.id == product_id).limit(1))
    ).scalars().first()
    if not db_product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )


    if await db.scalar(
        select(func.count()).select_from(OrderItem).where(OrderItem.product_id == product_id)
    ):

        db_product.is_active = False
        db_product.updated_at = utc_now()
        await db.commit()
    else:

        await db.delete(db_product)
        await db.commit()


    await redis.invalidate_product_cache(product_id)
    await redis.invalidate_category_cache()

# The columns the admin order list can be sorted by. sort_by used to accept
# any attribute of the Order class (hasattr): relationships such as "user"
# or "items" and SQLAlchemy internals such as "metadata" passed the check
# and then failed in ORDER BY with a 500. Same approach as
# ProductFilter.sort_by: a closed list, enforced by the query schema.
ORDER_SORT_COLUMNS = {
    "id": Order.id,
    "created_at": Order.created_at,
    "updated_at": Order.updated_at,
    "total_amount": Order.total_amount,
    "status": Order.status,
    "payment_status": Order.payment_status,
}


class AdminOrderListParams(PaginationParams):
    sort_by: str | None = Field(
        None, pattern=f"^({'|'.join(ORDER_SORT_COLUMNS)})_(asc|desc)$"
    )


@router.get(
    "/orders",
    response_model=OrderListResponse,
    responses={
        403: {"description": "Admin privileges required"},
        422: {"description": "Invalid status, payment_status, or sort_by value"},
    },
)
async def list_orders(
    pagination: Annotated[AdminOrderListParams, Depends()],
    current_user: AdminUser,
    db: DbSession,
    # Named "status" for the public API/query param; see the identical note
    # in orders.update_order_status - this shadows the fastapi.status
    # module within this function body, so error responses below must use
    # the http_status alias instead.
    status: OrderStatusFilter = None,
    payment_status: PaymentStatusFilter = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
):
    """List orders across all users, with filtering, sorting, and pagination."""

    # Eager-load shipping_address (many-to-one) with joinedload and items
    # -> product (one-to-many) with selectinload: OrderResponse serializes
    # both for every order in the page, so without this each row would
    # trigger its own lazy-load queries (N+1) for shipping_address and for
    # each item's product.
    # OrderResponse also reads each item's variant (variant_sku), which is
    # loaded here too: no lazy load is possible under asyncio. The total is
    # counted from the same filters, without the eager loading.
    query = select(Order).options(
        joinedload(Order.shipping_address),
        selectinload(Order.items).options(
            joinedload(OrderItem.product),
            joinedload(OrderItem.variant),
        ),
    )
    filters = []


    if status:
        try:
            order_status = OrderStatus(status.lower())
            filters.append(Order.status == order_status)
        except ValueError:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid order status. Valid values are: {', '.join([s.value for s in OrderStatus])}"
            ) from None

    if payment_status:
        try:
            pay_status = PaymentStatus(payment_status.lower())
            filters.append(Order.payment_status == pay_status)
        except ValueError:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid payment status. Valid values are: {', '.join([s.value for s in PaymentStatus])}"
            ) from None
    # A date without an offset is taken to be UTC (as clients always sent
    # them); one with an offset is converted. Either way the comparison is
    # between instants.
    if start_date:
        filters.append(Order.created_at >= _request_datetime(start_date, "start_date"))
    if end_date:
        filters.append(Order.created_at <= _request_datetime(end_date, "end_date"))

    # Apply sorting. AdminOrderListParams' pattern has already answered 422
    # for anything but a whitelisted field_asc/field_desc, so both lookups
    # below always succeed.
    if pagination.sort_by:
        field, order = pagination.sort_by.rsplit('_', 1)
        column = ORDER_SORT_COLUMNS[field]
        # The id breaks ties in the direction of the sort (a page boundary
        # inside a run of equal values must not repeat or skip an order).
        if order == 'desc':
            query = query.order_by(desc(column), Order.id.desc())
        else:
            query = query.order_by(column, Order.id)
    else:
        query = query.order_by(Order.created_at.desc(), Order.id.desc())

    total = await db.scalar(select(func.count()).select_from(Order).where(*filters))
    orders = (
        await db.execute(
            query.where(*filters)
            .offset((pagination.page - 1) * pagination.size)
            .limit(pagination.size)
        )
    ).scalars().all()

    return OrderListResponse(
        items=[OrderResponse.model_validate(order) for order in orders],
        total=total,
        page=pagination.page,
        size=pagination.size,
        has_more=total > pagination.page * pagination.size,
    )

@router.get(
    "/stats",
    response_model=OrderStatistics,
    responses={
        403: {"description": "Admin privileges required"},
        422: {"description": "Invalid period value"},
    },
)
async def get_statistics(
    current_user: AdminUser,
    db: DbSession,
    redis: Cache,
    period: Annotated[str, Query(pattern="^(24h|7d|30d)$")] = "24h",
):
    """Retrieve order/revenue statistics and popular products for a time period."""

    cache_key = f"admin_stats_{period}"
    cached_stats = await redis.get(cache_key)
    if cached_stats:
        # Written before the field existed? Add it (the cache lives 5 minutes).
        cached_stats.setdefault("currency", settings.STORE_CURRENCY)
        for entry in cached_stats.get("popular_products", []):
            entry.setdefault("currency", settings.STORE_CURRENCY)
        return cached_stats


    now = utc_now()
    if period == "24h":
        start_date = now - timedelta(hours=24)
    elif period == "7d":
        start_date = now - timedelta(days=7)
    else:  # 30d
        start_date = now - timedelta(days=30)


    paid_in_period = (
        Order.created_at >= start_date,
        Order.status != OrderStatus.CANCELLED,
        Order.payment_status == PaymentStatus.PAID,
    )


    stats = {
        "period": period,
        "currency": settings.STORE_CURRENCY,
        "order_count": await db.scalar(select(func.count()).select_from(Order).where(*paid_in_period)),
        "total_revenue": float(
            await db.scalar(select(func.sum(Order.total_amount)).where(*paid_in_period)) or 0
        ),
        "average_order_value": float(
            await db.scalar(select(func.avg(Order.total_amount)).where(*paid_in_period)) or 0
        ),
    }


    popular_products = (
        await db.execute(
            select(
                Product.id,
                Product.name,
                func.sum(OrderItem.quantity).label('total_quantity'),
                func.sum(OrderItem.quantity * OrderItem.price_at_time).label('total_revenue')
            )
            .select_from(Product)
            .join(OrderItem, OrderItem.product_id == Product.id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(*paid_in_period)
            .group_by(Product.id)
            .order_by(desc('total_quantity'))
            .limit(10)
        )
    ).all()

    stats["popular_products"] = [
        {
            "id": p.id,
            "name": p.name,
            "total_quantity": p.total_quantity,
            "total_revenue": float(p.total_revenue),
            "currency": settings.STORE_CURRENCY,
        }
        for p in popular_products
    ]


    await redis.setex(cache_key, 300, stats)

    return stats

# --- Product variants -------------------------------------------------
#
# ProductVariant (app/models/product.py) is additive: a Product with no
# variants still works exactly as before, using its own price/stock_quantity.
# Cart (app/api/v1/cart.py) and checkout (app/api/v1/orders.py) are
# variant-aware: an optional variant_id on a cart line prices and checks
# stock against that variant instead of the parent product, and
# decrements/restocks ProductVariant.stock_quantity independently of
# Product.stock_quantity.


async def _get_product_or_404(db: AsyncSession, product_id: int) -> Product:
    product = (
        await db.execute(select(Product).where(Product.id == product_id).limit(1))
    ).scalars().first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )
    return product


async def _variant_sku_exists(db: AsyncSession, sku: str) -> bool:
    """Pre-check only - the real guarantee against a duplicate SKU is the
    unique constraint on ProductVariant.sku, since this read-then-write
    check has a race window between two concurrent requests (see the
    IntegrityError handling in _commit_variant below). Factored out mainly so tests can force the race window (make
    this return False despite a real conflicting row already existing)
    without needing genuine thread interleaving against the test suite's
    single shared SQLite connection."""
    found = await db.execute(select(ProductVariant.id).where(ProductVariant.sku == sku).limit(1))
    return found.first() is not None


async def _commit_variant(db: AsyncSession, sku: str | None, variant_id: int | None = None) -> None:
    """Commit a created or updated variant, telling a SKU conflict apart
    from any other failed constraint.

    Both variant routes used to answer every IntegrityError with 409 "A
    variant with this SKU already exists", whatever constraint had actually
    failed. The driver's message is not parsed to tell them apart (it
    differs between Postgres and SQLite and between versions); after the
    rollback the question is simply asked again: does another variant now
    hold this SKU? Deliberately not _variant_sku_exists, which tests replace
    to force the race window open. Any other violation is a 422 with a
    generic detail; the database's own message, which names constraints and
    carries the statement's values, is only logged."""
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        if sku is not None:
            holder = select(ProductVariant.id).where(ProductVariant.sku == sku)
            if variant_id is not None:
                holder = holder.where(ProductVariant.id != variant_id)
            if (await db.execute(holder.limit(1))).first() is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A variant with this SKU already exists"
                ) from e
        logger.warning("Variant write violated a database constraint", exc_info=e)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The variant violates a data constraint"
        ) from e


@router.post(
    "/products/{product_id}/variants",
    response_model=ProductVariantResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Variant created", "headers": LOCATION_HEADER},
        403: {"description": "Admin privileges required"},
        404: {"description": "Product not found"},
        409: {"description": "SKU already in use"},
        422: {"description": "Invalid variant data"},
    },
)
async def create_product_variant(
    product_id: Int32,
    variant: ProductVariantCreate,
    request: Request,
    response: Response,
    current_user: AdminUser,
    db: DbSession,
):
    """Create a new variant (e.g. a size/color combination) under a product."""

    await _get_product_or_404(db, product_id)

    if await _variant_sku_exists(db, variant.sku):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A variant with this SKU already exists"
        )

    db_variant = ProductVariant(product_id=product_id, **variant.model_dump())
    db.add(db_variant)
    # The pre-check above is a plain read-then-write: two concurrent
    # requests creating a variant with the same SKU can both pass it before
    # either commits, so the real guarantee is the DB's unique constraint on
    # ProductVariant.sku, not the pre-check. _commit_variant turns the
    # loser's IntegrityError into the same 409 the pre-check path returns
    # for the sequential case, instead of a generic 500.
    await _commit_variant(db, variant.sku)
    await db.refresh(db_variant)

    set_location(
        request, response, "get_product_variant",
        product_id=product_id, variant_id=db_variant.id,
    )
    return ProductVariantResponse.model_validate(db_variant)

@router.get(
    "/products/{product_id}/variants",
    response_model=ProductVariantListResponse,
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Product not found"},
    },
)
async def list_product_variants(
    product_id: Int32,
    # Every route on this router needs it: this one used to answer anyone,
    # inactive variants, SKUs and exact stock levels included.
    current_user: AdminUser,
    db: DbSession,
):
    """List all variants belonging to a product."""

    await _get_product_or_404(db, product_id)

    variants = (
        await db.execute(
            select(ProductVariant)
            .where(ProductVariant.product_id == product_id)
            .order_by(ProductVariant.id)
        )
    ).scalars().all()

    return ProductVariantListResponse(
        items=[ProductVariantResponse.model_validate(v) for v in variants],
        total=len(variants),
    )

@router.get(
    "/products/{product_id}/variants/{variant_id}",
    response_model=ProductVariantResponse,
    responses={
        304: {"description": "Not modified: If-None-Match matches the variant's current ETag"},
        403: {"description": "Admin privileges required"},
        404: {"description": "Product or variant not found"},
    },
)
async def get_product_variant(
    product_id: Int32,
    variant_id: Int32,
    current_user: AdminUser,
    db: DbSession,
    response: Response,
    if_none_match: IfNoneMatch = None,
):
    """Retrieve one variant of a product.

    The ETag is computed exactly as on PUT/PATCH (hash of the
    ProductVariantResponse JSON), so it can be sent back as If-Match.
    """

    await _get_product_or_404(db, product_id)

    db_variant = (await db.execute(
        select(ProductVariant)
        .where(ProductVariant.id == variant_id, ProductVariant.product_id == product_id)
        .limit(1)
    )).scalars().first()
    if not db_variant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Variant not found"
        )

    result = ProductVariantResponse.model_validate(db_variant)
    etag = etag_for(result)
    if if_none_match_hits(if_none_match, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return result


# PATCH shares the PUT handler: same schema, checks, side effects, response.
@router.patch(
    "/products/{product_id}/variants/{variant_id}",
    response_model=ProductVariantResponse,
    summary="Patch Product Variant",
    description=PATCH_DESCRIPTION,
    openapi_extra=merge_patch_body("ProductVariantUpdate"),
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Product or variant not found"},
        409: {"description": "SKU already in use"},
        422: {"description": "Invalid variant data"},
    },
)
@router.put(
    "/products/{product_id}/variants/{variant_id}",
    response_model=ProductVariantResponse,
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Product or variant not found"},
        409: {"description": "SKU already in use"},
        412: {"description": "If-Match does not match the variant's current ETag"},
        422: {"description": "Invalid variant data"},
    },
)
async def update_product_variant(
    product_id: Int32,
    variant_id: Int32,
    variant: ProductVariantUpdate,
    current_user: AdminUser,
    db: DbSession,
    response: Response,
    if_match: IfMatch = None,
):
    """Update fields on an existing variant of a product.

    With If-Match (the ETag of an earlier update response of this variant, or
    *), applied only if the variant is unchanged since (412 otherwise).
    """

    await _get_product_or_404(db, product_id)

    stmt = (
        select(ProductVariant)
        .where(
            ProductVariant.id == variant_id,
            ProductVariant.product_id == product_id,
        )
        .limit(1)
    )
    if if_match is not None:
        stmt = stmt.with_for_update()
    db_variant = (await db.execute(stmt)).scalars().first()
    if not db_variant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Variant not found"
        )
    if if_match is not None:
        require_if_match(if_match, etag_for(ProductVariantResponse.model_validate(db_variant)))

    update_data = variant.model_dump(exclude_unset=True)
    if (
        "sku" in update_data
        and update_data["sku"] != db_variant.sku
        and await _variant_sku_exists(db, update_data["sku"])
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A variant with this SKU already exists"
        )

    for field, value in update_data.items():
        setattr(db_variant, field, value)

    db_variant.updated_at = utc_now()
    # Same read-then-write race as create_product_variant above: the
    # "sku" != db_variant.sku pre-check can be stale by the time this
    # commits if a concurrent request claimed the same SKU in between.
    await _commit_variant(db, update_data.get("sku"), variant_id)
    await db.refresh(db_variant)

    result = ProductVariantResponse.model_validate(db_variant)
    response.headers["ETag"] = etag_for(result)
    return result


# -- Payment reconciliation ----------------------------------------------------------

DISCREPANCY_SORT_COLUMNS = {
    "id": PaymentDiscrepancy.id,
    "detected_at": PaymentDiscrepancy.detected_at,
    "last_seen_at": PaymentDiscrepancy.last_seen_at,
    "kind": PaymentDiscrepancy.kind,
}
DiscrepancyListParams = sortable_pagination("DiscrepancyListParams", tuple(DISCREPANCY_SORT_COLUMNS))
WEBHOOK_EVENT_SORT_COLUMNS = {
    "id": WebhookEvent.id,
    "created_at": WebhookEvent.created_at,
    "status": WebhookEvent.status,
}
WebhookEventListParams = sortable_pagination("WebhookEventListParams", tuple(WEBHOOK_EVENT_SORT_COLUMNS))


@router.get(
    "/reconciliation/discrepancies",
    response_model=DiscrepancyListResponse,
    responses={403: {"description": "Admin privileges required"}},
)
async def list_reconciliation_discrepancies(
    pagination: Annotated[DiscrepancyListParams, Depends()],
    current_user: AdminUser,
    db: DbSession,
    order_id: Int32 | None = None,
):
    """The differences the payment reconciliation found between our payments
    and the provider's and that still stand, oldest first (admin only):
    which payment, what kind of difference, what each side says, and since
    when. Optionally those of one order.

    sort_by (id, detected_at, last_seen_at, kind) and sort_order (asc, desc) sort
    the list, ties broken by id in the same direction; without sort_by, or with
    another value, the order is oldest first.
    """
    rows, total = await db.run_sync(
        open_discrepancies, page=pagination.page, size=pagination.size, order_id=order_id,
        order_by=order_clauses(
            pagination.sort_by, pagination.sort_order, DISCREPANCY_SORT_COLUMNS,
            PaymentDiscrepancy.id, [PaymentDiscrepancy.detected_at, PaymentDiscrepancy.id],
        ),
    )
    return DiscrepancyListResponse(
        items=[
            DiscrepancyResponse(
                id=d.id,
                order_id=p.order_id,
                payment_id=p.id,
                transaction_id=p.payment_intent_id,
                kind=d.kind,
                subject=d.subject,
                ours=d.ours,
                theirs=d.theirs,
                detected_at=d.detected_at,
                last_seen_at=d.last_seen_at,
            )
            for d, p in rows
        ],
        total=total,
        page=pagination.page,
        size=pagination.size,
        has_more=total > pagination.page * pagination.size,
    )


@router.post(
    "/reconciliation/run",
    response_model=ReconciliationRunResult,
    responses={
        202: {"description": "The full run is queued (no order_id)", "model": ReconciliationQueued},
        403: {"description": "Admin privileges required"},
        404: {"description": "Order not found"},
        503: {"description": "Task queue unavailable, the full run was not queued"},
    },
)
async def run_reconciliation(
    current_user: AdminUser,
    db: DbSession,
    provider: Provider,
    order_id: Int32 | None = None,
):
    """Reconcile now (admin only). With order_id: every payment of that order,
    whatever its age, and the outcome in the response. Without: the same run
    as the daily one, queued for the worker (202), because it makes up to
    two provider calls per payment."""
    if order_id is None:
        try:
            # retry=False: with the broker down, fail at once instead of
            # sitting in kombu's publish retries.
            await run_in_threadpool(check_payment_reconciliation.apply_async, retry=False)
        except Exception as e:
            logger.warning("Could not queue the reconciliation run", exc_info=e)
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Task queue unavailable; reconciliation was not queued",
                headers={"Retry-After": "5"},
            ) from e
        return JSONResponse(
            ReconciliationQueued(queued=True).model_dump(),
            status_code=http_status.HTTP_202_ACCEPTED,
        )
    if not (await db.execute(select(Order.id).where(Order.id == order_id).limit(1))).first():
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Order not found",
        )
    # End the read transaction: nothing stays open on the route's session
    # while the provider is called, and the thread works on a session of its
    # own (an AsyncSession must not be handed to another thread).
    await db.commit()

    def reconcile_order():
        with database.session_scope() as thread_db:
            return reconcile_payments(thread_db, provider, order_id=order_id).as_dict()

    return await run_in_threadpool(reconcile_order)


@router.get(
    "/webhook-events",
    response_model=WebhookEventListResponse,
    responses={
        403: {"description": "Admin privileges required"},
        422: {"description": "Invalid status value"},
    },
)
async def list_webhook_events(
    pagination: Annotated[WebhookEventListParams, Depends()],
    current_user: AdminUser,
    db: DbSession,
    # Named "status" for the public API/query param; see list_orders above.
    status: WebhookEventStatusFilter = None,
):
    """
    List the Stripe webhook events the app stored, newest first, optionally
    by status (received, processing, processed, retrying, failed). Without
    the payload: it is the provider's raw object. A "failed" event ran out
    of retries and can be replayed.

    sort_by (id, created_at, status) and sort_order (asc, desc) sort the list, ties
    broken by id in the same direction; without sort_by, or with another value,
    the order is newest first.
    """
    filters = []
    if status:
        if status not in WEBHOOK_EVENT_STATUSES:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid webhook event status. Valid values are: {', '.join(WEBHOOK_EVENT_STATUSES)}",
            )
        filters.append(WebhookEvent.status == status)

    total = await db.scalar(select(func.count()).select_from(WebhookEvent).where(*filters))
    events = (
        await db.execute(
            select(WebhookEvent)
            .where(*filters)
            .order_by(*order_clauses(
                pagination.sort_by, pagination.sort_order, WEBHOOK_EVENT_SORT_COLUMNS,
                WebhookEvent.id, [WebhookEvent.created_at.desc(), WebhookEvent.id.desc()],
            ))
            .offset((pagination.page - 1) * pagination.size)
            .limit(pagination.size)
        )
    ).scalars().all()
    return WebhookEventListResponse(
        items=[WebhookEventResponse.model_validate(event) for event in events],
        total=total,
        page=pagination.page,
        size=pagination.size,
        has_more=total > pagination.page * pagination.size,
    )


@router.post(
    "/webhook-events/{event_id}/replay",
    response_model=WebhookEventResponse,
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Unknown webhook event"},
        409: {"description": "The event has not failed"},
    },
)
async def replay_webhook_event(
    event_id: str,
    current_user: AdminUser,
    db: DbSession,
):
    """
    Send a failed Stripe event through the worker again (after the cause of
    its failure is fixed): it becomes "received" with its attempts reset and
    is published. If the broker is down it stays received and the sweeper
    publishes it. Only a failed event can be replayed.
    """
    # Locked, so two admins replaying the same event publish it once: the
    # second finds it received.
    event = (
        await db.execute(
            select(WebhookEvent)
            .where(WebhookEvent.event_id == event_id)
            .with_for_update()
            .limit(1)
        )
    ).scalars().first()
    if event is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Webhook event not found"
        )
    if event.status != WEBHOOK_EVENT_FAILED:
        # Read before the rollback: it expires the event, and an expired
        # attribute cannot be loaded on the loop.
        current_status = event.status
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"Only a failed webhook event can be replayed (this one is {current_status})",
        )
    event.status = WEBHOOK_EVENT_RECEIVED
    event.attempts = 0
    await db.commit()
    await run_in_threadpool(enqueue_event, event.event_id)
    return WebhookEventResponse.model_validate(event)
