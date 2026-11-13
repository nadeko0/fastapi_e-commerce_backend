import hashlib
import logging
from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.core.config import settings
from app.models.order import Order
from app.models.payment import Payment
from app.schemas.order import OrderStatus, PaymentStatus
from app.services.payment.exceptions import PaymentProviderTimeoutError

# The name the module had before the orders package split: log filters and
# alerts keep matching.
logger = logging.getLogger("app.api.v1.orders")


def _guard_order_payable(order: Order) -> None:
    """
    Shared by /pay and /checkout-session: an order that is already paid or
    cancelled must never accept a new payment attempt of either kind.
    Factored out rather than duplicated so a future third payment flow
    (or a change to either guard) only has one place to update.
    """
    if order.payment_status == PaymentStatus.PAID:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Order already paid"
        )
    # Refunded is not "unpaid again": it was paid and the money returned.
    # Accepting a payment here charged the customer a second time and put
    # the order back to paid for goods already refunded.
    if order.payment_status == PaymentStatus.REFUNDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Order has been refunded"
        )

    # An order already cancelled (e.g. by an admin, or by the customer)
    # must never accept a new payment attempt - the resolution for a
    # payment somehow succeeding for a cancelled order is handled
    # separately in the webhook path (flagged for manual review, not
    # silently applied); here we simply refuse to start one.
    if order.status == OrderStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot pay for a cancelled order"
        )


def _guard_payment_window_open(order: Order) -> None:
    """
    Shared by /pay and /checkout-session for a new attempt (a retry of an
    existing one by its own key is not refused): an order is paid within
    UNPAID_ORDER_EXPIRE_MINUTES of being placed. The expiry sweep spares an
    order while an attempt of it is young, so a fresh attempt now and then
    used to keep an order nobody paid for, and its stock, alive for good.
    """
    if order.created_at <= utc_now() - timedelta(minutes=settings.UNPAID_ORDER_EXPIRE_MINUTES):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The payment window of this order has closed; place a new order",
        )


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
    would otherwise lock the order until it expires. Once an attempt succeeds,
    other open attempts are closed at the provider (sessions expired,
    requires_action intents cancelled); if a second charge completes nonetheless,
    it is flagged for manual review.

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


def _log_attempt_left_unknown(
    error: PaymentProviderTimeoutError, flow: str, payment_row: Payment, provider_key: str
) -> None:
    """One WARNING line when a /pay or /checkout-session attempt is left
    "unknown" because the provider could not be reached or answered with an
    error: without it the client's 503 had no cause anywhere in the logs (a
    Stripe 429 could not be told from a dropped connection). Stripe's
    Request-Id finds the request in the Dashboard. The idempotency key is
    logged only as a short SHA-256 prefix: enough to match the retries of
    one attempt, not the key."""
    details = {
        "flow": flow,
        "order_id": payment_row.order_id,
        "payment_id": payment_row.id,
        "error_class": error.error_class or type(error).__name__,
        "stripe_request_id": error.request_id,
        "http_status": error.http_status,
        "provider_code": error.provider_code,
        "idempotency_key_sha256": hashlib.sha256(provider_key.encode()).hexdigest()[:16],
    }
    logger.warning(
        "Payment attempt %s of order %s left unknown: provider unavailable "
        "(%s, Stripe request %s, HTTP %s, code %s)",
        payment_row.id, payment_row.order_id, details["error_class"],
        error.request_id, error.http_status, error.provider_code,
        extra=details,
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
