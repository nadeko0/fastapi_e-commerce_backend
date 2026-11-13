from typing import Annotated

from fastapi import (
    BackgroundTasks,
    Body,
    HTTPException,
    status,
)
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.api.v1.orders._create import _ORDER_RESPONSE_LOADS, _drop_product_details
from app.api.v1.orders._refunds import _has_open_dispute
from app.api.v1.orders._router import router
from app.core.clock import utc_now
from app.core.dependencies import AdminUser, Cache, CurrentUser, DbSession
from app.core.openapi import RETRY_AFTER_HEADER
from app.models.order import Order
from app.schemas.common import Int32
from app.schemas.order import (
    OrderResponse,
    OrderStatus,
    OrderStatusUpdate,
    PaymentStatus,
)
from app.services.email import (
    send_order_cancellation_email,
    send_order_status_update_email,
)
from app.services.orders import (
    PAID_ONLY_STATUSES,
    cancel_order_and_restock,
    close_attempts_at_provider,
    has_live_payment_attempt,
    is_anonymised_email,
    is_unpaid_and_new,
    is_valid_status_transition,
    open_payment_attempts,
    products_with_cached_stock,
)
from app.services.payment import get_payment_provider

# The transition table lives in app/services/orders.py, shared with the
# Celery worker; kept under this name for the call sites in this module.
_is_valid_status_transition = is_valid_status_transition


@router.get(
    "/{order_id}",
    response_model=OrderResponse,
    responses={404: {"description": "Order not found (an order of another user is not found either)"}},
)
async def get_order(
    order_id: Int32,
    current_user: CurrentUser,
    db: DbSession,
):
    """Retrieve a single order belonging to the current user."""

    # Eager-load shipping_address and items->product: OrderResponse
    # serializes both, so without this each access is a separate lazy-load
    # query (and on an AsyncSession a lazy load is an error, not a query).
    order = (await db.execute(
        select(Order).options(*_ORDER_RESPONSE_LOADS).where(
            Order.id == order_id,
            Order.user_id == current_user.id,
        ).limit(1)
    )).scalars().first()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )
    return OrderResponse.model_validate(order)

@router.put(
    "/{order_id}/status",
    response_model=OrderResponse,
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Order not found"},
        409: {"description": "Invalid status transition, the order is not paid (moving it on), or it is on hold for a payment dispute"},
    },
)
async def update_order_status(
    order_id: Int32,
    background_tasks: BackgroundTasks,
    current_user: AdminUser,  # Admin only
    db: DbSession,
    redis: Cache,
    body: Annotated[OrderStatusUpdate, Body()],
):
    """Transition an order to a new status (admin only).

    Allowed moves: new to confirmed or cancelled, confirmed to processing or
    cancelled, processing to sent or cancelled, sent to delivered or
    cancelled; delivered and cancelled are final. Moving an order to
    confirmed, processing, sent or delivered needs payment_status paid.
    An order whose payment is disputed is on hold: it cannot be moved on,
    only cancelled. Cancelling returns the stock and closes the order's open
    payment attempts at the provider; a paid order is not refunded by it
    (use the refund route)."""

    new_status = body.status

    # See get_order above: OrderResponse.model_validate(order) below serializes
    # shipping_address and items->product, so eager-load both here too.
    # Locked (FOR UPDATE OF orders only: the joined address is on the
    # nullable side of an outer join, which Postgres cannot lock) so that
    # two cancellations, or this and the unpaid-order expiry, cannot both
    # read the old status and both return the stock.
    # order.user.email is read for the notification below: joined here, as a
    # lazy load is not possible on an AsyncSession.
    order = (await db.execute(
        select(Order).options(*_ORDER_RESPONSE_LOADS, joinedload(Order.user))
        .where(Order.id == order_id).with_for_update(of=Order).limit(1)
    )).scalars().first()
    if not order:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )

    # An order whose payment is disputed is on hold: it is not moved on
    # (fulfilled) until the dispute closes. Cancelling it stays possible.
    if new_status != OrderStatus.CANCELLED and await db.run_sync(_has_open_dispute, order.id):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Order is on hold: a payment for it is disputed",
        )

    if not _is_valid_status_transition(OrderStatus(order.status), new_status):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Invalid status transition"
        )

    # A refunded order was paid but no longer is: it is not moved on either.
    if new_status in PAID_ONLY_STATUSES and order.payment_status != PaymentStatus.PAID:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Order is not paid: it cannot be confirmed, processed, sent or delivered",
        )


    attempts: list[tuple[str | None, str]] = []
    if new_status == OrderStatus.CANCELLED:
        # Releases the stock create_order took (the one restock path, shared
        # with the customer's own cancel and the unpaid-order expiry).
        await db.run_sync(cancel_order_and_restock, order)
        attempts = await db.run_sync(open_payment_attempts, order.id)
    else:
        order.status = new_status
        order.updated_at = utc_now()

    restocked = products_with_cached_stock(
        [(item.product_id, item.variant_id) for item in order.items]
    ) if new_status == OrderStatus.CANCELLED else []
    await db.commit()
    await _drop_product_details(redis, restocked)
    # After the commit, outside the transaction: an open Checkout Session or
    # PaymentIntent of a cancelled order could still be paid, for stock that
    # may already be sold again.
    if attempts:
        background_tasks.add_task(close_attempts_at_provider, get_payment_provider(), attempts)


    # The transition was validated above and no status moves to itself, so
    # the status always changed: the customer is always told.
    if not is_anonymised_email(order.user.email):
        background_tasks.add_task(
            send_order_status_update_email,
            order.user.email,
            OrderResponse.model_validate(order)
        )

    return OrderResponse.model_validate(order)


