from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import AwareDatetime, Field
from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import joinedload, selectinload

from app.api.v1.admin._router import router
from app.core.clock import as_utc, utc_now
from app.core.config import settings
from app.core.dependencies import AdminUser, Cache, DbSession
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.payment import Payment
from app.models.payment_ledger import DISPUTE_CLOSED_STATUSES, PaymentDispute
from app.models.product import Product
from app.schemas.admin import OrderStatistics
from app.schemas.common import PaginationParams
from app.schemas.order import (
    OrderListResponse,
    OrderResponse,
    OrderStatus,
    PaymentStatus,
)


def _cents(value) -> Decimal:
    """A sum or an average of order amounts as whole cents, halves rounded up
    (an average such as 10.005 is 10.01, the way a shop rounds). SQLite
    answers an average as a float, PostgreSQL as a numeric; str() reads the
    float by its shortest form, not its binary expansion."""
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


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
    # field_direction in one value, and anything else is a 422 (the other
    # lists take sort_by and sort_order apart and ignore an unknown field).
    sort_by: Annotated[
        Annotated[str, Field(
            pattern=f"^({'|'.join(ORDER_SORT_COLUMNS)})_(asc|desc)$",
            json_schema_extra={"enum": [f"{name}_{way}" for name in ORDER_SORT_COLUMNS for way in ("asc", "desc")]},
        )] | None,
        Field(description=(
            f"Sort field and direction in one value, e.g. total_amount_desc; fields: {', '.join(ORDER_SORT_COLUMNS)}. "
            "Without it, newest first."
        )),
    ] = None
    sort_order: Annotated[
        str | None,
        Field(pattern="^(asc|desc)$", description="Has no effect on this list: the direction is part of sort_by."),
    ] = None


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
    # Named "status" for the query param; it shadows fastapi.status here,
    # hence the http_status alias.
    status: OrderStatusFilter = None,
    payment_status: PaymentStatusFilter = None,
    start_date: Annotated[AwareDatetime | None, Query(
        description="Only orders created at or after this instant; ISO 8601 with a UTC offset."
    )] = None,
    end_date: Annotated[AwareDatetime | None, Query(
        description="Only orders created at or before this instant; ISO 8601 with a UTC offset."
    )] = None,
    has_dispute: Annotated[bool | None, Query(description=(
        "true: only orders with a payment dispute that is open or was lost (needs attention); "
        "false: only orders without one. Omitted: all orders."
    ))] = None,
):
    """List orders across all users, with filtering, sorting, and pagination.

    `status` and `payment_status` are matched case-insensitively and an unknown
    value is a 422. `start_date` and `end_date` filter on `created_at`, both
    inclusive, and must carry a UTC offset (a date without one is a 422).
    `page` starts at 1; `size` is 1 to 100 (20 by default)."""

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
    # A date must carry its offset (a naive one is a 422): the comparison is
    # between instants, in UTC like every stored timestamp.
    if start_date:
        filters.append(Order.created_at >= as_utc(start_date))
    if end_date:
        filters.append(Order.created_at <= as_utc(end_date))
    if has_dispute is not None:
        # Open (not closed) or lost: a won or withdrawn dispute needs nothing.
        needs_attention = (
            select(PaymentDispute.id)
            .join(Payment, Payment.id == PaymentDispute.payment_id)
            .where(
                Payment.order_id == Order.id,
                or_(
                    PaymentDispute.status.notin_(DISPUTE_CLOSED_STATUSES),
                    PaymentDispute.status == "lost",
                ),
            )
            .exists()
        )
        filters.append(needs_attention if has_dispute else ~needs_attention)

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

    total = (await db.scalar(select(func.count()).select_from(Order).where(*filters))) or 0
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
    period: Annotated[str, Query(
        pattern="^(24h|7d|30d)$",
        description="The last 24 hours, 7 days or 30 days.",
    )] = "24h",
):
    """Retrieve order/revenue statistics and popular products for a time period.

    Only orders that are paid and not cancelled, created within the period,
    are counted. The result is cached for 5 minutes, so it can lag behind
    new payments. `popular_products` lists up to 10 products by quantity
    sold."""

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


    stats: dict[str, Any] = {
        "period": period,
        "currency": settings.STORE_CURRENCY,
        "order_count": await db.scalar(select(func.count()).select_from(Order).where(*paid_in_period)),
        "total_revenue": _cents(
            await db.scalar(select(func.sum(Order.total_amount)).where(*paid_in_period))
        ),
        "average_order_value": _cents(
            await db.scalar(select(func.avg(Order.total_amount)).where(*paid_in_period))
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
            "total_revenue": _cents(p.total_revenue),
            "currency": settings.STORE_CURRENCY,
        }
        for p in popular_products
    ]

    statistics = OrderStatistics(**stats)
    # JSON mode: the amounts are cached as the strings the client gets.
    await redis.setex(cache_key, 300, statistics.model_dump(mode="json"))

    return statistics
