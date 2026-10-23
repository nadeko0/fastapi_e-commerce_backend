import json
import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Annotated, List  # noqa: UP035 - see the UP006 noqa on response_model

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status
from fastapi import status as http_status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.clock import utc_now
from app.core.config import settings
from app.core.dependencies import AdminUser, Cache, CurrentUser, DbSession, Provider
from app.core.idempotency import ensure_same_request, request_fingerprint
from app.models.address import Address
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.payment import Payment
from app.models.payment_ledger import DISPUTE_CLOSED_STATUSES, PaymentDispute, PaymentRefund
from app.models.product import Product, ProductVariant
from app.schemas.common import APIResponse, Int32
from app.schemas.order import (
    CheckoutSessionCreate,
    CheckoutSessionResponse,
    OrderResponse,
    OrderStatus,
    PaymentCreate,
    PaymentResponse,
    PaymentStatus,
    RefundCreate,
    RefundResponse,
)
from app.services.email import (
    send_order_cancellation_email,
    send_order_confirmation_email,
    send_order_status_update_email,
)
from app.services.orders import (
    cancel_order_and_restock,
    is_unpaid_and_new,
    is_valid_status_transition,
    lock_stock_rows,
)
from app.services.payment.exceptions import (
    CardError,
    IdempotencyError,
    InvalidRequestError,
    PaymentProviderTimeoutError,
    SignatureVerificationError,
)
from app.services.payment.types import PaymentIntentStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["orders"])

# Everything OrderResponse reads off an order: its address, its lines, and
# each line's product and variant (variant_sku). On an AsyncSession a lazy
# load raises MissingGreenlet, so every query whose result is serialized
# carries these options.
_ORDER_RESPONSE_LOADS = (
    joinedload(Order.shipping_address),
    selectinload(Order.items).joinedload(OrderItem.product),
    selectinload(Order.items).joinedload(OrderItem.variant),
)


async def _load_order_for_response(db: AsyncSession, order_id: int) -> Order:
    """The order with everything OrderResponse reads, loaded afresh."""
    return (await db.execute(
        select(Order).options(*_ORDER_RESPONSE_LOADS).where(Order.id == order_id).limit(1)
        .execution_options(populate_existing=True)
    )).scalars().one()


async def _order_by_idempotency_key(
    db: AsyncSession, user_id: int, idempotency_key: str
) -> Order | None:
    """The user's order created under this Idempotency-Key, if any."""
    return (await db.execute(
        select(Order).options(*_ORDER_RESPONSE_LOADS).where(
            Order.user_id == user_id,
            Order.idempotency_key == idempotency_key,
        ).limit(1)
    )).scalars().first()


@router.post(
    "",
    response_model=APIResponse[OrderResponse],
    responses={
        400: {"description": "Cart is empty, profile incomplete, or a cart item is out of stock"},
        403: {"description": "Email verification required before placing orders"},
        404: {"description": "Shipping address not found"},
        422: {"description": "Missing or malformed Idempotency-Key header, or the key was already used with a different shipping_address_id"},
    },
)
async def create_order(
    shipping_address_id: Int32,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    db: DbSession,
    redis: Cache,
    # Required, not optional. Placing an order is the one request in this API
    # that must never be applied twice, and the client is the only party that
    # knows whether two requests are one attempt retried or two purchases:
    # a timeout, a double click and a second browser tab all look the same
    # from here. While the header was optional, a request without it that was
    # retried after its response was lost created a second order from
    # whatever was still in the cart. Generate one key (a UUID) per checkout
    # attempt and resend the same key on every retry of that attempt.
    idempotency_key: Annotated[str, Header(
        alias="Idempotency-Key", min_length=8, max_length=255,
        description="Unique per checkout attempt; resend the same value when retrying.",
    )],
):
    """Create an order from the current user's cart, decrementing stock atomically.

    Requires an `Idempotency-Key` header. Repeating a request with a key
    already used by this user returns the order created the first time;
    a key already used with a different `shipping_address_id` is answered 422.
    """

    if not current_user.is_email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email verification required before placing orders"
        )

    if not current_user.full_name or not current_user.phone:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Complete profile information (full name and phone) required for placing orders"
        )

    # A retried checkout request (e.g. client timeout + retry, double-click)
    # with the same key returns the order already created instead of
    # double-charging stock/placing a duplicate order.
    # What defines this request is the address: the cart is server state, and
    # a replay returns the order the first request built from it.
    fingerprint = request_fingerprint(
        operation="create_order", shipping_address_id=shipping_address_id
    )
    existing = await _order_by_idempotency_key(db, current_user.id, idempotency_key)
    if existing:
        ensure_same_request(existing.idempotency_fingerprint, fingerprint)
        return APIResponse.success_response(OrderResponse.model_validate(existing))

    cart = await redis.get_cart(current_user.id)
    if not cart or not cart.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cart is empty"
        )


    # Queried through this route's session, not read off
    # current_user.addresses: current_user is a detached snapshot from the
    # auth dependency (app/core/security.py) and cannot lazy-load a
    # relationship. Only an active address: deleting one only deactivates
    # it (kept for the orders already shipped there), and a deleted
    # address used to be accepted here like any other.
    address = (await db.execute(
        select(Address).where(
            Address.id == shipping_address_id,
            Address.user_id == current_user.id,
            Address.is_active,
        ).limit(1)
    )).scalars().first()
    if not address:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shipping address not found"
        )


    order = Order(
        user_id=current_user.id,
        status=OrderStatus.NEW,
        payment_status=PaymentStatus.PENDING,
        shipping_address_id=shipping_address_id,
        idempotency_key=idempotency_key,
        idempotency_fingerprint=fingerprint,
        total_amount=Decimal('0.00'),
        currency=settings.STORE_CURRENCY,
    )
    db.add(order)
    # Flush (not commit) to populate order.id from the DB's autoincrement
    # before it's used as OrderItem.order_id below - without this, order.id
    # is still None at this point (session autoflush is off) and every
    # OrderItem insert fails its NOT NULL constraint. Pre-existing bug,
    # never caught because no prior test exercised order creation
    # end-to-end; surfaced while adding payment tests that need real orders.
    #
    # This INSERT is also where two concurrent requests with the same key
    # meet: both passed the pre-check above, and the unique index
    # (uix_order_user_idempotency_key) fails the second one here, not at
    # the commit below. Only the commit used to be guarded, so the loser of
    # that race answered 500 instead of the order the winner created.
    try:
        await db.flush()
    except IntegrityError:
        # The rollback expires every loaded object (expire_on_commit does not
        # cover it), and an expired attribute cannot be read back on an
        # AsyncSession: only current_user (detached, so not expired) and the
        # header value are used below.
        await db.rollback()
        existing = await _order_by_idempotency_key(db, current_user.id, idempotency_key)
        if existing:
            ensure_same_request(existing.idempotency_fingerprint, fingerprint)
            return APIResponse.success_response(OrderResponse.model_validate(existing))
        # Only the key's unique index can fail a bare order INSERT (its
        # foreign keys were just read and nothing deletes orders' users or
        # addresses mid-request); re-raised rather than mistaken for a
        # replay if that ever changes.
        raise  # pragma: no cover

    # Every stock row the lines below change is locked up front, in one
    # order, so that checkouts and cancellations touching the same products
    # in different orders cannot deadlock. The loop stays in cart order: the
    # line reported as short, and the totals, are the same as before.
    await db.run_sync(
        lock_stock_rows, [(item.product_id, item.variant_id) for item in cart.items.values()]
    )

    total_amount = Decimal('0.00')
    for item in cart.items.values():
        product = (await db.execute(
            select(Product).where(Product.id == item.product_id).limit(1)
        )).scalars().first()
        # is_active is re-checked here, for product and variant alike, and
        # not only when the line is added: either can be withdrawn from
        # sale while it sits in a cart, and used to be ordered regardless.
        if not product or not product.is_active:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product {item.product_id} not available in requested quantity"
            )

        variant = None
        if item.variant_id is not None:
            variant = (await db.execute(
                select(ProductVariant).where(
                    ProductVariant.id == item.variant_id,
                    ProductVariant.product_id == item.product_id,
                ).limit(1)
            )).scalars().first()
            if not variant or not variant.is_active:
                await db.rollback()
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Product {item.product_id} not available in requested quantity"
                )

        price_at_time = (
            variant.price_override
            if variant and variant.price_override is not None
            else product.price
        )

        # Atomic conditional decrement instead of read-then-write: under
        # concurrent checkouts, two requests reading stock_quantity=1 and
        # both deciding "enough stock" would oversell. The WHERE clause
        # makes the check-and-decrement a single database operation, so
        # only one of two concurrent requests for the last unit can win.
        # A variant line decrements the variant's own stock_quantity, not
        # the parent Product's - the two are independent counters.
        if variant is not None:
            stock_update = await db.execute(
                update(ProductVariant).where(
                    ProductVariant.id == variant.id,
                    ProductVariant.stock_quantity >= item.quantity,
                ).values(
                    stock_quantity=ProductVariant.stock_quantity - item.quantity
                ).execution_options(synchronize_session=False)
            )
        else:
            stock_update = await db.execute(
                update(Product).where(
                    Product.id == item.product_id,
                    Product.stock_quantity >= item.quantity,
                ).values(
                    stock_quantity=Product.stock_quantity - item.quantity
                ).execution_options(synchronize_session=False)
            )
        if stock_update.rowcount == 0:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product {item.product_id} not available in requested quantity"
            )

        order_item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            variant_id=variant.id if variant else None,
            quantity=item.quantity,
            price_at_time=price_at_time
        )
        db.add(order_item)

        total_amount += price_at_time * item.quantity

    order.total_amount = total_amount
    await db.commit()

    # Read back with get_order's eager options: OrderResponse reads the
    # address, the lines, their products and variants (and the columns the
    # INSERT left to the database), none of which can be lazy-loaded on an
    # AsyncSession. populate_existing refreshes the instance the session
    # already holds.
    order = await _load_order_for_response(db, order.id)

    # Take out of the cart only what this order was built from, not the
    # whole key: delete_cart() here also discarded anything added between
    # the read at the top and this point, without it having been ordered.
    # The order is already committed, so a Redis failure cannot fail the
    # request - but the ordered lines are then still in the cart. A retry of
    # this request carries the same Idempotency-Key and gets this order
    # back; a deliberate new checkout would order them again. Logged with
    # the order id so that case can be found.
    if not await redis.remove_ordered_items_atomic(current_user.id, cart):
        logger.error(
            "Order %s committed but its items could not be removed from user %s's cart",
            order.id, current_user.id,
        )


    background_tasks.add_task(
        send_order_confirmation_email,
        current_user.email,
        OrderResponse.model_validate(order)
    )

    return APIResponse.success_response(OrderResponse.model_validate(order))

