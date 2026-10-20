import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from fastapi import status as http_status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_active_user, get_current_admin_user
from app.models.address import Address
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.payment import Payment, WebhookEvent
from app.models.product import Product, ProductVariant
from app.models.user import User
from app.schemas.common import APIResponse
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
    send_order_confirmation_email,
    send_order_status_update_email,
)
from app.services.payment import (
    PaymentProvider,
    get_payment_provider,
)
from app.services.payment.exceptions import (
    CardError,
    IdempotencyError,
    InvalidRequestError,
    PaymentProviderTimeoutError,
    SignatureVerificationError,
)
from app.services.payment.types import PaymentIntentStatus
from app.services.redis import RedisService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["orders"])

@router.post(
    "",
    response_model=APIResponse[OrderResponse],
    responses={
        400: {"description": "Cart is empty, profile incomplete, or a cart item is out of stock"},
        403: {"description": "Email verification required before placing orders"},
        404: {"description": "Shipping address not found"},
        422: {"description": "Missing or malformed Idempotency-Key header"},
    },
)
async def create_order(
    shipping_address_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    redis: RedisService = Depends(),
    # Required, not optional. Placing an order is the one request in this API
    # that must never be applied twice, and the client is the only party that
    # knows whether two requests are one attempt retried or two purchases:
    # a timeout, a double click and a second browser tab all look the same
    # from here. While the header was optional, a request without it that was
    # retried after its response was lost created a second order from
    # whatever was still in the cart. Generate one key (a UUID) per checkout
    # attempt and resend the same key on every retry of that attempt.
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=8, max_length=255,
        description="Unique per checkout attempt; resend the same value when retrying.",
    ),
):
    """Create an order from the current user's cart, decrementing stock atomically.

    Requires an `Idempotency-Key` header. Repeating a request with a key
    already used by this user returns the order created the first time.
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
    existing = db.query(Order).filter(
        Order.user_id == current_user.id,
        Order.idempotency_key == idempotency_key,
    ).first()
    if existing:
        return APIResponse.success_response(OrderResponse.from_orm(existing))

    cart = redis.get_cart(current_user.id)
    if not cart or not cart.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cart is empty"
        )


    # Queried through this route's session, not read off
    # current_user.addresses: current_user is a detached snapshot from the
    # auth dependency (app/core/security.py) and cannot lazy-load a
    # relationship. Same rows as before - any address of this user.
    address = db.query(Address).filter(
        Address.id == shipping_address_id,
        Address.user_id == current_user.id,
    ).first()
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
    db.flush()

    total_amount = Decimal('0.00')
    for item in cart.items.values():
        product = db.query(Product).filter(Product.id == item.product_id).first()
        # is_active is re-checked here, for product and variant alike, and
        # not only when the line is added: either can be withdrawn from
        # sale while it sits in a cart, and used to be ordered regardless.
        if not product or not product.is_active:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product {item.product_id} not available in requested quantity"
            )

        variant = None
        if item.variant_id is not None:
            variant = db.query(ProductVariant).filter(
                ProductVariant.id == item.variant_id,
                ProductVariant.product_id == item.product_id,
            ).first()
            if not variant or not variant.is_active:
                db.rollback()
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
            updated_rows = db.query(ProductVariant).filter(
                ProductVariant.id == variant.id,
                ProductVariant.stock_quantity >= item.quantity,
            ).update(
                {ProductVariant.stock_quantity: ProductVariant.stock_quantity - item.quantity},
                synchronize_session=False,
            )
        else:
            updated_rows = db.query(Product).filter(
                Product.id == item.product_id,
                Product.stock_quantity >= item.quantity,
            ).update(
                {Product.stock_quantity: Product.stock_quantity - item.quantity},
                synchronize_session=False,
            )
        if updated_rows == 0:
            db.rollback()
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
    try:
        db.commit()
    except IntegrityError:
        # Two concurrent requests with the same idempotency key both passed
        # the pre-check above; the unique index catches the duplicate here.
        db.rollback()
        existing = db.query(Order).filter(
            Order.user_id == current_user.id,
            Order.idempotency_key == idempotency_key,
        ).first()
        if existing:
            return APIResponse.success_response(OrderResponse.from_orm(existing))
        raise


    # Take out of the cart only what this order was built from, not the
    # whole key: delete_cart() here also discarded anything added between
    # the read at the top and this point, without it having been ordered.
    # The order is already committed, so a Redis failure cannot fail the
    # request - but the ordered lines are then still in the cart. A retry of
    # this request carries the same Idempotency-Key and gets this order
    # back; a deliberate new checkout would order them again. Logged with
    # the order id so that case can be found.
    if not redis.remove_ordered_items_atomic(current_user.id, cart):
        logger.error(
            "Order %s committed but its items could not be removed from user %s's cart",
            order.id, current_user.id,
        )


    background_tasks.add_task(
        send_order_confirmation_email,
        current_user.email,
        OrderResponse.from_orm(order)
    )

    return APIResponse.success_response(OrderResponse.from_orm(order))

@router.get(
    "/{order_id}",
    response_model=APIResponse[OrderResponse],
    responses={404: {"description": "Order not found"}},
)
async def get_order(
    order_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Retrieve a single order belonging to the current user."""

    # Eager-load shipping_address and items->product: OrderResponse
    # serializes both, so without this each access is a separate lazy-load
    # query.
    order = db.query(Order).options(
        joinedload(Order.shipping_address),
        selectinload(Order.items).joinedload(OrderItem.product),
        selectinload(Order.items).joinedload(OrderItem.variant),
    ).filter(
        Order.id == order_id,
        Order.user_id == current_user.id
    ).first()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )
    return APIResponse.success_response(OrderResponse.from_orm(order))

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
    order_id: int,
    # Named "status" for the public API/query param, but that shadows the
    # `status` module (fastapi.status) imported above within this function
    # body - HTTPException below must use the http_status alias instead,
    # or status.HTTP_404_NOT_FOUND resolves against this OrderStatus value
    # and raises AttributeError instead of a proper 404/400 response.
    status: OrderStatus,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_admin_user),  # Admin only
    db: Session = Depends(get_db),
):
    """Transition an order to a new status (admin only)."""

    # See get_order above: OrderResponse.from_orm(order) below serializes
    # shipping_address and items->product, so eager-load both here too.
    order = db.query(Order).options(
        joinedload(Order.shipping_address),
        selectinload(Order.items).joinedload(OrderItem.product),
        selectinload(Order.items).joinedload(OrderItem.variant),
    ).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )


    if not _is_valid_status_transition(order.status, status):
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Invalid status transition"
        )


    old_status = order.status
    order.status = status
    order.updated_at = datetime.utcnow()

    # Cancelling an order must release the stock that create_order reserved
    # for it (the atomic UPDATE...WHERE decrement at checkout time) - without
    # this, every cancelled order permanently leaks its quantity out of
    # stock_quantity, understating real availability forever. Only applies
    # when transitioning INTO cancelled (not a no-op re-cancel, which
    # _is_valid_status_transition already rejects since CANCELLED has no
    # outgoing transitions).
    if status == OrderStatus.CANCELLED:
        for item in order.items:
            if item.variant_id is not None:
                db.query(ProductVariant).filter(ProductVariant.id == item.variant_id).update(
                    {ProductVariant.stock_quantity: ProductVariant.stock_quantity + item.quantity},
                    synchronize_session=False,
                )
            else:
                db.query(Product).filter(Product.id == item.product_id).update(
                    {Product.stock_quantity: Product.stock_quantity + item.quantity},
                    synchronize_session=False,
                )

    db.commit()


    if status != old_status:
        background_tasks.add_task(
            send_order_status_update_email,
            order.user.email,
            OrderResponse.from_orm(order)
        )

    return APIResponse.success_response(OrderResponse.from_orm(order))

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


