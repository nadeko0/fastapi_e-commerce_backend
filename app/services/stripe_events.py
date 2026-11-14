"""
What a Stripe webhook event does to the shop.

Plain functions (no Celery, no FastAPI), so they can be tested, and
mutation-tested, on their own. The webhook route verifies, stores and
enqueues an event; the process_stripe_event task (app/tasks.py) loads the
stored event and calls apply_event. The payment helpers that /pay and
/refund share (the order lock, the success and failure transitions, the
manual-review flag, the refund ledger) live in app/services/payment/
(order_state, refund_ledger, attempts).
"""
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.core.config import settings
from app.models.order import Order
from app.models.payment import (
    WEBHOOK_EVENT_FAILED,
    WEBHOOK_EVENT_PROCESSED,
    WEBHOOK_EVENT_RECEIVED,
    WEBHOOK_EVENT_RETRYING,
    Payment,
    WebhookEvent,
)
from app.models.payment_ledger import DISPUTE_CLOSED_STATUSES, PaymentDispute
from app.services.payment.attempts import (
    _PENDING_CHECKOUT_ID_PREFIX,
    _PENDING_INTENT_ID_PREFIX,
    _is_checkout_row,
)
from app.services.payment.order_state import (
    _MANUAL_REVIEW_RESPONSE,
    TaskQueue,
    _apply_payment_failure,
    _apply_payment_success,
    _flag_payment_for_manual_review,
    _reported_charge_matches_order,
)
from app.services.payment.refund_ledger import (
    _apply_refund_object,
    _settle_refunds,
)
from app.services.stripe_event_redaction import _object_id, _redact_if_owner_erased

logger = logging.getLogger(__name__)


class PaymentAttemptNotRecorded(Exception):
    """The event overtook its payment attempt: one of our orders has an
    attempt still waiting for the provider's answer, so the PaymentIntent or
    Checkout Session the event is about is not stored yet. Nothing was
    applied; the event must be tried again later."""


def _attempt_still_storing_its_id(db: Session, object_data: dict[str, Any]) -> bool:
    """
    Whether an event that matched no Payment row is nevertheless for an
    attempt of ours that has not stored its provider id yet: the object's
    metadata names one of our orders (both flows send order_id), and that
    order has an attempt still in flight (processing, recent) carrying
    only the "pending-..." placeholder, with no Checkout Session id. The
    metadata alone is not enough - another deployment sharing the Stripe
    account numbers its orders the same way - and an attempt in flight for
    longer than PAYMENT_ATTEMPT_STALE_AFTER_MINUTES (its request died) does
    not hold events back.
    """
    metadata = object_data.get("metadata")
    if not isinstance(metadata, dict):
        return False
    try:
        order_id = int(metadata.get("order_id"))  # type: ignore[arg-type]  # None raises TypeError, handled here
    except (TypeError, ValueError):
        return False
    if not 0 < order_id < 2 ** 31:
        return False
    started_after = utc_now() - timedelta(
        minutes=settings.PAYMENT_ATTEMPT_STALE_AFTER_MINUTES
    )
    return db.scalars(
        select(Payment.id)
        .where(
            Payment.order_id == order_id,
            Payment.status.in_(("processing", "unknown")),
            Payment.checkout_session_id.is_(None),
            Payment.payment_intent_id.like(f"{_PENDING_INTENT_ID_PREFIX}%"),
            Payment.created_at > started_after,
        )
        .limit(1)
    ).first() is not None


def _checkout_attempt_named_by_metadata(db: Session, object_data: dict[str, Any]) -> Payment | None:
    """
    The Checkout Session attempt a payment_intent.* event is about, for an
    event whose PaymentIntent id is not stored on any Payment row. Such an
    attempt holds the "pending-cs-..." placeholder until its
    checkout.session.completed event arrives, and that event may be late or
    lost. create_checkout_session copies {order_id, payment_id} onto the
    session's PaymentIntent (payment_intent_data.metadata), so the event
    names its own attempt. Both ids must agree with one row that still holds
    the placeholder: metadata alone is not trusted (another deployment on the
    same Stripe account numbers its rows the same way), and a Session created
    before this metadata was sent carries none, so it is not found here.
    """
    metadata = object_data.get("metadata")
    if not isinstance(metadata, dict):
        return None
    try:
        order_id = int(metadata.get("order_id"))  # type: ignore[arg-type]  # None raises TypeError, handled here
        payment_id = int(metadata.get("payment_id"))  # type: ignore[arg-type]  # as above
    except (TypeError, ValueError):
        return None
    if not (0 < order_id < 2 ** 31 and 0 < payment_id < 2 ** 31):
        return None
    return db.scalars(
        select(Payment)
        .where(
            Payment.id == payment_id,
            Payment.order_id == order_id,
            Payment.payment_intent_id.like(f"{_PENDING_CHECKOUT_ID_PREFIX}%"),
        )
        .limit(1)
    ).first()