@router.get(
    "/{order_id}",
    response_model=APIResponse[OrderResponse],
    responses={404: {"description": "Order not found"}},
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
    return APIResponse.success_response(OrderResponse.model_validate(order))

@router.put(
    "/{order_id}/status",
    response_model=APIResponse[OrderResponse],
    responses={
        400: {"description": "Invalid status transition"},
        403: {"description": "Admin privileges required"},
        404: {"description": "Order not found"},
    },
)
async def update_order_status(
    order_id: Int32,
    # Named "status" for the public API/query param, but that shadows the
    # `status` module (fastapi.status) imported above within this function
    # body - HTTPException below must use the http_status alias instead,
    # or status.HTTP_404_NOT_FOUND resolves against this OrderStatus value
    # and raises AttributeError instead of a proper 404/400 response.
    status: OrderStatus,
    background_tasks: BackgroundTasks,
    current_user: AdminUser,  # Admin only
    db: DbSession,
):
    """Transition an order to a new status (admin only)."""

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
    if status != OrderStatus.CANCELLED and await db.run_sync(_has_open_dispute, order.id):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Order is on hold: a payment for it is disputed",
        )

    if not _is_valid_status_transition(order.status, status):
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Invalid status transition"
        )


    if status == OrderStatus.CANCELLED:
        # Releases the stock create_order took (the one restock path, shared
        # with the customer's own cancel and the unpaid-order expiry).
        await db.run_sync(cancel_order_and_restock, order)
    else:
        order.status = status
        order.updated_at = utc_now()

    await db.commit()


    # The transition was validated above and no status moves to itself, so
    # the status always changed: the customer is always told.
    background_tasks.add_task(
        send_order_status_update_email,
        order.user.email,
        OrderResponse.model_validate(order)
    )

    return APIResponse.success_response(OrderResponse.model_validate(order))


@router.post(
    "/{order_id}/cancel",
    response_model=APIResponse[OrderResponse],
    responses={
        400: {"description": "Order is paid, or no longer new"},
        404: {"description": "Order not found"},
    },
)
async def cancel_own_order(
    order_id: Int32,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    db: DbSession,
):
    """Cancel one of the current user's own orders that has not been paid,
    returning its items to stock.

    Stock is taken when an order is placed, not when it is paid, and only
    an admin could cancel: an order its customer abandoned kept its items
    out of sale until someone noticed. A paid order is not cancelled here;
    that goes through support, with a refund. Unpaid orders are also
    cancelled automatically UNPAID_ORDER_EXPIRE_MINUTES after they were
    placed (cancel_expired_unpaid_orders in app/tasks.py)."""
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
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only a new, unpaid order can be cancelled; contact support for a paid one"
        )

    await db.run_sync(cancel_order_and_restock, order)
    await db.commit()

    background_tasks.add_task(
        send_order_cancellation_email, current_user.email, OrderResponse.model_validate(order)
    )

    return APIResponse.success_response(OrderResponse.model_validate(order))


async def _payment_by_key(
    db: AsyncSession, order_id: int, idempotency_key: str
) -> Payment | None:
    """The order's payment attempt recorded under this Idempotency-Key."""
    return (await db.execute(
        select(Payment).where(
            Payment.order_id == order_id,
            Payment.idempotency_key == idempotency_key,
        ).limit(1)
    )).scalars().first()


def _provider_idempotency_key(kind: str, order: Order, client_key: str) -> str:
    """
    The idempotency key sent to the payment provider for one request about
    `order`. A client's Idempotency-Key is unique only per order in our own
    tables (Payment: order_id + key), while the provider scopes keys to the
    whole account: sent as-is, the same key used for a second order of the
    same amount got the first order's PaymentIntent back, whose confirm
    then reported "succeeded" - the second order marked paid without being
    charged. So the key is namespaced by the kind of request and the order.
    """
    return f"{kind}-order-{order.id}:{client_key}"


def _decimal_to_cents(amount: Decimal) -> int:
    """Stripe (and every real payment processor) represents amounts as
    integers in the smallest currency unit - never as Decimal/float dollars.
    Converting once, here, at the API boundary defends against the classic
    cents-vs-dollars mistake propagating into the provider layer."""
    return int((amount * 100).to_integral_value())


def _cents_to_decimal(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal('0.01'))


