from typing import TYPE_CHECKING, Annotated

from fastapi import Query
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload, selectinload

from app.api.v1.users._router import router
from app.core.dependencies import CurrentUser, DbSession
from app.core.sorting import order_clauses
from app.models.order import Order
from app.models.order_items import OrderItem
from app.schemas.common import (
    PaginationParams,
    sortable_pagination,
)
from app.schemas.order import OrderListResponse, OrderResponse

ORDER_SORT_COLUMNS = {"id": Order.id, "created_at": Order.created_at, "total_amount": Order.total_amount}
if TYPE_CHECKING:
    UserOrderListParams = PaginationParams
else:
    UserOrderListParams = sortable_pagination("UserOrderListParams", tuple(ORDER_SORT_COLUMNS))


@router.get("/orders", response_model=OrderListResponse)
async def get_user_orders(
    pagination: Annotated[UserOrderListParams, Query()],
    current_user: CurrentUser,
    db: DbSession,
):
    """List the current user's orders, most recent first, paginated (`page`
    from 1, `size` 1 to 100, 20 by default). `sort_by` is one of id,
    created_at, total_amount, with `sort_order` asc (the default when `sort_by`
    is given) or desc. An unpaid order shows status `new` until it is paid,
    cancelled, or expired UNPAID_ORDER_EXPIRE_MINUTES (30 by default) after
    it was placed."""

    total = await db.scalar(
        select(func.count()).select_from(Order).where(Order.user_id == current_user.id)
    )
    assert total is not None, "SELECT count(*) always returns an integer"

    # Eager-load shipping_address (many-to-one) and items->product/variant
    # (one-to-many): OrderResponse serializes them for every order in the
    # page (variant_sku reads item.variant), and an AsyncSession cannot
    # lazy-load them while the response is built.
    orders = (await db.execute(
        select(Order)
        .options(
            joinedload(Order.shipping_address),
            selectinload(Order.items).joinedload(OrderItem.product),
            selectinload(Order.items).joinedload(OrderItem.variant),
        )
        .where(Order.user_id == current_user.id)
        .order_by(*order_clauses(
            pagination.sort_by, pagination.sort_order, ORDER_SORT_COLUMNS, Order.id,
            [Order.created_at.desc(), Order.id.desc()],
        ))
        .offset((pagination.page - 1) * pagination.size)
        .limit(pagination.size)
    )).scalars().all()
    return OrderListResponse(
        items=[OrderResponse.model_validate(order) for order in orders],
        total=total,
        page=pagination.page,
        size=pagination.size,
        has_more=total > pagination.page * pagination.size,
    )
