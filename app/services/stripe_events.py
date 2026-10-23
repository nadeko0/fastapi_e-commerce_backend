"""
What a Stripe webhook event does to the shop.

Plain functions (no Celery, no FastAPI), so they can be tested, and
mutation-tested, on their own. The webhook route verifies, stores and
enqueues an event; the process_stripe_event task (app/tasks.py) loads the
stored event and calls apply_event. The payment helpers that /pay and
/refund share (the order lock, the success and failure transitions, the
manual-review flag, the refund ledger) stay in app/api/v1/orders.py and are
imported from there.
"""
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.v1.orders import (
    _MANUAL_REVIEW_RESPONSE,
    _PENDING_INTENT_ID_PREFIX,
    _REFUND_STATUSES_NOT_RETURNED,
    _apply_payment_failure,
    _apply_payment_success,
    _flag_payment_for_manual_review,
    _record_refund,
    _reported_charge_matches_order,
    _settle_refunds,
)
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
from app.models.payment_ledger import DISPUTE_CLOSED_STATUSES, PaymentDispute, PaymentRefund

logger = logging.getLogger(__name__)


class PaymentAttemptNotRecorded(Exception):
    """The event overtook its payment attempt: one of our orders has an
    attempt still waiting for the provider's answer, so the PaymentIntent or
    Checkout Session the event is about is not stored yet. Nothing was
    applied; the event must be tried again later."""


def _attempt_still_storing_its_id(db: Session, object_data: dict) -> bool:
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
        order_id = int(metadata.get("order_id"))
    except (TypeError, ValueError):
        return False
    if not 0 < order_id < 2 ** 31:
        return False
    started_after = utc_now() - timedelta(
        minutes=settings.PAYMENT_ATTEMPT_STALE_AFTER_MINUTES
    )
    return db.query(Payment.id).filter(
        Payment.order_id == order_id,
        Payment.status.in_(("processing", "unknown")),
        Payment.checkout_session_id.is_(None),
        Payment.payment_intent_id.like(f"{_PENDING_INTENT_ID_PREFIX}%"),
        Payment.created_at > started_after,
    ).first() is not None


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


def _apply_dispute_event(db: Session, object_data: dict) -> dict:
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
        db.query(Payment).filter(Payment.payment_intent_id == payment_intent_id).first()
        if payment_intent_id
        else None
    )
    if payment_row is None or not isinstance(dispute_id, str) or not isinstance(status, str):
        db.commit()
        return {"status": "processed", "applied": False}

    order = db.query(Order).filter(Order.id == payment_row.order_id).with_for_update().first()
    db.refresh(payment_row)
    dispute = db.query(PaymentDispute).filter(PaymentDispute.dispute_id == dispute_id).first()
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
    dispute.status = status
    is_closed = status in DISPUTE_CLOSED_STATUSES
    if is_closed:
        dispute.closed_at = utc_now()
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


def _object_id(value) -> Optional[str]:
    """An object reference in an event payload: an id string, or the
    expanded object carrying one."""
    if isinstance(value, dict):
        value = value.get("id")
    return value if isinstance(value, str) and value else None


# Events about a refund of a charge: the Refund itself (refund.*), or the
# Charge it refunded, which lists its refunds only in older API versions
# or when expanded - Stripe: "Listen to refund.created for information
# about the refund".
_REFUND_EVENT_TYPES = ("refund.created", "refund.updated", "refund.failed", "charge.refunded")


def _apply_refund_event(db: Session, event_type: str, object_data: dict) -> dict:
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
        db.query(Payment).filter(Payment.payment_intent_id == payment_intent_id).first()
        if payment_intent_id
        else None
    )
    if payment_row is None or not refund_objects:
        db.commit()
        return {"status": "processed", "applied": False}

    order = db.query(Order).filter(Order.id == payment_row.order_id).with_for_update().first()
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


def _apply_refund_object(db: Session, payment_row: Payment, refund_object: dict) -> bool:
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
    refund_row = db.query(PaymentRefund).filter(PaymentRefund.refund_id == refund_id).first()
    if refund_row is None:
        # A refund this app has not recorded: made in the Stripe Dashboard
        # (or by another integration), or one of ours whose event overtook
        # the /refund request still waiting for the provider's answer -
        # that request then finds it recorded and does not count it again.
        # Refunds made outside the app used to be ignored altogether: the
        # order stayed paid for money already returned.
        amount = refund_object.get("amount")
        if isinstance(amount, int) and not isinstance(amount, bool) and amount > 0:
            _record_refund(db, payment_row, refund_id, amount, status, source="provider")
        return False
    if refund_row.status in _REFUND_STATUSES_NOT_RETURNED:
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