# Payment.status (free-text column, mirrors the provider's own PaymentIntent
# status names) -> the more limited PaymentStatus enum exposed in the API.
_PAYMENT_ROW_STATUS_TO_API_STATUS = {
    "processing": PaymentStatus.PENDING,
    "unknown": PaymentStatus.PENDING,
    "requires_action": PaymentStatus.REQUIRES_ACTION,
    "succeeded": PaymentStatus.PAID,
    "failed": PaymentStatus.FAILED,
}


def _payment_row_to_response(payment_row: Payment, order: Order) -> PaymentResponse:
    return PaymentResponse(
        order_id=order.id,
        payment_method="stripe",
        amount=_cents_to_decimal(payment_row.amount),
        currency=payment_row.currency,
        id=payment_row.id,
        status=_PAYMENT_ROW_STATUS_TO_API_STATUS.get(payment_row.status, PaymentStatus.PENDING),
        created_at=payment_row.created_at,
        transaction_id=payment_row.payment_intent_id,
        client_secret=payment_row.client_secret,
        requires_action=payment_row.status == "requires_action",
    )


def _checkout_session_row_to_response(payment_row: Payment, order: Order) -> CheckoutSessionResponse:
    return CheckoutSessionResponse(
        order_id=order.id,
        payment_id=payment_row.id,
        checkout_session_id=payment_row.checkout_session_id,
        url=payment_row.checkout_session_url or "",
        status=_PAYMENT_ROW_STATUS_TO_API_STATUS.get(
            payment_row.status, PaymentStatus.PENDING
        ).value,
        amount=_cents_to_decimal(payment_row.amount),
        currency=payment_row.currency,
        created_at=payment_row.created_at,
    )


def _is_checkout_attempt(payment_row: Payment) -> bool:
    """A Checkout Session attempt that has not stored a session id."""
    return payment_row.payment_intent_id.startswith(_PENDING_CHECKOUT_ID_PREFIX)


def _is_checkout_row(payment_row: Payment) -> bool:
    """An attempt of the Checkout Session flow, with or without its session."""
    return payment_row.checkout_session_id is not None or _is_checkout_attempt(payment_row)


def _ensure_same_request_in_flow(
    payment_row: Payment | None, fingerprint: str, *, checkout: bool
) -> None:
    """
    422 when the attempt recorded under the key came from this flow (/pay, or
    /checkout-session when `checkout`) with a different request. An attempt of
    the other flow keeps the answer it always had (a 409, or the replay): the
    two flows' fingerprints are not comparable.
    """
    if payment_row is not None and _is_checkout_row(payment_row) == checkout:
        ensure_same_request(payment_row.idempotency_fingerprint, fingerprint)


def _replay_checkout_session(payment_row: Payment, order: Order):
    """
    /checkout-session's answer to a key this order has already used. An
    attempt with a session is replayed as it is. One without a session - a
    twin request still waiting on the provider, a session the provider
    refused, a /pay attempt that used the same key - has nothing a
    CheckoutSessionResponse could describe, and the key cannot be used for
    a second attempt: the unique index on (order, key) refuses the row.
    Such a retry used to go on and hit that index, answering 500; it is a
    409 telling the client to start a new attempt with a new key.
    """
    if payment_row.checkout_session_id:
        return APIResponse.success_response(
            _checkout_session_row_to_response(payment_row, order)
        )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            "This Idempotency-Key belongs to a payment attempt without a checkout "
            "session (still in progress, or failed); retry later, or start a new "
            "attempt with a new Idempotency-Key"
        ),
    )


def _apply_payment_success(
    db: Session,
    order: Order,
    payment_row: Payment,
    background_tasks: BackgroundTasks,
    notify_email: str,
) -> bool:
    """
    Shared by the synchronous /pay endpoint (immediate success) and the
    webhook handler (success reported asynchronously, e.g. after
    requires_action). Guards against double-application: if this payment
    row is already marked succeeded, this is a no-op - regression test for
    "same event/attempt processed twice must not re-email or re-transition
    the order".

    Returns False when the success is not applied, and the attempt is
    recorded as succeeded and flagged for manual review (a refund) instead:
    - the order was cancelled after the attempt started (by an admin, by
      its customer, or because it stayed unpaid too long and its stock was
      released). A genuine race, not a bug; marking a cancelled order paid
      would ship or refund it out of sequence and hide a real
      reconciliation problem. Its status and payment_status stay as they
      are.
    - the order was already paid (or refunded) by another attempt: two
      charges for one order. No second confirmation email goes out.
    """
    _lock_order_and_reload(db, order, payment_row)
    if payment_row.status == "succeeded":
        db.commit()
        return True
    if order.status == OrderStatus.CANCELLED:
        _flag_payment_for_manual_review(db, payment_row, "order is cancelled")
        return False
    if order.payment_status in (PaymentStatus.PAID, PaymentStatus.REFUNDED):
        _flag_payment_for_manual_review(
            db, payment_row, "order was already paid by another attempt"
        )
        return False

    payment_row.status = "succeeded"
    order.payment_status = PaymentStatus.PAID
    if _is_valid_status_transition(order.status, OrderStatus.CONFIRMED):
        order.status = OrderStatus.CONFIRMED
    order.updated_at = utc_now()
    db.commit()

    background_tasks.add_task(
        send_order_status_update_email,
        notify_email,
        OrderResponse.model_validate(order)
    )
    return True


def _lock_order_and_reload(db: Session, order: Order, payment_row: Payment) -> None:
    """
    Every change to an order's payment state is a read-decide-write: is it
    already paid, did this attempt already succeed. Without a lock two
    requests (a webhook and /pay's own success, a failure and a success)
    can both read the old state and both write. SELECT ... FOR UPDATE on
    the order row makes the second wait for the first to commit and then
    read what it wrote; the attempt is re-read under that lock too. Both
    are re-read in any case: sessions are expire_on_commit=False
    (app/core/database.py), so /pay's copies predate the provider call.
    The lock lasts until the caller's commit. On SQLite (the test suite)
    FOR UPDATE is not emitted at all; there the re-read is what remains.
    Flushed first, so a change the caller already made (the webhook's
    backfilled payment_intent_id) is kept rather than discarded by the
    re-read.
    """
    db.flush()
    db.refresh(order, with_for_update=True)
    db.refresh(payment_row)


# Order.payment_status values a failed attempt may move the order to FAILED
# from. Never from PAID or REFUNDED: an order has several attempts (a
# declined card then a retry, an abandoned Checkout Session then /pay), and
# a failure reported for one of them - late, out of order, or for an
# attempt the customer gave up on - says nothing about the money another
# attempt already took. Setting FAILED there made a paid order payable
# again (a second charge) and refused its refund.
_PAYMENT_STATUSES_A_FAILURE_MAY_REPLACE = {PaymentStatus.PENDING, PaymentStatus.FAILED}


def _apply_payment_failure(db: Session, order: Order, payment_row: Payment) -> None:
    """
    The one place an attempt's failure is applied: by /pay on a decline,
    and by the webhook for payment_intent.payment_failed,
    checkout.session.expired and checkout.session.async_payment_failed.
    Nothing changes for an attempt that already succeeded (its failure
    event arrived after its success: Stripe delivery is not ordered).
    Otherwise the attempt is marked failed, and the order too unless it
    has been paid.
    """
    # Locked and re-read first: another attempt may have paid the order
    # since `order` was read (see _lock_order_and_reload).
    _lock_order_and_reload(db, order, payment_row)
    if payment_row.status == "succeeded":
        db.commit()
        return
    payment_row.status = "failed"
    if order.payment_status in _PAYMENT_STATUSES_A_FAILURE_MAY_REPLACE:
        order.payment_status = PaymentStatus.FAILED
    order.updated_at = utc_now()
    db.commit()