# charge.dispute.*: a chargeback or inquiry (Dispute object: id, amount,
# currency, payment_intent, status, reason). Only the status matters here;
# the funds_* events report the balance moving and carry it as well.
_DISPUTE_EVENT_TYPES = (
    "charge.dispute.created",
    "charge.dispute.updated",
    "charge.dispute.closed",
    "charge.dispute.funds_withdrawn",
    "charge.dispute.funds_reinstated",
)


def _apply_dispute_event(db: Session, object_data: dict[str, Any]) -> dict[str, Any]:
    """
    Record a dispute of one of our charges (PaymentDispute, by dispute id)
    under the order's lock. While it is open the order is on hold (see
    _has_open_dispute: no fulfilment, no refund); its status and
    payment_status are not changed silently, it is flagged for review.
    Once closed it is final - a late, out-of-order event for it is
    acknowledged and ignored - and a lost one refunds the order
    (_settle_refunds). Following WooCommerce's Stripe gateway, mapped onto
    this order model, which has no "on hold" status of its own.
    """
    dispute_id = object_data.get("id")
    status = object_data.get("status")
    payment_intent_id = _object_id(object_data.get("payment_intent"))
    payment_row = (
        db.scalars(
            select(Payment).where(Payment.payment_intent_id == payment_intent_id).limit(1)
        ).first()
        if payment_intent_id
        else None
    )
    if payment_row is None or not isinstance(dispute_id, str) or not isinstance(status, str):
        db.commit()
        return {"status": "processed", "applied": False}

    order = db.scalars(
        select(Order).where(Order.id == payment_row.order_id).with_for_update().limit(1)
    ).first()
    assert order is not None, "payments.order_id is a non-null foreign key to orders"
    db.refresh(payment_row)
    dispute = db.scalars(
        select(PaymentDispute).where(PaymentDispute.dispute_id == dispute_id).limit(1)
    ).first()
    if dispute is not None and dispute.status in DISPUTE_CLOSED_STATUSES:
        db.commit()
        return {"status": "processed", "applied": False}
    if dispute is None:
        amount = object_data.get("amount")
        reason = object_data.get("reason")
        dispute = PaymentDispute(
            payment_id=payment_row.id,
            dispute_id=dispute_id,
            amount=amount if isinstance(amount, int) and not isinstance(amount, bool) else 0,
            currency=payment_row.currency,
            reason=reason if isinstance(reason, str) else None,
        )
        db.add(dispute)
    # created and funds_withdrawn (and updated) report the same status for
    # one dispute: logged when the dispute is first seen or its status moves.
    status_changed = dispute.status is None or dispute.status != status  # type: ignore[redundant-expr]  # None on a dispute just created above
    dispute.status = status
    for column, value in _evidence_details(object_data).items():
        setattr(dispute, column, value)
    is_closed = status in DISPUTE_CLOSED_STATUSES
    if is_closed:
        dispute.closed_at = utc_now()
    if status_changed:
        logger.warning(
            "Dispute %s of payment %s (order %s) is %s%s",
            dispute_id, payment_row.id, order.id, status,
            "" if is_closed else ": order on hold, needs review",
        )
    _settle_refunds(db, order, payment_row)
    order.updated_at = utc_now()
    db.commit()
    if is_closed:
        return {"status": "processed", "applied": True}
    return {"status": "processed", "applied": True, "requires_manual_review": True}


