import logging
from collections.abc import Callable
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.models.order import Order
from app.models.payment import Payment
from app.schemas.order import OrderResponse, OrderStatus, PaymentStatus
from app.services.email import send_order_status_update_email
from app.services.orders import (
    close_attempts_at_provider,
    is_anonymised_email,
    open_payment_attempts,
)
from app.services.orders import (
    is_valid_status_transition as _is_valid_status_transition,
)
from app.services.payment import get_payment_provider
from app.services.payment.attempts import _decimal_to_cents

# The name the code logged under before it moved: log filters and alerts keep matching.
logger = logging.getLogger("app.api.v1.orders")

# What Stripe answers for a refund of a charge under dispute (charge_disputed
# seen in its test mode; its error list also names refund_disputed_payment).
DISPUTED_CHARGE_CODES = ("charge_disputed", "refund_disputed_payment")


class TaskQueue(Protocol):
    """What the payment helpers need of `background_tasks`: FastAPI's BackgroundTasks, or
    the after-commit queue the Stripe event worker passes."""

    def add_task(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None: ...


def _apply_payment_success(
    db: Session,
    order: Order,
    payment_row: Payment,
    background_tasks: TaskQueue,
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
    if _is_valid_status_transition(OrderStatus(order.status), OrderStatus.CONFIRMED):
        order.status = OrderStatus.CONFIRMED
    order.updated_at = utc_now()
    db.commit()

    if not is_anonymised_email(notify_email):
        background_tasks.add_task(
            send_order_status_update_email,
            notify_email,
            OrderResponse.model_validate(order)
        )
    # The order's other attempts could still charge. Closed at the provider
    # after the response (or, in the worker, after the event's commit): no
    # request or transaction waits on those calls.
    attempts = open_payment_attempts(db, order.id, except_payment_id=payment_row.id)
    if attempts:
        background_tasks.add_task(close_attempts_at_provider, get_payment_provider(), attempts)
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


def _reported_charge_matches_order(object_data: dict[str, Any], amount_field: str, order: Order) -> bool:
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


def _flag_payment_for_manual_review(db: Session, payment_row: Payment, reason: str) -> dict[str, Any]:
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