def _guard_order_payable(order: Order) -> None:
    """
    Shared by /pay and /checkout-session: an order that is already paid or
    cancelled must never accept a new payment attempt of either kind.
    Factored out rather than duplicated so a future third payment flow
    (or a change to either guard) only has one place to update.
    """
    if order.payment_status == PaymentStatus.PAID:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order already paid"
        )
    # Refunded is not "unpaid again": it was paid and the money returned.
    # Accepting a payment here charged the customer a second time and put
    # the order back to paid for goods already refunded.
    if order.payment_status == PaymentStatus.REFUNDED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order has been refunded"
        )

    # An order already cancelled (e.g. by an admin, or by the customer)
    # must never accept a new payment attempt - the resolution for a
    # payment somehow succeeding for a cancelled order is handled
    # separately in the webhook path (flagged for manual review, not
    # silently applied); here we simply refuse to start one.
    if order.status == OrderStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot pay for a cancelled order"
        )


# Payment.payment_intent_id of an attempt until the provider has given it
# a real id (the column is NOT NULL and unique): /pay stores the
# PaymentIntent's id when it is created, and a Checkout Session attempt
# when its session completes.
_PENDING_INTENT_ID_PREFIX = "pending-"
# The placeholder of a Checkout Session attempt (still starts with the
# prefix above, so everything that looks for "pending-" sees it). It tells
# the two flows apart while an attempt has no session id: a key whose
# attempt timed out can only be retried by the flow that made it.
_PENDING_CHECKOUT_ID_PREFIX = f"{_PENDING_INTENT_ID_PREFIX}cs-"


# Payment.status values of an attempt whose outcome is not known yet: the
# request is still talking to the provider (or died doing so), the customer
# is in 3D Secure, or a Checkout Session has not been completed or expired.
# "unknown" is an attempt (/pay or /checkout-session) whose request to the
# provider timed out: the provider may or may not have carried it out, so
# it is neither failed nor still running, and only a retry with its own
# Idempotency-Key settles it.
_IN_FLIGHT_PAYMENT_STATUSES = ("processing", "requires_action", "unknown")


def _guard_no_attempt_in_flight(
    db: Session, order: Order, except_payment_id: int | None = None
) -> None:
    """
    Shared by /pay and /checkout-session, called with the order row locked:
    refuse a new attempt while another one for this order may still charge
    the customer. The Idempotency-Key makes one attempt retried a replay,
    but two attempts with their own keys (two tabs, a client making a new
    key per click) both passed the unpaid guard - the lock is released
    before the provider call - and both could be charged; the second
    success was only flagged for manual review. The caller's own key was
    already checked for a replay, so any attempt found here is another one.

    An attempt in flight for longer than PAYMENT_ATTEMPT_STALE_AFTER_MINUTES
    no longer blocks: a request that crashed after recording its attempt
    would otherwise lock the order until it expires. If such an attempt
    does succeed after all, the webhook flags the second success for manual
    review, as before.

    `except_payment_id` is the attempt being retried by its own key.
    """
    started_after = utc_now() - timedelta(
        minutes=settings.PAYMENT_ATTEMPT_STALE_AFTER_MINUTES
    )
    query = select(Payment.id).where(
        Payment.order_id == order.id,
        Payment.status.in_(_IN_FLIGHT_PAYMENT_STATUSES),
        Payment.created_at > started_after,
    )
    if except_payment_id is not None:
        query = query.where(Payment.id != except_payment_id)
    in_flight = db.scalars(query.limit(1)).first()
    if in_flight is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Another payment attempt for this order is in progress; "
                "finish it or retry with the same Idempotency-Key"
            ),
        )


def _guard_currency_matches_order(requested_currency: str | None, order: Order) -> None:
    """
    Shared by /pay and /checkout-session: the order is charged in its own
    currency only. The currency used to be taken from the request as-is
    while only the amount was compared, so a 100.00 USD order could be paid
    as 100.00 JPY. A client may omit it; one it sends must be the order's.
    """
    if requested_currency is not None and requested_currency != order.currency:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payment currency does not match order currency"
        )


def _reported_charge_matches_order(object_data: dict, amount_field: str, order: Order) -> bool:
    """
    Whether a provider success event charged exactly what the order costs,
    in the order's currency. `amount_field` names where the event carries
    the charged amount (amount_received on a PaymentIntent, amount_total
    on a Checkout Session); Stripe reports currencies in lower case. A
    missing amount or currency is a mismatch, not a pass.
    """
    reported_amount = object_data.get(amount_field)
    reported_currency = object_data.get("currency")
    return (
        isinstance(reported_amount, int)
        and not isinstance(reported_amount, bool)
        and reported_amount == _decimal_to_cents(order.total_amount)
        and isinstance(reported_currency, str)
        and reported_currency.upper() == order.currency
    )


def _flag_payment_for_manual_review(db: Session, payment_row: Payment, reason: str) -> dict:
    """
    A provider reports a payment succeeded, but applying it to the order
    would be wrong (the order was cancelled meanwhile, or the charge is not
    what the order costs). The Payment row records the provider's true
    state and is flagged for a person to reconcile; the order's status and
    payment_status are left exactly as they are.
    """
    payment_row.status = "succeeded"
    payment_row.requires_manual_review = True
    db.commit()
    logger.warning(
        "Payment %s for order %s needs manual review: %s",
        payment_row.id, payment_row.order_id, reason,
    )
    return _MANUAL_REVIEW_RESPONSE


# The webhook's answer for an event recorded but not applied to the order.
_MANUAL_REVIEW_RESPONSE = {"status": "processed", "applied": False, "requires_manual_review": True}