def _evidence_details(object_data: dict[str, Any]) -> dict[str, Any]:
    """The PaymentDispute columns from a Dispute's evidence_details; empty
    when the event carries none (the stored values stay). A field of the
    wrong type is stored as None (not reported)."""
    details = object_data.get("evidence_details")
    if not isinstance(details, dict):
        return {}

    def _flag(name: str) -> bool | None:
        value = details.get(name)
        return value if isinstance(value, bool) else None

    due_by = details.get("due_by")
    count = details.get("submission_count")
    method = details.get("submission_method")
    return {
        "evidence_due_by": (
            datetime.fromtimestamp(due_by, UTC)
            if isinstance(due_by, int) and not isinstance(due_by, bool)
            else None
        ),
        "evidence_has_evidence": _flag("has_evidence"),
        "evidence_past_due": _flag("past_due"),
        "evidence_submission_count": (
            count if isinstance(count, int) and not isinstance(count, bool) else None
        ),
        "evidence_submission_method": method if isinstance(method, str) else None,
    }


# Events about a refund of a charge: the Refund itself (refund.*), or the
# Charge it refunded, which lists its refunds only in older API versions
# or when expanded - Stripe: "Listen to refund.created for information
# about the refund".
_REFUND_EVENT_TYPES = ("refund.created", "refund.updated", "refund.failed", "charge.refunded")


def _apply_refund_event(db: Session, event_type: str, object_data: dict[str, Any]) -> dict[str, Any]:
    """
    A refund reported by the provider, applied to the charge's Payment row
    (found by the PaymentIntent it refunds) under the order's lock. A
    refund already recorded - by /refund or by an earlier event - is
    brought up to date; its amount is counted once.
    """
    if event_type == "charge.refunded":
        listed = object_data.get("refunds")
        refund_objects = listed.get("data") if isinstance(listed, dict) else None
        refund_objects = [r for r in refund_objects or [] if isinstance(r, dict)]
    else:
        refund_objects = [object_data]
    payment_intent_id = _object_id(object_data.get("payment_intent"))
    payment_row = (
        db.scalars(
            select(Payment).where(Payment.payment_intent_id == payment_intent_id).limit(1)
        ).first()
        if payment_intent_id
        else None
    )
    if payment_row is None or not refund_objects:
        db.commit()
        return {"status": "processed", "applied": False}

    order = db.scalars(
        select(Order).where(Order.id == payment_row.order_id).with_for_update().limit(1)
    ).first()
    assert order is not None, "payments.order_id is a non-null foreign key to orders"
    db.refresh(payment_row)
    flagged = False
    for refund_object in refund_objects:
        flagged = _apply_refund_object(db, payment_row, refund_object) or flagged
    _settle_refunds(db, order, payment_row)
    order.updated_at = utc_now()
    db.commit()
    if flagged:
        return {"status": "processed", "applied": True, "requires_manual_review": True}
    return {"status": "processed", "applied": True}


def _find_payment_row(db: Session, event_type: str, object_data: dict[str, Any]) -> Payment | None:
    if event_type.startswith("checkout.session."):
        # Session events key off Payment.checkout_session_id, not
        # payment_intent_id - a fresh Checkout Session's payment_intent is
        # null until the customer actually completes the hosted page (see
        # CheckoutSession's docstring in app/services/payment/types.py), so
        # it cannot be used to find the Payment row at all event types
        # (e.g. checkout.session.expired never gets one).
        return (
            db.scalars(
                select(Payment).where(Payment.checkout_session_id == object_data.get("id")).limit(1)
            ).first()
            if object_data.get("id")
            else None
        )
    payment_intent_id = object_data.get("id")
    payment_row = (
        db.scalars(
            select(Payment).where(Payment.payment_intent_id == payment_intent_id).limit(1)
        ).first()
        if payment_intent_id
        else None
    )
    if payment_row is None and event_type.startswith("payment_intent."):
        payment_row = _checkout_attempt_named_by_metadata(db, object_data)
    return payment_row