def _apply_payment_success(
    db: Session,
    order: Order,
    payment_row: Payment,
    background_tasks: BackgroundTasks,
    notify_email: str,
) -> None:
    """
    Shared by the synchronous /pay endpoint (immediate success) and the
    webhook handler (success reported asynchronously, e.g. after
    requires_action). Guards against double-application: if this payment
    row is already marked succeeded, this is a no-op - regression test for
    "same event/attempt processed twice must not re-email or re-transition
    the order".
    """
    if payment_row.status == "succeeded":
        return

    payment_row.status = "succeeded"
    order.payment_status = PaymentStatus.PAID
    if _is_valid_status_transition(order.status, OrderStatus.CONFIRMED):
        order.status = OrderStatus.CONFIRMED
    order.updated_at = datetime.utcnow()
    db.commit()

    background_tasks.add_task(
        send_order_status_update_email,
        notify_email,
        OrderResponse.from_orm(order)
    )


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


def _guard_currency_matches_order(requested_currency: Optional[str], order: Order) -> None:
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
    return {"status": "processed", "applied": False, "requires_manual_review": True}


@router.post(
    "/{order_id}/pay",
    response_model=APIResponse[PaymentResponse],
    responses={
        400: {"description": "Order already paid, cancelled, or amount mismatch"},
        402: {"description": "Card declined by the payment provider"},
        404: {"description": "Order not found"},
        409: {"description": "Idempotency key already used with different payment parameters"},
        503: {"description": "Payment provider unavailable, retry the request"},
    },
)
async def process_payment(
    order_id: int,
    payment: PaymentCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_payment_provider),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Charge an order's total via the payment provider, creating a PaymentIntent."""

    order = db.query(Order).filter(
        Order.id == order_id,
        Order.user_id == current_user.id
    ).first()
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
    if idempotency_key:
        existing_payment = db.query(Payment).filter(
            Payment.order_id == order.id,
            Payment.idempotency_key == idempotency_key,
        ).first()
        if existing_payment:
            return APIResponse.success_response(
                _payment_row_to_response(existing_payment, order)
            )

    _guard_order_payable(order)

    if payment.amount != order.total_amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payment amount does not match order total"
        )
    _guard_currency_matches_order(payment.currency, order)

    amount_cents = _decimal_to_cents(payment.amount)
    provider_idempotency_key = idempotency_key or f"auto-{uuid.uuid4().hex}"

    payment_row = Payment(
        order_id=order.id,
        provider="stripe",
        payment_intent_id=f"pending-{uuid.uuid4().hex[:24]}",
        idempotency_key=idempotency_key,
        amount=amount_cents,
        currency=order.currency,
        status="processing",
    )
    db.add(payment_row)
    try:
        db.commit()
    except IntegrityError:
        # Two concurrent requests with the same idempotency key both passed
        # the pre-check above; the unique index catches the duplicate here.
        db.rollback()
        existing_payment = db.query(Payment).filter(
            Payment.order_id == order.id,
            Payment.idempotency_key == idempotency_key,
        ).first()
        if existing_payment:
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
        db.commit()

        # A different key from the create call above, derived from it.
        # Stripe scopes an idempotency key to one request: reusing the
        # create call's key on the confirm endpoint is rejected with
        # "Keys for idempotent requests can only be used with the same
        # parameters they were first used with". With one shared key, as
        # this used to be, every /pay against real Stripe failed with 409 -
        # found by an end-to-end run against Stripe's test mode
        # (scripts/loadtest, FINDINGS.md); the in-process mock accepted it,
        # so the test suite never saw it. The mock now rejects it too.
        intent = await run_in_threadpool(
            provider.confirm_payment_intent,
            intent.id, idempotency_key=f"{provider_idempotency_key}:confirm",
        )
    except PaymentProviderTimeoutError:
        # Provider unreachable/timed out: we do not know whether Stripe
        # actually processed anything, so the order's payment_status/status
        # must stay exactly as they were before this attempt - only this
        # attempt's own Payment row is marked failed, for audit and so a
        # retry with a fresh Idempotency-Key can be told apart from this
        # dead attempt. Regression test for: a transport failure silently
        # leaving the order half-updated.
        payment_row.status = "failed"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment provider unavailable, please retry",
        )
    except IdempotencyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key already used with different payment parameters",
        )
    except InvalidRequestError as e:
        # A structurally invalid amount/currency (e.g. a non-integer or
        # non-positive amount, or a currency code that isn't a real ISO
        # 4217 code) - the request was never "sent to the processor", so
        # this attempt is a hard failure, not a retryable transport issue.
        payment_row.status = "failed"
        order.updated_at = datetime.utcnow()
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=e.message,
        )
    except CardError as e:
        # The provider *did* respond - with a decline. This is a terminal
        # outcome for this attempt (not a transport failure), so the order
        # is marked failed (not left pending) and the customer can retry
        # with a new attempt/new Idempotency-Key.
        payment_row.status = "failed"
        order.payment_status = PaymentStatus.FAILED
        order.updated_at = datetime.utcnow()
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=e.message,
        )

    if intent.status == PaymentIntentStatus.REQUIRES_ACTION:
        # 3D Secure / SCA required: a distinct, non-terminal state - must
        # not be conflated with either success or failure. The order is
        # left exactly as-is (still pending payment); the client uses
        # client_secret to complete authentication, after which either a
        # follow-up confirm or the payment_intent.succeeded webhook
        # finalizes it.
        payment_row.status = "requires_action"
        db.commit()
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

    # intent.status == SUCCEEDED
    _apply_payment_success(db, order, payment_row, background_tasks, current_user.email)

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
        409: {"description": "Idempotency key already used with different checkout session parameters"},
        503: {"description": "Payment provider unavailable, retry the request"},
    },
)
async def create_checkout_session(
    order_id: int,
    background_tasks: BackgroundTasks,
    payload: Optional[CheckoutSessionCreate] = None,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_payment_provider),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
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

    order = db.query(Order).filter(
        Order.id == order_id,
        Order.user_id == current_user.id
    ).first()
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
    if idempotency_key:
        existing_payment = db.query(Payment).filter(
            Payment.order_id == order.id,
            Payment.idempotency_key == idempotency_key,
        ).first()
        if existing_payment and existing_payment.checkout_session_id:
            return APIResponse.success_response(
                _checkout_session_row_to_response(existing_payment, order)
            )

    _guard_order_payable(order)
    _guard_currency_matches_order(payload.currency, order)

    amount_cents = _decimal_to_cents(order.total_amount)
    provider_idempotency_key = idempotency_key or f"auto-{uuid.uuid4().hex}"
    success_url = payload.success_url or (
        "https://example.com/checkout/success?session_id={CHECKOUT_SESSION_ID}"
    )
    cancel_url = payload.cancel_url or "https://example.com/checkout/cancel"

    payment_row = Payment(
        order_id=order.id,
        provider="stripe",
        payment_intent_id=f"pending-{uuid.uuid4().hex[:24]}",
        idempotency_key=idempotency_key,
        amount=amount_cents,
        currency=order.currency,
        status="processing",
    )
    db.add(payment_row)
    try:
        db.commit()
    except IntegrityError:
        # Two concurrent requests with the same idempotency key both passed
        # the pre-check above; the unique index catches the duplicate here.
        db.rollback()
        existing_payment = db.query(Payment).filter(
            Payment.order_id == order.id,
            Payment.idempotency_key == idempotency_key,
        ).first()
        if existing_payment and existing_payment.checkout_session_id:
            return APIResponse.success_response(
                _checkout_session_row_to_response(existing_payment, order)
            )
        raise

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
    except PaymentProviderTimeoutError:
        # Same reasoning as /pay: unknown whether Stripe actually created
        # the session, so only this attempt's own Payment row is marked
        # failed - the order's payment_status/status stay untouched.
        payment_row.status = "failed"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment provider unavailable, please retry",
        )
    except IdempotencyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key already used with different checkout session parameters",
        )
    except InvalidRequestError as e:
        payment_row.status = "failed"
        order.updated_at = datetime.utcnow()
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=e.message,
        )

    payment_row.checkout_session_id = session.id
    payment_row.checkout_session_url = session.url
    db.commit()

    return APIResponse.success_response(_checkout_session_row_to_response(payment_row, order))