@router.post(
    "/{order_id}/pay",
    response_model=APIResponse[PaymentResponse],
    responses={
        400: {"description": "Order already paid, cancelled, or amount mismatch"},
        402: {"description": "Card declined by the payment provider"},
        404: {"description": "Order not found"},
        409: {"description": "Another attempt for this order is in progress, or idempotency key already used with different payment parameters"},
        422: {"description": "Missing or malformed Idempotency-Key header, or the key was already used with a different request body"},
        503: {"description": "Payment provider unavailable, retry the request"},
    },
)
async def process_payment(
    order_id: Int32,
    payment: PaymentCreate,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    db: DbSession,
    provider: Provider,
    # Required, as on POST /orders and for the same reason. While it was
    # optional, two requests without it that arrived together (a double
    # click, a second tab, a retry after a lost response) both passed the
    # unpaid guard below and both charged the card: the order lock is not
    # held across the provider call, so the second success could only be
    # flagged for manual review, after the customer was charged twice. With
    # a key, the second request of one attempt is the first one's replay.
    idempotency_key: Annotated[str, Header(
        alias="Idempotency-Key", min_length=8, max_length=255,
        description="Unique per payment attempt; resend the same value when retrying.",
    )],
):
    """Charge an order's total via the payment provider, creating a PaymentIntent.

    Requires an `Idempotency-Key` header. Repeating a request with a key
    already used for this order returns the first attempt's outcome; a key
    already used with a different request body is answered 422.
    """

    # Locked (SELECT ... FOR UPDATE) until the commit that records this
    # attempt below, so two attempts check the guards and record themselves
    # one after the other, never both against the same unpaid state. The
    # lock is not held across the provider call (no transaction stays open
    # while Stripe is waited on); what can still happen after it is handled
    # where the outcome is applied (_lock_order_and_reload).
    order = (await db.execute(
        select(Order).where(
            Order.id == order_id,
            Order.user_id == current_user.id,
        ).with_for_update().limit(1)
    )).scalars().first()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )

    # A retried checkout-payment request (client timeout + retry, double
    # click on "Pay") with the same key returns the original attempt's
    # outcome instead of creating a second PaymentIntent / re-charging the
    # card. This is our own DB-level idempotency guard, independent of (and
    # in addition to) the provider's own idempotency-key cache below - a
    # real integration needs both, since a crash between the provider call
    # succeeding and our own commit would otherwise be invisible to us.
    # Checked *before* the "already paid" guard below: a successful retry
    # of the same key on an order that is now paid is exactly the case this
    # guard exists for, and must not be rejected as a duplicate payment.
    existing_payment = await _payment_by_key(db, order.id, idempotency_key)
    # What defines the request is the whole validated body (amount, currency,
    # method, token), so a retry that changes any of it is not a replay. The
    # order is fixed by the path and the key is scoped to it.
    fingerprint = request_fingerprint(
        operation="pay", body=payment.model_dump(mode="json")
    )
    _ensure_same_request_in_flow(existing_payment, fingerprint, checkout=False)
    #
    # One exception: an attempt whose provider request timed out ("unknown")
    # is not an outcome to replay. Stripe's guidance for a request that may
    # or may not have been executed is to send it again with the same
    # idempotency key and parameters, and Stripe then returns the original
    # result; so the retry below goes through to the provider again with
    # this attempt's own keys. Answering it with a "failed" row sent the
    # client to a new key, which could create and charge a second
    # PaymentIntent for the same order.
    retried_attempt = None
    if existing_payment:
        if existing_payment.status != "unknown":
            return APIResponse.success_response(
                _payment_row_to_response(existing_payment, order)
            )
        if _is_checkout_attempt(existing_payment):
            # Sending it as a PaymentIntent would be a second way to
            # charge the order; only /checkout-session settles it.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This Idempotency-Key belongs to a checkout session attempt "
                    "whose outcome is not known yet; retry it on "
                    "/checkout-session with the same Idempotency-Key"
                ),
            )
        retried_attempt = existing_payment

    _guard_order_payable(order)
    await db.run_sync(
        _guard_no_attempt_in_flight, order,
        except_payment_id=retried_attempt.id if retried_attempt else None,
    )

    if payment.amount != order.total_amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payment amount does not match order total"
        )
    _guard_currency_matches_order(payment.currency, order)

    amount_cents = _decimal_to_cents(payment.amount)
    provider_idempotency_key = _provider_idempotency_key("pay", order, idempotency_key)

    if retried_attempt is not None:
        # Claimed under the order's lock: a second retry of the same key
        # now finds the attempt "processing" and is answered with that,
        # instead of sending the provider the same request at the same time.
        payment_row = retried_attempt
        payment_row.status = "processing"
        await db.commit()
    else:
        payment_row = Payment(
            order_id=order.id,
            provider="stripe",
            payment_intent_id=f"{_PENDING_INTENT_ID_PREFIX}{uuid.uuid4().hex[:24]}",
            idempotency_key=idempotency_key,
            idempotency_fingerprint=fingerprint,
            amount=amount_cents,
            currency=order.currency,
            status="processing",
        )
        db.add(payment_row)
        try:
            await db.commit()
        except IntegrityError:
            # Two concurrent requests with the same idempotency key both passed
            # the pre-check above; the unique index catches the duplicate here.
            # The rollback expires `order`, which the replay below reads: it
            # is reloaded (an expired attribute cannot be read on an
            # AsyncSession).
            await db.rollback()
            await db.refresh(order)
            existing_payment = await _payment_by_key(db, order.id, idempotency_key)
            if existing_payment:
                _ensure_same_request_in_flow(existing_payment, fingerprint, checkout=False)
                return APIResponse.success_response(
                    _payment_row_to_response(existing_payment, order)
                )
            raise

    # Every provider call in this module goes through run_in_threadpool.
    # The provider is synchronous (the Stripe SDK makes blocking HTTP calls:
    # measured 0.4 s for a create and 0.7 s for a confirm against Stripe's
    # test mode), and these handlers run on the event loop, so a direct call
    # stopped the whole worker for the duration of every payment - no other
    # request was served for about a second. Each call is made with no
    # database transaction open (the commit just above it), so no pooled
    # connection is held while waiting on Stripe.
    try:
        intent = await run_in_threadpool(
            provider.create_payment_intent,
            amount=amount_cents,
            currency=order.currency,
            idempotency_key=provider_idempotency_key,
            payment_method=payment.payment_method_token,
            metadata={"order_id": str(order.id)},
        )
        payment_row.payment_intent_id = intent.id
        payment_row.client_secret = intent.client_secret
        await db.commit()

        # A different key from the create call above, derived from it.
        # Stripe scopes an idempotency key to one request: reusing the
        # create call's key on the confirm endpoint is rejected with
        # "Keys for idempotent requests can only be used with the same
        # parameters they were first used with". With one shared key, as
        # this used to be, every /pay against real Stripe failed with 409 -
        # found by an end-to-end run against Stripe's test mode
        # (scripts/loadtest, FINDINGS.md); the in-process mock accepted it,
        # so the test suite never saw it. The mock now rejects it too.
        #
        # return_url is where 3D Secure sends the customer back after
        # authenticating: the frontend's checkout page for this order, built
        # on FRONTEND_URL like the Checkout Session pages. Stripe adds the
        # PaymentIntent's id and client secret to it as query parameters.
        intent = await run_in_threadpool(
            provider.confirm_payment_intent,
            intent.id, idempotency_key=f"{provider_idempotency_key}:confirm",
            return_url=f"{settings.FRONTEND_URL}/checkout/return?order_id={order.id}",
        )
    except PaymentProviderTimeoutError as e:
        # Provider unreachable/timed out: we do not know whether Stripe
        # actually processed anything, so the order's payment_status/status
        # must stay exactly as they were before this attempt - only this
        # attempt's own Payment row is marked "unknown" (not failed: it may
        # have been carried out). It keeps its provider keys, so a retry
        # with the same Idempotency-Key is sent again and settles it, and
        # it blocks other attempts meanwhile (_guard_no_attempt_in_flight).
        # Regression test for: a transport failure silently leaving the
        # order half-updated.
        payment_row.status = "unknown"
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment provider unavailable, please retry",
        ) from e
    except IdempotencyError:
        await db.rollback()
        if retried_attempt is not None:
            # Refused before it was carried out: still not settled. The
            # rollback expired the attempt's row; it is reloaded before it
            # is written.
            await db.refresh(payment_row)
            payment_row.status = "unknown"
            await db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key already used with different payment parameters",
        ) from None
    except InvalidRequestError as e:
        # A structurally invalid amount/currency (e.g. a non-integer or
        # non-positive amount, or a currency code that isn't a real ISO
        # 4217 code) - the request was never "sent to the processor", so
        # this attempt is a hard failure, not a retryable transport issue.
        payment_row.status = "failed"
        order.updated_at = utc_now()
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=e.message,
        ) from e
    except CardError as e:
        # The provider *did* respond - with a decline. This is a terminal
        # outcome for this attempt (not a transport failure), so the order
        # is marked failed (not left pending) and the customer can retry
        # with a new attempt/new Idempotency-Key. Through the shared helper:
        # the order may have been paid by another attempt (a Checkout
        # Session completing) since the guard above read it.
        await db.run_sync(_apply_payment_failure, order, payment_row)
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=e.message,
        ) from e

    if intent.status == PaymentIntentStatus.REQUIRES_ACTION:
        # 3D Secure / SCA required: a distinct, non-terminal state - must
        # not be conflated with either success or failure. The order is
        # left exactly as-is (still pending payment); the client uses
        # client_secret to complete authentication, after which either a
        # follow-up confirm or the payment_intent.succeeded webhook
        # finalizes it.
        payment_row.status = "requires_action"
        await db.commit()
        return APIResponse.success_response(PaymentResponse(
            order_id=order.id,
            payment_method=payment.payment_method,
            amount=payment.amount,
            currency=order.currency,
            payment_method_token=payment.payment_method_token,
            id=payment_row.id,
            status=PaymentStatus.REQUIRES_ACTION,
            created_at=payment_row.created_at,
            transaction_id=intent.id,
            client_secret=intent.client_secret,
            requires_action=True,
            next_action=intent.next_action,
        ))

    if intent.status == PaymentIntentStatus.PROCESSING:
        # Accepted but not settled yet (Stripe's "processing", and any
        # status the live provider does not model, e.g. requires_capture,
        # which it reports as PROCESSING). Not a success: no money has been
        # taken. The attempt stays in flight and its outcome arrives by
        # webhook, as for 3D Secure above. This branch and the one below
        # used to be missing - everything other than requires_action was
        # applied as a success, and the order marked paid and confirmed.
        return APIResponse.success_response(_payment_row_to_response(payment_row, order))

    if intent.status != PaymentIntentStatus.SUCCEEDED:
        # Confirmed without an error and still unpaid (requires_payment_method,
        # canceled, ...): a failed attempt, like a decline.
        await db.run_sync(_apply_payment_failure, order, payment_row)
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="The payment was not completed; please try again or use another payment method",
        )

    await db.run_sync(
        _apply_payment_success, order, payment_row, background_tasks, current_user.email
    )

    return APIResponse.success_response(PaymentResponse(
        order_id=order.id,
        payment_method=payment.payment_method,
        amount=payment.amount,
        currency=order.currency,
        payment_method_token=payment.payment_method_token,
        id=payment_row.id,
        status=PaymentStatus.PAID,
        created_at=payment_row.created_at,
        transaction_id=intent.id,
        client_secret=intent.client_secret,
    ))