def _apply_intent_succeeded(
    db: Session, event_type: str, order: Order, payment_row: Payment,
    object_data: dict[str, Any], background_tasks: TaskQueue, event_id: str | None,
) -> dict[str, Any]:
    # A Checkout attempt found through its metadata still holds the
    # placeholder: store the real PaymentIntent id, as
    # checkout.session.completed does (re-read under the lock in apply_event,
    # so a session event that got there first has already stored it).
    # Whichever of the two events runs second finds the attempt succeeded and
    # changes nothing (_apply_payment_success).
    intent_id = object_data.get("id")
    if payment_row.payment_intent_id.startswith(_PENDING_CHECKOUT_ID_PREFIX) and (
        isinstance(intent_id, str) and intent_id
    ):
        payment_row.payment_intent_id = intent_id
    # A success for a cancelled order, or for one another attempt
    # already paid, is flagged rather than applied inside
    # _apply_payment_success (the same rule for /pay's own outcome).
    if payment_row.status != "succeeded" and not _reported_charge_matches_order(
        object_data, "amount_received", order
    ):
        # Stripe charged something other than what the order costs (or
        # in another currency): marking the order paid would ship it
        # for the wrong money. Recorded and flagged for manual review.
        # Only for an attempt not yet known to have succeeded: a report
        # about one already applied changes nothing (see
        # _apply_payment_success). Checked first, this used to flag the
        # attempt that had paid the order, leaving the order paid with
        # no unflagged charge behind it - and impossible to refund.
        return _flag_payment_for_manual_review(
            db, payment_row, "charged amount or currency differs from the order"
        )

    if not _apply_payment_success(db, order, payment_row, background_tasks, order.user.email):
        return _MANUAL_REVIEW_RESPONSE
    return {"status": "processed", "applied": True}


def _apply_intent_failed(
    db: Session, event_type: str, order: Order, payment_row: Payment,
    object_data: dict[str, Any], background_tasks: TaskQueue, event_id: str | None,
) -> dict[str, Any]:
    if not _is_checkout_row(payment_row):
        return _apply_attempt_ended(
            db, event_type, order, payment_row, object_data, background_tasks, event_id
        )
    # A decline on Stripe's hosted page is not the end of the attempt:
    # the customer can retry on the same open Session, and the Session's
    # own events (completed, expired) end it. Marking the attempt and the
    # order failed here would be wrong, so it is only recorded in the log.
    error = object_data.get("last_payment_error")
    error = error if isinstance(error, dict) else {}
    logger.warning(
        "Checkout payment attempt %s (order %s) was declined on the hosted page "
        "(event %s, code %s, decline code %s); the session stays open for a retry",
        payment_row.id, order.id, event_id, error.get("code"), error.get("decline_code"),
    )
    db.commit()
    return {"status": "processed", "applied": False}


def _apply_attempt_ended(
    db: Session, event_type: str, order: Order, payment_row: Payment,
    object_data: dict[str, Any], background_tasks: TaskQueue, event_id: str | None,
) -> dict[str, Any]:
    # A terminal failure or expiry of the attempt: a failed PaymentIntent
    # (outside Checkout), an expired or failed Checkout Session, or a
    # canceled PaymentIntent. A canceled one (from the Dashboard, by the
    # integration, or by Stripe after an abandoned authentication) can never
    # be charged: the attempt is over, like a failed one. It used to be
    # unhandled, so the attempt stayed "requires_action" and blocked every
    # new attempt for the order while it was recent
    # (_guard_no_attempt_in_flight). A succeeded intent cannot be canceled,
    # and _apply_payment_failure never touches an attempt that succeeded or
    # a paid order.
    _apply_payment_failure(db, order, payment_row)
    return {"status": "processed", "applied": True}


def _apply_session_paid(
    db: Session, event_type: str, order: Order, payment_row: Payment,
    object_data: dict[str, Any], background_tasks: TaskQueue, event_id: str | None,
) -> dict[str, Any]:
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
    if event_type == "checkout.session.completed" and payment_status_raw not in (
        "paid",
        "no_payment_required",
    ):
        db.commit()
        return {"status": "processed", "applied": False}

    # Same conflict handling as payment_intent.succeeded (_apply_intent_succeeded).
    if payment_row.status != "succeeded" and not _reported_charge_matches_order(
        object_data, "amount_total", order
    ):
        return _flag_payment_for_manual_review(
            db, payment_row, "charged amount or currency differs from the order"
        )

    if not _apply_payment_success(db, order, payment_row, background_tasks, order.user.email):
        return _MANUAL_REVIEW_RESPONSE
    return {"status": "processed", "applied": True}


_PAYMENT_EVENT_HANDLERS: dict[
    str,
    Callable[
        [Session, str, Order, Payment, dict[str, Any], TaskQueue, str | None], dict[str, Any]
    ],
] = {
    "payment_intent.succeeded": _apply_intent_succeeded,
    "payment_intent.payment_failed": _apply_intent_failed,
    "payment_intent.canceled": _apply_attempt_ended,
    "checkout.session.completed": _apply_session_paid,
    "checkout.session.async_payment_succeeded": _apply_session_paid,
    "checkout.session.expired": _apply_attempt_ended,
    "checkout.session.async_payment_failed": _apply_attempt_ended,
}