@router.post(
    "/{order_id}/refund",
    response_model=APIResponse[RefundResponse],
    responses={
        400: {"description": "Order not paid, or no successful payment found"},
        403: {"description": "Admin privileges required"},
        404: {"description": "Order not found"},
        409: {"description": "Idempotency key already used with different refund parameters"},
        503: {"description": "Payment provider unavailable, retry the request"},
    },
)
async def refund_payment(
    order_id: int,
    refund_request: RefundCreate,
    current_user: User = Depends(get_current_admin_user),  # Refunds are a support/admin action
    db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_payment_provider),
    redis: RedisService = Depends(),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """Issue a full or partial refund for a paid order (admin only).

    Unlike /pay and /checkout-session, there is no local Payment-row table to
    key a pre-check off of (Payment rows model charge attempts, not refunds,
    and a single payment can be refunded multiple times). The
    Idempotency-Key header is instead forwarded to the payment provider
    as-is, which is where real Stripe already dedupes refund requests by key
    - see StripePaymentProvider.create_refund's own idempotency cache, which
    mirrors that behavior for the mock. Without this, a retried refund
    request (client timeout, double-click "Refund") would create a second,
    distinct refund instead of returning the first one's result."""
    order = db.query(Order).filter(Order.id == order_id).first()
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

    payment_row = (
        db.query(Payment)
        .filter(Payment.order_id == order.id, Payment.status == "succeeded")
        .order_by(Payment.id.desc())
        .first()
    )
    if not payment_row:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="No successful payment found for this order"
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
    db.commit()

    try:
        refund = await run_in_threadpool(
            provider.create_refund,
            payment_intent_id=payment_intent_id,
            amount=refund_amount_cents,
            idempotency_key=idempotency_key,
        )
    except PaymentProviderTimeoutError:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment provider unavailable, please retry",
        )
    except IdempotencyError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Idempotency key already used with different refund parameters",
        )
    except (CardError, InvalidRequestError) as e:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=e.message)

    # provider.create_refund's own idempotency cache means a retried request
    # with the same Idempotency-Key returns the *same* refund.id instead of
    # creating a new one - but that alone doesn't stop this endpoint from
    # double-applying it to payment_row.amount_refunded, since this code
    # would otherwise run unconditionally on both the original call and the
    # replay. SETNX on the refund's own id guards the local bookkeeping the
    # same way the provider guards its own side: the second call sees its
    # key already set and skips straight to returning the (unchanged)
    # current state instead of incrementing amount_refunded a second time.
    # Fails open (treats as new/never-applied) on a Redis error - the
    # provider-level guard above already prevents refunding more than the
    # original charge, so the worst case here is only ever this
    # bookkeeping-accuracy layer, not an actual double refund at Stripe.
    try:
        already_applied = not redis._redis.set(f"refund_applied:{refund.id}", "1", nx=True)
    except Exception as e:
        logger.error("Redis error guarding refund idempotency for %s: %s", refund.id, e)
        already_applied = False

    if not already_applied:
        payment_row.amount_refunded += refund.amount
        is_full_refund = payment_row.amount_refunded >= payment_row.amount
        # A full refund moves the order to REFUNDED; a partial refund leaves
        # it PAID (the order was fulfilled - only part of the charge was
        # returned).
        if is_full_refund:
            order.payment_status = PaymentStatus.REFUNDED
        order.updated_at = datetime.utcnow()
        db.commit()

    return APIResponse.success_response(RefundResponse(
        id=refund.id,
        order_id=order.id,
        amount=_cents_to_decimal(refund.amount),
        currency=refund.currency,
        status=refund.status.value,
        payment_status=order.payment_status,
    ))