@router.post(
    "/{order_id}/checkout-session",
    response_model=APIResponse[CheckoutSessionResponse],
    responses={
        400: {"description": "Order already paid or cancelled"},
        404: {"description": "Order not found"},
        409: {"description": "Another attempt for this order is in progress, or idempotency key already used with different checkout session parameters"},
        422: {"description": "Missing or malformed Idempotency-Key header, or the key was already used with a different request body"},
        503: {"description": "Payment provider unavailable, retry the request"},
    },
)
async def create_checkout_session(
    order_id: Int32,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    db: DbSession,
    provider: Provider,
    # Required like /pay's: two sessions created for one order without a
    # key can both be completed, and both charge the customer.
    idempotency_key: Annotated[str, Header(
        alias="Idempotency-Key", min_length=8, max_length=255,
        description="Unique per payment attempt; resend the same value when retrying.",
    )],
    payload: CheckoutSessionCreate | None = None,
):
    """
    Create a Stripe Checkout Session (the fully-hosted-redirect flow) for an
    order's total. Unlike /pay - which confirms a PaymentIntent server-side
    using a payment_method_token the client already collected via
    Stripe.js/Elements - this flow hands card entry/3DS/the entire payment
    UI to Stripe's own hosted page: the caller redirects the customer to the
    returned `url`, and the eventual outcome is reported back asynchronously
    via /webhooks/stripe (checkout.session.completed/.expired/
    .async_payment_failed), never in this response. Both flows remain
    available side by side; this does not replace or change /pay.
    """
    payload = payload or CheckoutSessionCreate()

    # Locked like /pay's read of the order, for the same reason.
    order = (await db.execute(
        select(Order).where(
            Order.id == order_id,
            Order.user_id == current_user.id,
        ).with_for_update().limit(1)
    )).scalars().first()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )

    # Same idempotent-retry guard as /pay: a retried checkout-session
    # request (client timeout + retry, double click) with the same key
    # returns the original attempt's session instead of creating a second
    # one. Checked before the paid/cancelled guard below for the same
    # reason as /pay - a successful retry against an order that is now
    # paid must not be rejected as a fresh duplicate attempt.
    existing_payment = await _payment_by_key(db, order.id, idempotency_key)
    # The validated body is the request (the amount comes from the order).
    fingerprint = request_fingerprint(
        operation="checkout_session", body=payload.model_dump(mode="json")
    )
    _ensure_same_request_in_flow(existing_payment, fingerprint, checkout=True)
    # Except an attempt of this flow whose provider request timed out
    # ("unknown"), as in /pay: Stripe's guidance for a request that may or
    # may not have been executed is to send it again with the same
    # idempotency key and parameters, and Stripe returns the original
    # session. A 409 that sent the client to a new key could create a
    # second Checkout Session for the order.
    retried_attempt = None
    if existing_payment:
        if existing_payment.status != "unknown" or not _is_checkout_attempt(existing_payment):
            return _replay_checkout_session(existing_payment, order)
        retried_attempt = existing_payment

    _guard_order_payable(order)
    await db.run_sync(
        _guard_no_attempt_in_flight, order,
        except_payment_id=retried_attempt.id if retried_attempt else None,
    )
    _guard_currency_matches_order(payload.currency, order)

    amount_cents = _decimal_to_cents(order.total_amount)
    provider_idempotency_key = _provider_idempotency_key("checkout", order, idempotency_key)
    # A client's own URLs are already known to be on the frontend's origin
    # (CheckoutSessionCreate); omitted ones are the frontend's checkout
    # pages. {CHECKOUT_SESSION_ID} is filled in by Stripe.
    success_url = payload.success_url or (
        f"{settings.FRONTEND_URL}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}"
    )
    cancel_url = payload.cancel_url or f"{settings.FRONTEND_URL}/checkout/cancel"

    if retried_attempt is not None:
        # Claimed under the order's lock, as in /pay: a second retry of the
        # same key finds the attempt "processing" and is answered with that.
        payment_row = retried_attempt
        payment_row.status = "processing"
        await db.commit()
    else:
        payment_row = Payment(
            order_id=order.id,
            provider="stripe",
            payment_intent_id=f"{_PENDING_CHECKOUT_ID_PREFIX}{uuid.uuid4().hex[:24]}",
            idempotency_key=idempotency_key,
            idempotency_fingerprint=fingerprint,
            amount=amount_cents,
            currency=order.currency,
            status="processing",
        )
        db.add(payment_row)
        try:
            await db.commit()
        except IntegrityError:
            # Two concurrent requests with the same idempotency key both passed
            # the pre-check above; the unique index catches the duplicate here.
            # The rollback expires `order`, which the replay reads: reloaded
            # first (an expired attribute cannot be read on an AsyncSession).
            await db.rollback()
            await db.refresh(order)
            existing_payment = await _payment_by_key(db, order.id, idempotency_key)
            if existing_payment:
                _ensure_same_request_in_flow(existing_payment, fingerprint, checkout=True)
                return _replay_checkout_session(existing_payment, order)
            # See the same re-raise in create_order: only the key's unique
            # index can fail this INSERT.
            raise  # pragma: no cover

    try:
        session = await run_in_threadpool(
            provider.create_checkout_session,
            amount=amount_cents,
            currency=order.currency,
            idempotency_key=provider_idempotency_key,
            success_url=success_url,
            cancel_url=cancel_url,
            description=f"Order #{order.id}",
            metadata={"order_id": str(order.id)},
        )
    except PaymentProviderTimeoutError as e:
        # Same reasoning as /pay: unknown whether Stripe actually created
        # the session, so only this attempt's own Payment row is touched,
        # marked "unknown" (not failed: it may have been carried out) - the
        # order's payment_status/status stay untouched. The row keeps its
        # key, so a retry with the same Idempotency-Key is sent again and
        # settles it, and it blocks other attempts meanwhile.
        payment_row.status = "unknown"
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment provider unavailable, please retry",
        ) from e
    except IdempotencyError:
        await db.rollback()
        if retried_attempt is not None:
            # Refused before it was carried out: still not settled. The
            # rollback expired the attempt's row; reloaded before written.
            await db.refresh(payment_row)
            payment_row.status = "unknown"
            await db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key already used with different checkout session parameters",
        ) from None
    except InvalidRequestError as e:
        payment_row.status = "failed"
        order.updated_at = utc_now()
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=e.message,
        ) from e

    payment_row.checkout_session_id = session.id
    payment_row.checkout_session_url = session.url
    await db.commit()

    return APIResponse.success_response(_checkout_session_row_to_response(payment_row, order))