@router.post(
    "/{order_id}/cancel",
    response_model=OrderResponse,
    responses={
        404: {"description": "Order not found (an order of another user is not found either)"},
        409: {"description": "Order is paid, or no longer new; or a payment attempt for it is still in "
                             "progress (Retry-After: 60): finish or abandon it first",
              "headers": RETRY_AFTER_HEADER},
    },
)
async def cancel_own_order(
    order_id: Int32,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    db: DbSession,
    redis: Cache,
):
    """Cancel one of the current user's own orders that has not been paid,
    returning its items to stock.

    Stock is taken when an order is placed, not when it is paid, and only
    an admin could cancel: an order its customer abandoned kept its items
    out of sale until someone noticed. A paid order is not cancelled here;
    that goes through support, with a refund. Unpaid orders are also
    cancelled automatically UNPAID_ORDER_EXPIRE_MINUTES (30 by default)
    after they were placed (cancel_expired_unpaid_orders in app/tasks.py).

    Only an order with status new and payment_status pending or failed can
    be cancelled here. While a payment attempt can still be paid (in
    progress, 3D Secure pending, outcome unknown, or an open Checkout
    Session, for up to CHECKOUT_SESSION_EXPIRE_MINUTES, 60 by default) the
    answer is 409 with Retry-After: 60."""
    # Locked like update_order_status's read, for the same reason; another
    # user's order is a 404, not a 403, as in get_order.
    order = (await db.execute(
        select(Order).options(*_ORDER_RESPONSE_LOADS).where(
            Order.id == order_id,
            Order.user_id == current_user.id,
        ).with_for_update(of=Order).limit(1)
    )).scalars().first()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )

    if not is_unpaid_and_new(order):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a new, unpaid order can be cancelled; contact support for a paid one"
        )
    if await db.run_sync(has_live_payment_attempt, order):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A payment for this order is in progress; finish or abandon it first",
            headers={"Retry-After": "60"},
        )

    await db.run_sync(cancel_order_and_restock, order)
    # Older than the window above, so its Checkout Session has expired, but a
    # PaymentIntent (3D Secure left open) does not expire by itself.
    attempts = await db.run_sync(open_payment_attempts, order.id)
    restocked = products_with_cached_stock(
        [(item.product_id, item.variant_id) for item in order.items]
    )
    await db.commit()
    await _drop_product_details(redis, restocked)
    if attempts:
        background_tasks.add_task(close_attempts_at_provider, get_payment_provider(), attempts)

    if not is_anonymised_email(current_user.email):
        background_tasks.add_task(
            send_order_cancellation_email, current_user.email, OrderResponse.model_validate(order)
        )

    return OrderResponse.model_validate(order)
