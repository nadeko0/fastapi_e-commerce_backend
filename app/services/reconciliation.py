"""
Daily reconciliation: compare our payment records with the provider's and
record every difference (PaymentDiscrepancy).

Tests prove the code is right; this catches what goes wrong anyway - a
webhook that never arrived, a refund made in the Stripe Dashboard while we
were down, a bug nobody tested. Per payment it compares

- the status (ours: succeeded / failed / canceled / still in flight, against
  the PaymentIntent's),
- the amount (received, for a succeeded one) and the currency,
- the total refunded: our Payment.amount_refunded against the sum of the
  provider's refunds that did or may still return money (not failed or
  canceled), as the rest of the code counts it,
- every refund: one the provider has and we do not, one we have and the
  provider does not, and a refund's status and amount.

It only reports. A payment is NOT flagged requires_manual_review: that flag
means "this charge did not pay the order" and makes /refund pass over the
payment and the order's refunded state stop following its refunds, so
setting it on the charge that did pay the order would make it unrefundable.
The discrepancy rows (GET /admin/reconciliation/discrepancies) are the flag.

Idempotent: a difference is one row per (payment, kind, subject); seeing it
again updates the row, a difference that is gone is marked resolved, and one
that comes back reopens its row.

The provider is called with no database transaction open and one payment at a
time: what a payment looked like is read first, the provider asked, and the
rows written and committed afterwards. A webhook landing in between shows as
a difference until the next run finds it gone.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.core.config import settings
from app.models.payment import Payment
from app.models.payment_discrepancy import (
    KIND_AMOUNT,
    KIND_CURRENCY,
    KIND_PAYMENT_MISSING,
    KIND_REFUND_AMOUNT,
    KIND_REFUND_NOT_RECORDED,
    KIND_REFUND_STATUS,
    KIND_REFUND_UNKNOWN,
    KIND_REFUNDED_TOTAL,
    KIND_STATUS,
    PaymentDiscrepancy,
)
from app.services.payment.base import PaymentProvider
from app.services.payment.exceptions import CardError, PaymentProviderError
from app.services.payment.types import PaymentIntent, PaymentIntentStatus, Refund

logger = logging.getLogger(__name__)

# Payment.payment_intent_id of a Checkout Session attempt before the customer
# completed the hosted page (app/api/v1/orders.py, _PENDING_INTENT_ID_PREFIX):
# there is no PaymentIntent to ask about yet.
PENDING_INTENT_ID_PREFIX = "pending-"

# Refund statuses whose money is not going back to the customer; they are
# left out of the provider's refunded total, like Payment.amount_refunded.
_REFUND_NOT_RETURNED = ("failed", "canceled")

_S = PaymentIntentStatus
# Payment.status -> the PaymentIntent statuses it agrees with.
_EXPECTED_INTENT_STATUS: dict[str, set[PaymentIntentStatus]] = {
    "succeeded": {_S.SUCCEEDED},
    "canceled": {_S.CANCELED},
    # A declined attempt waits for another card; a timed-out one never
    # reached the provider. Succeeded or still processing there is a
    # difference: the money may have been taken.
    "failed": {
        _S.REQUIRES_PAYMENT_METHOD, _S.REQUIRES_CONFIRMATION, _S.REQUIRES_ACTION, _S.CANCELED,
    },
    "processing": {_S.PROCESSING, _S.REQUIRES_CONFIRMATION},
    # A /pay request that timed out: the provider may have got no further
    # than creating the intent, or may have confirmed it.
    "unknown": {_S.PROCESSING, _S.REQUIRES_CONFIRMATION},
    "requires_action": {_S.REQUIRES_ACTION},
}


@dataclass(frozen=True)
class Difference:
    kind: str
    subject: str
    ours: str | None
    theirs: str | None


@dataclass(frozen=True)
class RefundSnapshot:
    refund_id: str
    amount: int
    status: str


@dataclass(frozen=True)
class PaymentSnapshot:
    """What one payment looked like when the run read it."""

    id: int
    payment_intent_id: str
    status: str
    amount: int
    amount_refunded: int
    currency: str
    refunds: tuple[RefundSnapshot, ...]


@dataclass
class ReconciliationResult:
    checked: int = 0          # payments compared with the provider
    skipped: int = 0          # nothing to ask the provider about (no PaymentIntent yet)
    errors: int = 0           # payments the provider could not answer for
    opened: int = 0           # differences newly found or found again
    resolved: int = 0         # differences that are gone
    truncated: bool = False   # more payments in the window than RECONCILIATION_MAX_PAYMENTS
    aborted: bool = False     # stopped early: too many provider failures in a row
    error_payment_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "checked": self.checked, "skipped": self.skipped, "errors": self.errors,
            "opened": self.opened, "resolved": self.resolved,
            "truncated": self.truncated, "aborted": self.aborted,
        }


def snapshot_payment(payment: Payment) -> PaymentSnapshot:
    return PaymentSnapshot(
        id=payment.id,
        payment_intent_id=payment.payment_intent_id,
        status=payment.status,
        amount=payment.amount,
        amount_refunded=payment.amount_refunded,
        currency=payment.currency,
        refunds=tuple(
            RefundSnapshot(r.refund_id, r.amount, r.status)
            for r in sorted(payment.refunds, key=lambda r: r.id)
        ),
    )


def provider_refunded_total(refunds: list[Refund]) -> int:
    return sum(r.amount for r in refunds if _refund_status(r) not in _REFUND_NOT_RETURNED)


def _refund_status(refund: Refund) -> str:
    return refund.provider_status or refund.status.value


def compare(
    payment: PaymentSnapshot, intent: PaymentIntent | None, refunds: list[Refund]
) -> list[Difference]:
    """Every way `payment` differs from the provider's `intent` and
    `refunds`. `intent` None means the provider has no such payment."""
    if intent is None:
        return [Difference(KIND_PAYMENT_MISSING, "", payment.payment_intent_id, None)]

    found: list[Difference] = []
    if intent.status not in _EXPECTED_INTENT_STATUS.get(payment.status, set()):
        found.append(Difference(KIND_STATUS, "", payment.status, intent.status.value))

    charged = intent.amount_received if intent.status == _S.SUCCEEDED else intent.amount
    if payment.amount != charged:
        found.append(Difference(KIND_AMOUNT, "", str(payment.amount), str(charged)))
    if payment.currency.upper() != intent.currency.upper():
        found.append(Difference(KIND_CURRENCY, "", payment.currency, intent.currency))

    their_total = provider_refunded_total(refunds)
    if payment.amount_refunded != their_total:
        found.append(
            Difference(KIND_REFUNDED_TOTAL, "", str(payment.amount_refunded), str(their_total))
        )

    ours = {r.refund_id: r for r in payment.refunds}
    theirs = {r.id: r for r in refunds}
    for refund_id in sorted(ours.keys() | theirs.keys()):
        ours_r, theirs_r = ours.get(refund_id), theirs.get(refund_id)
        if theirs_r is None:
            found.append(Difference(
                KIND_REFUND_UNKNOWN, refund_id, f"{ours_r.status} {ours_r.amount}", None))
        elif ours_r is None:
            found.append(Difference(
                KIND_REFUND_NOT_RECORDED, refund_id, None,
                f"{_refund_status(theirs_r)} {theirs_r.amount}"))
        else:
            if ours_r.status != _refund_status(theirs_r):
                found.append(Difference(
                    KIND_REFUND_STATUS, refund_id, ours_r.status, _refund_status(theirs_r)))
            if ours_r.amount != theirs_r.amount:
                found.append(Difference(
                    KIND_REFUND_AMOUNT, refund_id, str(ours_r.amount), str(theirs_r.amount)))
    return found


def record(
    db: Session, payment_id: int, found: list[Difference], now: datetime
) -> tuple[int, int]:
    """Bring the payment's discrepancy rows in line with `found`; commits.
    Returns (opened, resolved): rows created or reopened, rows closed."""
    rows = {
        (d.kind, d.subject): d
        for d in db.scalars(
            select(PaymentDiscrepancy).where(PaymentDiscrepancy.payment_id == payment_id)
        )
    }
    opened = resolved = 0
    seen = set()
    for diff in found:
        key = (diff.kind, diff.subject)
        seen.add(key)
        row = rows.get(key)
        if row is None:
            db.add(PaymentDiscrepancy(
                payment_id=payment_id, kind=diff.kind, subject=diff.subject,
                ours=diff.ours, theirs=diff.theirs, detected_at=now, last_seen_at=now,
            ))
            opened += 1
            continue
        if row.resolved_at is not None:
            row.resolved_at = None
            row.detected_at = now
            opened += 1
        row.ours, row.theirs, row.last_seen_at = diff.ours, diff.theirs, now
    for key, row in rows.items():
        if key not in seen and row.resolved_at is None:
            row.resolved_at = now
            resolved += 1
    db.commit()
    return opened, resolved


def _ask_provider(
    provider: PaymentProvider, payment_intent_id: str
) -> tuple[PaymentIntent | None, list[Refund]]:
    """The provider's PaymentIntent and refunds; (None, []) if it has no
    such PaymentIntent. Raises PaymentProviderError for any other failure."""
    try:
        intent = provider.retrieve_payment_intent(payment_intent_id)
    except CardError as e:
        if e.code != "resource_missing":
            raise
        return None, []
    return intent, provider.list_refunds(payment_intent_id)


def reconcile_payments(
    db: Session,
    provider: PaymentProvider,
    *,
    order_id: int | None = None,
    now: datetime | None = None,
) -> ReconciliationResult:
    """Reconcile the payments of the window (see the RECONCILIATION_*
    settings), or every payment of `order_id` whatever its age. Stripe
    errors for one payment are counted and the run goes on with the next."""
    now = now or utc_now()
    query = select(Payment.id)
    if order_id is not None:
        query = query.where(Payment.order_id == order_id)
    else:
        query = query.where(
            Payment.created_at >= now - timedelta(days=settings.RECONCILIATION_WINDOW_DAYS),
            Payment.created_at <= now - timedelta(minutes=settings.RECONCILIATION_MIN_AGE_MINUTES),
        )
    # One more than the cap, to know whether the cap cut anything off.
    limit = settings.RECONCILIATION_MAX_PAYMENTS
    ids = list(db.scalars(query.order_by(Payment.id.desc()).limit(limit + 1)))

    result = ReconciliationResult(truncated=len(ids) > limit)
    if result.truncated:
        logger.warning(
            "Reconciliation: more than %s payments in the window; the oldest are not checked",
            limit,
        )
    consecutive_errors = 0
    for payment_id in ids[:limit]:
        payment = db.get(Payment, payment_id)
        if payment.payment_intent_id.startswith(PENDING_INTENT_ID_PREFIX):
            result.skipped += 1
            continue
        snapshot = snapshot_payment(payment)
        # End the read transaction: no connection waits on the provider.
        db.commit()
        try:
            intent, refunds = _ask_provider(provider, snapshot.payment_intent_id)
        except PaymentProviderError as e:
            logger.warning(
                "Reconciliation: payment %s (%s) could not be compared: %s",
                payment_id, snapshot.payment_intent_id, e,
            )
            result.errors += 1
            result.error_payment_ids.append(payment_id)
            consecutive_errors += 1
            if consecutive_errors >= settings.RECONCILIATION_MAX_CONSECUTIVE_ERRORS:
                result.aborted = True
                break
            continue
        consecutive_errors = 0
        opened, resolved = record(db, payment_id, compare(snapshot, intent, refunds), now)
        result.checked += 1
        result.opened += opened
        result.resolved += resolved
    db.commit()  # nothing is left open when the last payment was skipped
    logger.info("Reconciliation finished: %s", result.as_dict())
    return result


def open_discrepancies(
    db: Session, *, page: int, size: int, order_id: int | None = None
) -> tuple[list[tuple[PaymentDiscrepancy, Payment]], int]:
    """A page of the unresolved discrepancies, oldest first, each with its
    payment, and how many there are in all."""
    query = (
        select(PaymentDiscrepancy, Payment)
        .join(Payment, Payment.id == PaymentDiscrepancy.payment_id)
        .where(PaymentDiscrepancy.resolved_at.is_(None))
    )
    if order_id is not None:
        query = query.where(Payment.order_id == order_id)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(
        query.order_by(PaymentDiscrepancy.detected_at, PaymentDiscrepancy.id)
        .offset((page - 1) * size)
        .limit(size)
    ).all()
    return rows, total