@router.post(
    "/{order_id}/refund",
    response_model=APIResponse[RefundResponse],
    responses={
        400: {"description": "Order not paid, or no successful payment found"},
        403: {"description": "Admin privileges required"},
        404: {"description": "Order not found"},
        409: {"description": "Idempotency key already used with different refund parameters"},
        422: {"description": "Missing or malformed Idempotency-Key header"},
        503: {"description": "Payment provider unavailable, retry the request"},
    },
)
async def refund_payment(
    order_id: Int32,
    refund_request: RefundCreate,
    current_user: AdminUser,  # Refunds are a support/admin action
    db: DbSession,
    provider: Provider,
    # Required like /pay's: without a key, a retried or double-clicked
    # partial refund was a second refund, the money returned twice.
    idempotency_key: Annotated[str, Header(
        alias="Idempotency-Key", min_length=8, max_length=255,
        description="Unique per refund; resend the same value when retrying.",
    )],
):
    """Issue a full or partial refund for a paid order (admin only).

    Unlike /pay and /checkout-session, the key is not looked up locally
    first: a refund only gets its id from the provider. The Idempotency-Key
    header is forwarded to the payment provider (namespaced, see
    _provider_idempotency_key), which is where real Stripe dedupes refund
    requests by key - see StripePaymentProvider.create_refund's own
    idempotency cache, which mirrors that behavior for the mock. A retried
    refund request (client timeout, double-click "Refund") therefore gets
    the first one's refund back, and that refund is recorded once
    (PaymentRefund, by refund id)."""
    order = (await db.execute(
        select(Order).where(Order.id == order_id).limit(1)
    )).scalars().first()
    if not order:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )

    if order.payment_status != PaymentStatus.PAID:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Only a paid order can be refunded"
        )

    # The charge that paid the order: its succeeded attempt that is not
    # flagged for manual review. An order charged twice (a stale attempt
    # completing after another paid it) has a second succeeded attempt,
    # flagged; that money is returned by whoever reconciles the flag. This
    # used to take the newest succeeded attempt whatever its flag, which
    # could be the second charge: the order was then marked refunded while
    # the charge that paid it was never returned, and could no longer be.
    payment_row = (await db.execute(
        select(Payment)
        .where(
            Payment.order_id == order.id,
            Payment.status == "succeeded",
            Payment.requires_manual_review.is_(False),
        )
        .order_by(Payment.id.desc())
        .limit(1)
    )).scalars().first()
    if not payment_row:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="No successful payment found for this order"
        )
    # Stripe: refunding a disputed charge returns the money twice - the
    # disputed amount has already been taken from the balance, and a lost
    # dispute is not offset by the refund.
    if await db.run_sync(_has_open_dispute, order.id, payment_row.id):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="This payment is disputed; it cannot be refunded while the dispute is open",
        )

    refund_amount_cents = (
        _decimal_to_cents(refund_request.amount) if refund_request.amount is not None else None
    )

    # Nothing above wrote anything, but the two reads opened a transaction
    # and with it checked out a connection. End it before waiting on the
    # provider (see the note in process_payment): a pooled connection must not sit
    # idle in a transaction for the length of an external HTTP call.
    # expire_on_commit is off, so `order` and `payment_row` stay usable.
    payment_intent_id = payment_row.payment_intent_id
    await db.commit()

    try:
        refund = await run_in_threadpool(
            provider.create_refund,
            payment_intent_id=payment_intent_id,
            amount=refund_amount_cents,
            # Namespaced like /pay's (see _provider_idempotency_key).
            idempotency_key=_provider_idempotency_key("refund", order, idempotency_key),
        )
    except PaymentProviderTimeoutError as e:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment provider unavailable, please retry",
        ) from e
    except IdempotencyError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Idempotency key already used with different refund parameters",
        ) from None
    except (CardError, InvalidRequestError) as e:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=e.message) from e

    # Re-read under the order's lock before adding to it: payment_row was
    # read before the provider call, and another refund of this charge may
    # have been recorded since. Adding to that stale copy overwrote it - two
    # refunds taken by the provider, one recorded, and an order whose
    # refunds covered the charge left "paid".
    await db.run_sync(_lock_order_and_reload, order, payment_row)
    # provider.create_refund's own idempotency cache means a retried request
    # with the same Idempotency-Key returns the *same* refund.id instead of
    # creating a new one; recording it by that id (unique) keeps the replay
    # from being counted a second time. This used to be a Redis SETNX
    # marker that failed open: with Redis down, a retried refund was added
    # to amount_refunded again, and two retries of more than half the
    # charge broke the refunded <= amount constraint and answered 500.
    refund_row = await db.run_sync(
        _record_refund, payment_row, refund.id, refund.amount, refund.status.value,
        source="admin",
    )
    # Refunded once the refunds that have succeeded cover the charge. A
    # partial refund leaves the order PAID (it was fulfilled - only part of
    # the charge was returned), and so does a pending one: it used to be
    # reported as completed, and the order marked refunded, as soon as the
    # provider accepted it.
    await db.run_sync(_settle_refunds, order, payment_row)
    order.updated_at = utc_now()
    await db.commit()

    return APIResponse.success_response(RefundResponse(
        id=refund.id,
        order_id=order.id,
        amount=_cents_to_decimal(refund_row.amount),
        # The charge's own code, as on the order and the payment: Stripe
        # reports currencies in lower case and the live provider passes
        # Refund.currency through as sent, so this answered "usd" for a
        # USD order.
        currency=payment_row.currency,
        # As recorded: a replay, or a request whose refund a webhook has
        # already reported, says where the refund is now.
        status=refund_row.status,
        payment_status=order.payment_status,
    ))


@router.get(
    "/{order_id}/payments",
    response_model=APIResponse[List[dict]],  # noqa: UP006 - List, not list: the repr names the OpenAPI schema (contract)
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Order not found"},
    },
)
async def list_order_payments(
    order_id: Int32,
    current_user: AdminUser,
    db: DbSession,
):
    """Every payment attempt of an order, oldest first, with its refunds and
    disputes (admin only). Amounts are in major units, as everywhere in the
    API. `requires_manual_review` on an attempt (a second charge, a success
    for a cancelled order or for the wrong amount) or on a refund (one that
    failed after it was recorded) marks what a person has to reconcile; an
    open dispute (closed_at null) holds the order."""
    order = (await db.execute(
        select(Order).where(Order.id == order_id).limit(1)
    )).scalars().first()
    if not order:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )
    payments = (await db.execute(
        select(Payment).where(Payment.order_id == order.id).order_by(Payment.id)
    )).scalars().all()
    payment_ids = [p.id for p in payments]
    refunds = (await db.execute(
        select(PaymentRefund).where(
            PaymentRefund.payment_id.in_(payment_ids)
        ).order_by(PaymentRefund.id)
    )).scalars().all()
    disputes = (await db.execute(
        select(PaymentDispute).where(
            PaymentDispute.payment_id.in_(payment_ids)
        ).order_by(PaymentDispute.id)
    )).scalars().all()
    return APIResponse.success_response([
        {
            "id": p.id,
            "status": p.status,
            "amount": _cents_to_decimal(p.amount),
            "amount_refunded": _cents_to_decimal(p.amount_refunded),
            "currency": p.currency,
            "transaction_id": p.payment_intent_id,
            "checkout_session_id": p.checkout_session_id,
            "requires_manual_review": p.requires_manual_review,
            "created_at": p.created_at,
            "refunds": [
                {
                    "id": r.refund_id,
                    "amount": _cents_to_decimal(r.amount),
                    "currency": r.currency,
                    "status": r.status,
                    "source": r.source,
                    "failure_reason": r.failure_reason,
                    "requires_manual_review": r.requires_manual_review,
                    "created_at": r.created_at,
                }
                for r in refunds if r.payment_id == p.id
            ],
            "disputes": [
                {
                    "id": d.dispute_id,
                    "amount": _cents_to_decimal(d.amount),
                    "currency": d.currency,
                    "status": d.status,
                    "reason": d.reason,
                    "closed_at": d.closed_at,
                    "created_at": d.created_at,
                }
                for d in disputes if d.payment_id == p.id
            ],
        }
        for p in payments
    ])