def apply_event(
    db: Session, event_type: str, event_data: Any, background_tasks: TaskQueue, event_id: str | None = None
) -> dict[str, Any]:
    """
    Apply one verified event to the order/payment state and commit. The
    caller's transaction may already hold what must be committed together
    with the change (the stored event's processed mark): every path below
    that applies, or decides there is nothing to apply, ends in a commit,
    which keeps both.

    `event_data` is the event's "data" member. `background_tasks.add_task(fn, *args)` queues
    what must run once the change is committed (the customer's email).
    Returns what happened, as {"status": "processed", "applied": ...}.

    Raises PaymentAttemptNotRecorded, after rolling back, for an event that
    overtook its payment attempt.
    """
    object_data = event_data.get("object", {}) if isinstance(event_data, dict) else {}
    if event_type in _REFUND_EVENT_TYPES:
        return _apply_refund_event(db, event_type, object_data)
    if event_type in _DISPUTE_EVENT_TYPES:
        return _apply_dispute_event(db, object_data)

    payment_row = _find_payment_row(db, event_type, object_data)
    if payment_row is None:
        if _attempt_still_storing_its_id(db, object_data):
            # One of our orders has an attempt that has not stored its
            # PaymentIntent/Checkout Session id yet: this event overtook the
            # request still waiting on the provider's answer. Acknowledged,
            # it used to be dropped as "not ours". It is not applied and not
            # recorded as done (the rollback discards the caller's pending
            # changes): it is tried again later, by when the id is stored.
            db.rollback()
            raise PaymentAttemptNotRecorded(
                "The payment attempt for this event is not recorded yet"
            )
        # Unknown/foreign payment_intent or checkout.session (e.g. a
        # webhook for a test event sent from the Stripe dashboard, an
        # object another integration on the account created, or one that
        # predates this payment) - recorded and acknowledged so Stripe
        # stops retrying, but there is nothing to apply.
        db.commit()
        return {"status": "processed", "applied": False}

    # Locked for the rest of this event's handling (every handler ends in a
    # commit), and the attempt re-read under the lock: a concurrent /pay or
    # another event for the same order waits instead of deciding on the
    # same state. See _lock_order_and_reload.
    order = db.scalars(
        select(Order).where(Order.id == payment_row.order_id).with_for_update().limit(1)
    ).first()
    assert order is not None, "payments.order_id is a non-null foreign key to orders"
    db.refresh(payment_row)

    handler = _PAYMENT_EVENT_HANDLERS.get(event_type)
    if handler is None:
        # A type with no handling of its own (payment_intent.created,
        # .processing, ...: Stripe sends them to an endpoint subscribed to
        # them). Recorded, and answered as not applied - it used to be
        # answered "applied" like a payment that was.
        db.commit()
        return {"status": "processed", "applied": False}
    return handler(db, event_type, order, payment_row, object_data, background_tasks, event_id)


# -- Storing, publishing, processing and sweeping stored events ------------------

# Characters of an error kept in WebhookEvent.last_error.
_LAST_ERROR_MAX_CHARS = 2000

# last_error of a received event whose task could not be published (the
# broker was down). The sweeper publishes these on its next run instead of
# waiting STRIPE_EVENT_SWEEP_AFTER_MINUTES, the wait meant for retries; an
# admin listing stuck events sees why.
NOT_ENQUEUED = "Not enqueued: the broker could not be reached; the sweeper publishes it"


def store_event(db: Session, event_id: str, event_type: str, payload: str) -> bool:
    """
    Store a verified event as "received" and commit. False when an event
    with this id is already stored: the unique constraint on event_id is
    the idempotency guard (Stripe delivers at-least-once, and a duplicate
    can arrive while the first is still being stored - its INSERT waits on
    the unique index, then fails). Any other database error propagates: the
    route answers 5xx and Stripe delivers the event again, nothing is lost.
    """
    db.add(WebhookEvent(
        event_id=event_id,
        event_type=event_type,
        payload=payload,
        status=WEBHOOK_EVENT_RECEIVED,
        attempts=0,
    ))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


