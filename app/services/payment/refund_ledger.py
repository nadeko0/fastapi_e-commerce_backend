import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.order import Order
from app.models.payment import Payment
from app.models.payment_ledger import DISPUTE_CLOSED_STATUSES, PaymentDispute, PaymentRefund
from app.schemas.order import PaymentStatus

# The name the code logged under before it moved: log filters and alerts keep matching.
logger = logging.getLogger("app.api.v1.orders")

# Refund.status values (Stripe: pending, requires_action, succeeded, failed,
# canceled) of a refund whose money is not going back to the customer.
# Final: a refund in one of them never changes again.
_REFUND_STATUSES_NOT_RETURNED = ("failed", "canceled")


def _apply_refund_object(db: Session, payment_row: Payment, refund_object: dict[str, Any]) -> bool:
    """
    Bring one recorded refund up to date with the provider's report of it.
    Returns True when the refund turned out not to return the money - it
    failed or was canceled after it had been recorded (Stripe: card
    refunds can fail days later, refund.failed): its amount is taken back
    out of amount_refunded, so the order is no longer refunded by it, and
    it is flagged for manual review, since the customer has to be refunded
    some other way. This used to go unnoticed: the order stayed refunded.
    A failed or canceled refund is final and stays as it is.
    """
    refund_id = refund_object.get("id")
    # Refund.status is nullable in Stripe's API reference.
    status = refund_object.get("status")
    if not isinstance(refund_id, str) or not isinstance(status, str):
        return False
    refund_row = db.scalars(
        select(PaymentRefund).where(PaymentRefund.refund_id == refund_id).limit(1)
    ).first()
    if refund_row is None:
        amount = refund_object.get("amount")
        if isinstance(amount, int) and not isinstance(amount, bool) and amount > 0:
            _record_refund(db, payment_row, refund_id, amount, status, source="provider")
        return False
    if refund_row.status in _REFUND_STATUSES_NOT_RETURNED:
        return False
    if refund_row.status == "succeeded" and status not in _REFUND_STATUSES_NOT_RETURNED:
        return False
    refund_row.status = status
    if status not in _REFUND_STATUSES_NOT_RETURNED:
        return False
    payment_row.amount_refunded -= refund_row.amount
    failure_reason = refund_object.get("failure_reason")
    refund_row.failure_reason = failure_reason if isinstance(failure_reason, str) else None
    refund_row.requires_manual_review = True
    logger.warning(
        "Refund %s of payment %s (order %s) is %s after it was recorded (%s): "
        "the customer was not refunded, needs manual review",
        refund_id, payment_row.id, payment_row.order_id, status, refund_row.failure_reason,
    )
    return True


def _record_refund(
    db: Session, payment_row: Payment, refund_id: str, amount: int, status: str, source: str
) -> PaymentRefund:
    """
    Record one provider refund of `payment_row`'s charge, once, and return
    its row. A refund already recorded (by id) is returned as it is; a new
    one is added, and its amount to payment_row.amount_refunded unless it
    has already failed. The caller holds the order's lock
    (_lock_order_and_reload) and commits.

    `source` says who made the refund. The webhook for a refund can be
    recorded before the /refund request that made it records its own row
    (the event is processed by a worker, concurrently): the row is then
    first recorded as "provider", and the admin's own call, which knows it
    made the refund, corrects it to "admin". A webhook never changes an
    existing row's source.
    """
    refund_row = db.scalars(
        select(PaymentRefund).where(PaymentRefund.refund_id == refund_id).limit(1)
    ).first()
    if refund_row is not None:
        if source == "admin":
            refund_row.source = "admin"
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
    not_completed = (db.scalar(
        select(func.coalesce(func.sum(PaymentRefund.amount), 0)).where(
            PaymentRefund.payment_id == payment_row.id,
            PaymentRefund.status.notin_(("succeeded", *_REFUND_STATUSES_NOT_RETURNED)),
        )
    )) or 0
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