# Refund.status values (Stripe: pending, requires_action, succeeded, failed,
# canceled) of a refund whose money is not going back to the customer.
# Final: a refund in one of them never changes again.
_REFUND_STATUSES_NOT_RETURNED = ("failed", "canceled")


def _record_refund(
    db: Session, payment_row: Payment, refund_id: str, amount: int, status: str, source: str
) -> PaymentRefund:
    """
    Record one provider refund of `payment_row`'s charge, once, and return
    its row. A refund already recorded (by id) is returned as it is; a new
    one is added, and its amount to payment_row.amount_refunded unless it
    has already failed. The caller holds the order's lock
    (_lock_order_and_reload) and commits.
    """
    refund_row = db.scalars(
        select(PaymentRefund).where(PaymentRefund.refund_id == refund_id).limit(1)
    ).first()
    if refund_row is not None:
        return refund_row
    refund_row = PaymentRefund(
        payment_id=payment_row.id,
        refund_id=refund_id,
        amount=amount,
        currency=payment_row.currency,
        status=status,
        source=source,
    )
    db.add(refund_row)
    if status not in _REFUND_STATUSES_NOT_RETURNED:
        payment_row.amount_refunded += amount
    return refund_row


def _settle_refunds(db: Session, order: Order, payment_row: Payment) -> None:
    """
    The order's payment_status from the refunds of the charge that paid
    it: REFUNDED once the refunds that have succeeded cover the charge,
    PAID otherwise. amount_refunded also counts refunds still pending
    (the money is on its way back, and may not be refunded twice), so
    those are taken out first. A charge flagged for manual review (a
    second charge) did not pay the order, and an order that is not paid
    or refunded is left alone.
    """
    if payment_row.requires_manual_review or order.payment_status not in (
        PaymentStatus.PAID, PaymentStatus.REFUNDED,
    ):
        return
    db.flush()
    # A lost dispute returned the whole disputed charge to the customer
    # through their bank: nothing of it is left to refund.
    if db.scalars(
        select(PaymentDispute.id)
        .where(PaymentDispute.payment_id == payment_row.id, PaymentDispute.status == "lost")
        .limit(1)
    ).first() is not None:
        order.payment_status = PaymentStatus.REFUNDED
        return
    not_completed = db.scalar(
        select(func.coalesce(func.sum(PaymentRefund.amount), 0)).where(
            PaymentRefund.payment_id == payment_row.id,
            PaymentRefund.status.notin_(("succeeded", *_REFUND_STATUSES_NOT_RETURNED)),
        )
    )
    returned = payment_row.amount_refunded - not_completed
    order.payment_status = (
        PaymentStatus.REFUNDED if returned >= payment_row.amount else PaymentStatus.PAID
    )


def _has_open_dispute(db: Session, order_id: int, payment_id: int | None = None) -> bool:
    """Whether a charge of the order (or the given one) is disputed and the
    dispute has not closed."""
    query = (
        select(PaymentDispute.id)
        .join(Payment)
        .where(
            Payment.order_id == order_id,
            PaymentDispute.status.notin_(DISPUTE_CLOSED_STATUSES),
        )
    )
    if payment_id is not None:
        query = query.where(PaymentDispute.payment_id == payment_id)
    return db.scalars(query.limit(1)).first() is not None


@router.post(
    "/webhooks/stripe",
    response_model=APIResponse[dict],
    responses={400: {"description": "Invalid webhook signature"}},
)
async def stripe_webhook(
    request: Request,
    db: DbSession,
    provider: Provider,
    stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
):
    """
    Receive Stripe webhook events for both payment flows. The event is
    verified, stored and acknowledged (200) here; the process_stripe_event
    task then reconciles order/payment state:
      - payment_intent.succeeded / payment_intent.payment_failed /
        payment_intent.canceled - the server-confirmed-PaymentIntent flow
        (/pay).
      - checkout.session.completed / .async_payment_succeeded /
        .async_payment_failed / .expired - the hosted-redirect Checkout
        Session flow (/checkout-session).
    Idempotent - duplicate deliveries of the same event.id are answered
    "already_processed" without being stored or applied again.

    No auth dependency: Stripe webhooks are unauthenticated by design and
    are instead trusted via the HMAC signature on the payload (verified
    below) - this is the correct, current Stripe-recommended pattern, not a
    missing-auth bug.
    """
    payload = await request.body()

    try:
        event = provider.construct_webhook_event(
            payload,
            stripe_signature,
            settings.STRIPE_WEBHOOK_SECRET.get_secret_value(),
            tolerance_seconds=settings.STRIPE_WEBHOOK_TOLERANCE_SECONDS,
        )
    except SignatureVerificationError:
        # Reject outright - never trust an unsigned/mis-signed payload,
        # and never let it touch any order/payment state.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature",
        ) from None

    # Stripe's own advice (https://docs.stripe.com/webhooks): answer 2xx
    # quickly, before any complex logic, and handle events asynchronously.
    # The event is stored (with its payload), a Celery task is published to
    # apply it, and the answer goes out; app/services/stripe_events.py has
    # what an event does and how a failing one is retried.
    #
    # Stripe guarantees at-least-once delivery and retries unacknowledged
    # webhooks for up to 72 hours, so the same event.id can and will arrive
    # more than once. The unique constraint on event_id is the idempotency
    # guard: a duplicate is answered without being stored, published or
    # applied again. A store that fails raises, so the answer is a 5xx and
    # Stripe delivers the event again - nothing is lost. A broker that is
    # down does not fail the request: the event is stored, and the sweeper
    # task publishes it later.
    # Imported here: app.services.stripe_events imports this module's
    # payment helpers.
    from app.services import stripe_events

    # store_event is the one sync implementation (the Celery side uses the
    # same module); it runs on the async connection through run_sync, and
    # is looked up on the module so a test can replace it.
    stored = await db.run_sync(
        stripe_events.store_event,
        event.id,
        event.type,
        json.dumps(
            {"id": event.id, "type": event.type, "created": event.created, "data": event.data}
        ),
    )
    if not stored:
        return APIResponse.success_response({"status": "already_processed"})
    # Publishing to the broker is blocking network IO (apply_async).
    await run_in_threadpool(stripe_events.enqueue_event, event.id)
    return APIResponse.success_response({"status": "received"})


# The transition table lives in app/services/orders.py, shared with the
# Celery worker; kept under this name for the call sites in this module.
_is_valid_status_transition = is_valid_status_transition