def enqueue_event(event_id: str) -> bool:
    """
    Publish the process_stripe_event task for a stored event. False, with a
    warning, when the broker cannot be reached: the event is stored, so
    nothing is lost - the sweeper (sweep_stripe_events) publishes events
    that stay received or retrying - and the caller still answers 2xx.
    """
    # Imported here: app.tasks imports this module.
    from app import tasks

    try:
        # No publish retries: the caller is a request Stripe is waiting on.
        tasks.process_stripe_event.apply_async(args=[event_id], retry=False)
    except Exception as exc:  # noqa: BLE001 - the broker can fail in any way; the sweeper re-queues the event
        logger.warning(
            "Could not enqueue Stripe event %s: %s: %s; the sweeper re-queues it",
            event_id,
            type(exc).__name__,
            exc,
        )
        return False
    return True


def mark_not_enqueued(db: Session, event_id: str) -> None:
    """Record that a received event's task could not be published
    (enqueue_event returned False), so the sweeper's next run publishes it."""
    db.execute(
        update(WebhookEvent)
        .where(WebhookEvent.event_id == event_id, WebhookEvent.status == WEBHOOK_EVENT_RECEIVED)
        .values(last_error=NOT_ENQUEUED)
        .execution_options(synchronize_session=False)
    )
    db.commit()


def retry_delay_seconds(attempts: int) -> int:
    """Seconds to wait before retrying an event that has failed `attempts`
    times: STRIPE_EVENT_RETRY_BASE_SECONDS, doubling each time, up to
    STRIPE_EVENT_RETRY_MAX_SECONDS."""
    # `2 ** int` is typed Any (a negative exponent gives a float); attempts >= 1.
    delay: int = settings.STRIPE_EVENT_RETRY_BASE_SECONDS * 2 ** (attempts - 1)
    return min(delay, settings.STRIPE_EVENT_RETRY_MAX_SECONDS)


@dataclass
class EventOutcome:
    """What one run of the worker did with an event. state is "processed"
    (result says what it did), "skipped" (not ours to do: gone, locked by
    another worker, already processed or failed), "retrying" (retry_in
    seconds from now) or "failed" (out of retries)."""

    state: str
    result: dict[str, Any] | None = None
    retry_in: int | None = None