@router.post(
    "/webhooks/stripe",
    response_model=APIResponse[dict],
    responses={400: {"description": "Invalid webhook signature"}},
)
async def stripe_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_payment_provider),
    stripe_signature: Optional[str] = Header(default=None, alias="Stripe-Signature"),
):
    """
    Receive Stripe webhook events for both payment flows and reconcile
    order/payment state:
      - payment_intent.succeeded / payment_intent.payment_failed - the
        server-confirmed-PaymentIntent flow (/pay).
      - checkout.session.completed / .async_payment_succeeded /
        .async_payment_failed / .expired - the hosted-redirect Checkout
        Session flow (/checkout-session).
    Idempotent - duplicate deliveries of the same event.id are detected and
    short-circuited.

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
            settings.STRIPE_WEBHOOK_SECRET,
            tolerance_seconds=settings.STRIPE_WEBHOOK_TOLERANCE_SECONDS,
        )
    except SignatureVerificationError:
        # Reject outright - never trust an unsigned/mis-signed payload,
        # and never let it touch any order/payment state.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature",
        )

    # Idempotent event processing: Stripe guarantees at-least-once delivery
    # and retries failed/unacknowledged webhooks for up to 72 hours, so the
    # same event.id can and will arrive more than once. The unique
    # constraint on event_id is the idempotency guard - a duplicate
    # delivery is detected and short-circuited before any state mutation
    # (order update or email) happens a second time.
    webhook_event = WebhookEvent(event_id=event.id, event_type=event.type)
    db.add(webhook_event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return APIResponse.success_response({"status": "already_processed"})

    object_data = event.data.get("object", {}) if isinstance(event.data, dict) else {}
    is_checkout_session_event = event.type.startswith("checkout.session.")

    if is_checkout_session_event:
        # Session events key off Payment.checkout_session_id, not
        # payment_intent_id - a fresh Checkout Session's payment_intent is
        # null until the customer actually completes the hosted page (see
        # CheckoutSession's docstring in app/services/payment/types.py), so
        # it cannot be used to find the Payment row at all event types
        # (e.g. checkout.session.expired never gets one).
        payment_row = (
            db.query(Payment).filter(Payment.checkout_session_id == object_data.get("id")).first()
            if object_data.get("id")
            else None
        )
    else:
        payment_intent_id = object_data.get("id")
        payment_row = (
            db.query(Payment).filter(Payment.payment_intent_id == payment_intent_id).first()
            if payment_intent_id
            else None
        )

    if payment_row is None:
        # Unknown/foreign payment_intent or checkout.session (e.g. a
        # webhook for a test event sent from the Stripe dashboard, or one
        # that predates this payment) - acknowledge so Stripe stops
        # retrying, but there is nothing to apply.
        return APIResponse.success_response({"status": "processed", "applied": False})

    order = db.query(Order).filter(Order.id == payment_row.order_id).first()

    if event.type == "payment_intent.succeeded":
        if order.status == OrderStatus.CANCELLED:
            # Conflict: the order was cancelled after the payment attempt
            # was created, and Stripe is now reporting it as paid (a
            # genuine race, not a bug). We must NOT silently mark a
            # cancelled order as paid - that would ship/refund it out of
            # sequence and hide a real reconciliation problem. Record the
            # true provider state on the Payment row, flag it for manual
            # review, and leave the order's status/payment_status exactly
            # as they are.
            return APIResponse.success_response(
                _flag_payment_for_manual_review(db, payment_row, "order is cancelled")
            )
        if not _reported_charge_matches_order(object_data, "amount_received", order):
            # Stripe charged something other than what the order costs (or
            # in another currency): marking the order paid would ship it
            # for the wrong money. Same handling as the cancelled case.
            return APIResponse.success_response(
                _flag_payment_for_manual_review(
                    db, payment_row, "charged amount or currency differs from the order"
                )
            )

        _apply_payment_success(
            db, order, payment_row, background_tasks, order.user.email
        )
    elif event.type == "payment_intent.payment_failed":
        if payment_row.status != "succeeded":
            payment_row.status = "failed"
            order.payment_status = PaymentStatus.FAILED
            order.updated_at = datetime.utcnow()
            db.commit()
    elif event.type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        # Backfill the real PaymentIntent id now that Stripe has attached
        # one - Session.payment_intent is a string id on the webhook
        # payload (confirmed via Stripe's own docs example for this event:
        # https://docs.stripe.com/payments/momo/save-during-payment), not
        # an expanded object. payment_row.payment_intent_id started out as
        # a "pending-..." placeholder set at session-creation time (see
        # create_checkout_session), matching /pay's own placeholder
        # convention for the same not-yet-known-value situation.
        session_payment_intent = object_data.get("payment_intent")
        if isinstance(session_payment_intent, dict):
            session_payment_intent = session_payment_intent.get("id")
        if session_payment_intent:
            payment_row.payment_intent_id = session_payment_intent

        # checkout.session.completed can fire with payment_status="unpaid"
        # for delayed/async payment methods (e.g. bank debits) - Stripe
        # documents that the real outcome then arrives later via a
        # checkout.session.async_payment_succeeded/_failed event. Only a
        # session that is actually paid (or needs no payment at all) is
        # applied as a success here; an unpaid "completed" session is
        # acknowledged but left pending.
        payment_status_raw = object_data.get("payment_status")
        if event.type == "checkout.session.completed" and payment_status_raw not in (
            "paid",
            "no_payment_required",
        ):
            db.commit()
            return APIResponse.success_response({"status": "processed", "applied": False})

        # Same conflict handling as payment_intent.succeeded above.
        if order.status == OrderStatus.CANCELLED:
            return APIResponse.success_response(
                _flag_payment_for_manual_review(db, payment_row, "order is cancelled")
            )
        if not _reported_charge_matches_order(object_data, "amount_total", order):
            return APIResponse.success_response(
                _flag_payment_for_manual_review(
                    db, payment_row, "charged amount or currency differs from the order"
                )
            )

        _apply_payment_success(
            db, order, payment_row, background_tasks, order.user.email
        )
    elif event.type in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        # Mirrors payment_intent.payment_failed's handling: a terminal
        # failure/expiry for this attempt, applied only if this attempt
        # hasn't already succeeded (defends against an expired/failed event
        # arriving after a completed one due to Stripe's at-least-once,
        # not-strictly-ordered delivery).
        if payment_row.status != "succeeded":
            payment_row.status = "failed"
            order.payment_status = PaymentStatus.FAILED
            order.updated_at = datetime.utcnow()
            db.commit()

    return APIResponse.success_response({"status": "processed", "applied": True})


def _is_valid_status_transition(old_status: OrderStatus, new_status: OrderStatus) -> bool:
    valid_transitions = {
        OrderStatus.NEW: {OrderStatus.CONFIRMED, OrderStatus.CANCELLED},
        OrderStatus.CONFIRMED: {OrderStatus.PROCESSING, OrderStatus.CANCELLED},
        OrderStatus.PROCESSING: {OrderStatus.SENT, OrderStatus.CANCELLED},
        OrderStatus.SENT: {OrderStatus.DELIVERED, OrderStatus.CANCELLED},
        OrderStatus.DELIVERED: set(),
        OrderStatus.CANCELLED: set(),
    }
    return new_status in valid_transitions.get(old_status, set())