def apply_event(db: Session, event_type: str, event_data: Any, background_tasks) -> dict:
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
    is_checkout_session_event = event_type.startswith("checkout.session.")

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

    # Locked for the rest of this event's handling (every branch ends in a
    # commit), and the attempt re-read under the lock: a concurrent /pay or
    # another event for the same order waits instead of deciding on the
    # same state. See _lock_order_and_reload.
    order = db.query(Order).filter(Order.id == payment_row.order_id).with_for_update().first()
    db.refresh(payment_row)

    if event_type == "payment_intent.succeeded":
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
    elif event_type in ("payment_intent.payment_failed", "payment_intent.canceled"):
        # A canceled PaymentIntent (from the Dashboard, by the integration,
        # or by Stripe after an abandoned authentication) can never be
        # charged: the attempt is over, like a failed one. It used to be
        # unhandled, so the attempt stayed "requires_action" and blocked
        # every new attempt for the order while it was recent
        # (_guard_no_attempt_in_flight). A succeeded intent cannot be
        # canceled, and _apply_payment_failure leaves a succeeded attempt
        # alone in any case.
        _apply_payment_failure(db, order, payment_row)
    elif event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
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

        # Same conflict handling as payment_intent.succeeded above.
        if payment_row.status != "succeeded" and not _reported_charge_matches_order(
            object_data, "amount_total", order
        ):
            return _flag_payment_for_manual_review(
                db, payment_row, "charged amount or currency differs from the order"
            )

        if not _apply_payment_success(db, order, payment_row, background_tasks, order.user.email):
            return _MANUAL_REVIEW_RESPONSE
    elif event_type in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        # Mirrors payment_intent.payment_failed's handling: a terminal
        # failure/expiry for this attempt. See _apply_payment_failure for
        # why it never touches an attempt that succeeded or a paid order.
        _apply_payment_failure(db, order, payment_row)
    else:
        # A type with no handling of its own (payment_intent.created,
        # .processing, ...: Stripe sends them to an endpoint subscribed to
        # them). Recorded, and answered as not applied - it used to be
        # answered "applied" like a payment that was.
        db.commit()
        return {"status": "processed", "applied": False}

    return {"status": "processed", "applied": True}


# -- Storing, publishing, processing and sweeping stored events ------------------

# Characters of an error kept in WebhookEvent.last_error.
_LAST_ERROR_MAX_CHARS = 2000


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
    except Exception:
        logger.warning(
            "Could not enqueue Stripe event %s; it stays stored and the sweeper will enqueue it",
            event_id, exc_info=True,
        )
        return False
    return True


def retry_delay_seconds(attempts: int) -> int:
    """Seconds to wait before retrying an event that has failed `attempts`
    times: STRIPE_EVENT_RETRY_BASE_SECONDS, doubling each time, up to
    STRIPE_EVENT_RETRY_MAX_SECONDS."""
    return min(
        settings.STRIPE_EVENT_RETRY_BASE_SECONDS * 2 ** (attempts - 1),
        settings.STRIPE_EVENT_RETRY_MAX_SECONDS,
    )


@dataclass
class EventOutcome:
    """What one run of the worker did with an event. state is "processed"
    (result says what it did), "skipped" (not ours to do: gone, locked by
    another worker, already processed or failed), "retrying" (retry_in
    seconds from now) or "failed" (out of retries)."""

    state: str
    result: Optional[dict] = None
    retry_in: Optional[int] = None


class _AfterCommit:
    """Stands in for FastAPI's BackgroundTasks in the worker: what the
    payment code queues (the customer's email) runs once the event is
    committed. A failure then must not undo or retry an event that has been
    applied."""

    def __init__(self) -> None:
        self._calls: list = []

    def add_task(self, func, *args, **kwargs) -> None:
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


def _claim_event(db: Session, event_id: str) -> Optional[WebhookEvent]:
    """The stored event, row-locked for the rest of the transaction.
    SKIP LOCKED: an event another worker is applying right now is not
    returned (None), so two workers given the same message apply it once -
    the second leaves it to the first."""
    return (
        db.query(WebhookEvent)
        .filter(WebhookEvent.event_id == event_id)
        .with_for_update(skip_locked=True)
        .first()
    )


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
        data = json.loads(row.payload).get("data")
        row.status = WEBHOOK_EVENT_PROCESSED
        row.processed_at = utc_now()
        result = apply_event(db, row.event_type, data, after_commit)
        db.commit()
    except Exception as error:
        db.rollback()
        return _record_failure(db, event_id, error)
    after_commit.run(event_id)
    return EventOutcome("processed", result)


def _record_failure(db: Session, event_id: str, error: Exception) -> EventOutcome:
    """Count a failed attempt in a new transaction (the failed one was rolled
    back, event and state changes together): retrying with a backoff, or
    failed once STRIPE_EVENT_MAX_RETRIES retries are used up."""
    row = db.query(WebhookEvent).filter(WebhookEvent.event_id == event_id).with_for_update().one()
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
    The sweeper: publish the events that are still received or retrying
    STRIPE_EVENT_SWEEP_AFTER_MINUTES after they last changed - the message
    to the worker was never published (the broker was down when the route
    ran) or was lost. Oldest first, at most STRIPE_EVENT_SWEEP_BATCH. The
    events are not locked while they are published (a worker given the
    message would skip a locked row); each published event's updated_at is
    set to now instead, so the next run leaves it for the worker. Stops at
    the first event the broker refuses. Returns how many were published.
    """
    now = utc_now()
    stuck = (
        db.query(WebhookEvent.id, WebhookEvent.event_id)
        .filter(
            WebhookEvent.status.in_((WEBHOOK_EVENT_RECEIVED, WEBHOOK_EVENT_RETRYING)),
            WebhookEvent.updated_at < now - timedelta(minutes=settings.STRIPE_EVENT_SWEEP_AFTER_MINUTES),
        )
        .order_by(WebhookEvent.updated_at, WebhookEvent.id)
        .limit(settings.STRIPE_EVENT_SWEEP_BATCH)
        .all()
    )
    published = []
    for row_id, event_id in stuck:
        if not enqueue_event(event_id):
            break
        published.append(row_id)
    if published:
        db.query(WebhookEvent).filter(
            WebhookEvent.id.in_(published),
            WebhookEvent.status.in_((WEBHOOK_EVENT_RECEIVED, WEBHOOK_EVENT_RETRYING)),
        ).update({WebhookEvent.updated_at: now}, synchronize_session=False)
    db.commit()
    if published:
        logger.info("Enqueued %s stuck Stripe event(s)", len(published))
    return len(published)