class _AfterCommit:
    """Stands in for FastAPI's BackgroundTasks in the worker: what the
    payment code queues (the customer's email) runs once the event is
    committed. A failure then must not undo or retry an event that has been
    applied."""

    def __init__(self) -> None:
        self._calls: list[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = []

    def add_task(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        self._calls.append((func, args, kwargs))

    def run(self, event_id: str) -> None:
        for func, args, kwargs in self._calls:
            try:
                func(*args, **kwargs)
            except Exception:
                logger.exception(
                    "Stripe event %s was applied, but its follow-up %s failed",
                    event_id, getattr(func, "__name__", func),
                )


def _claim_event(db: Session, event_id: str) -> WebhookEvent | None:
    """The stored event, row-locked for the rest of the transaction.
    SKIP LOCKED: an event another worker is applying right now is not
    returned (None), so two workers given the same message apply it once -
    the second leaves it to the first."""
    return db.scalars(
        select(WebhookEvent)
        .where(WebhookEvent.event_id == event_id)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).first()


def process_stored_event(db: Session, event_id: str) -> EventOutcome:
    """
    The body of the process_stripe_event task: apply a stored event.

    The event row is locked (_claim_event), marked processed, and applied;
    apply_event commits the mark together with the state changes it made
    (and rolls both back on an error), so an event is never recorded as
    processed without what it changed. An event that is not received or
    retrying is left alone. Anything that stops the event from being
    applied - an event that overtook its payment attempt
    (PaymentAttemptNotRecorded) or any other error - is recorded and
    answered with a retry (_record_failure).
    """
    # As the request sessions are (app/core/database.py): objects keep their
    # values after a commit instead of being re-read, unlocked, for the
    # customer's email. The payment helpers (_lock_order_and_reload) are
    # written for it.
    db.expire_on_commit = False
    row = _claim_event(db, event_id)
    if row is None or row.status not in (WEBHOOK_EVENT_RECEIVED, WEBHOOK_EVENT_RETRYING):
        db.rollback()
        return EventOutcome("skipped")
    after_commit = _AfterCommit()
    try:
        assert row.payload is not None, "unprocessed event always has a payload"
        data = json.loads(row.payload).get("data")
        _redact_if_owner_erased(db, row)
        row.status = WEBHOOK_EVENT_PROCESSED
        row.processed_at = utc_now()
        result = apply_event(db, row.event_type, data, after_commit, event_id=row.event_id)
        db.commit()
    except Exception as error:  # noqa: BLE001 - any failure is recorded as a failed attempt and retried
        db.rollback()
        return _record_failure(db, event_id, error)
    after_commit.run(event_id)
    return EventOutcome("processed", result)


def _record_failure(db: Session, event_id: str, error: Exception) -> EventOutcome:
    """Count a failed attempt in a new transaction (the failed one was rolled
    back, event and state changes together): retrying with a backoff, or
    failed once STRIPE_EVENT_MAX_RETRIES retries are used up."""
    row = db.scalars(
        select(WebhookEvent).where(WebhookEvent.event_id == event_id).with_for_update()
    ).one()
    if row.status == WEBHOOK_EVENT_PROCESSED:
        # The error came after the commit that applied the event: it is
        # done, and retrying would apply it twice.
        db.rollback()
        logger.error("Stripe event %s was applied, then raised: %r", event_id, error)
        return EventOutcome("skipped")
    row.attempts += 1
    row.last_error = f"{type(error).__name__}: {error}"[:_LAST_ERROR_MAX_CHARS]
    if row.attempts > settings.STRIPE_EVENT_MAX_RETRIES:
        row.status = WEBHOOK_EVENT_FAILED
        db.commit()
        logger.error(
            "Stripe event %s (%s) failed after %s attempts and is not retried any more; "
            "replay it from the admin once the cause is fixed",
            event_id, row.event_type, row.attempts, exc_info=error,
        )
        return EventOutcome("failed")
    row.status = WEBHOOK_EVENT_RETRYING
    db.commit()
    logger.warning(
        "Stripe event %s (%s) not applied (attempt %s), will retry: %r",
        event_id, row.event_type, row.attempts, error,
    )
    return EventOutcome("retrying", retry_in=retry_delay_seconds(row.attempts))


def enqueue_stuck_events(db: Session) -> int:
    """
    The sweeper: publish the received events marked NOT_ENQUEUED (the
    broker was down when the route ran), and the events that are still
    received or retrying STRIPE_EVENT_SWEEP_AFTER_MINUTES after they last
    changed (the message was lost). Only marked events are taken early: a
    received event without the mark may just be waiting in a long queue,
    and a second message for it would add work when the queue is longest.
    Oldest first, at most STRIPE_EVENT_SWEEP_BATCH. The events are not
    locked while they are published (a worker given the message would skip
    a locked row); each published event's updated_at is set to now and its
    mark cleared instead, so the next run leaves it for the worker. Stops at
    the first event the broker refuses. Returns how many were published.
    """
    now = utc_now()
    stuck = db.execute(
        select(WebhookEvent.id, WebhookEvent.event_id)
        .where(
            or_(
                and_(
                    WebhookEvent.status == WEBHOOK_EVENT_RECEIVED,
                    WebhookEvent.last_error == NOT_ENQUEUED,
                ),
                and_(
                    WebhookEvent.status.in_((WEBHOOK_EVENT_RECEIVED, WEBHOOK_EVENT_RETRYING)),
                    WebhookEvent.updated_at
                    < now - timedelta(minutes=settings.STRIPE_EVENT_SWEEP_AFTER_MINUTES),
                ),
            )
        )
        .order_by(WebhookEvent.updated_at, WebhookEvent.id)
        .limit(settings.STRIPE_EVENT_SWEEP_BATCH)
    ).all()
    published = []
    for row_id, event_id in stuck:
        if not enqueue_event(event_id):
            break
        published.append(row_id)
    if published:
        db.execute(
            update(WebhookEvent)
            .where(
                WebhookEvent.id.in_(published),
                WebhookEvent.status.in_((WEBHOOK_EVENT_RECEIVED, WEBHOOK_EVENT_RETRYING)),
            )
            .values({
                WebhookEvent.updated_at: now,
                WebhookEvent.last_error: case(
                    (WebhookEvent.last_error == NOT_ENQUEUED, None),
                    else_=WebhookEvent.last_error,
                ),
            })
            .execution_options(synchronize_session=False)
        )
    db.commit()
    if published:
        logger.info("Enqueued %s stuck Stripe event(s)", len(published))
    return len(published)
