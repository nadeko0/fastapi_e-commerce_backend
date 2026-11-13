"""
Erasing personal data from stored Stripe webhook events (GDPR Art. 17).

The anonymisation task (app/tasks.py) redacts the events stored before it
ran (redact_users_events); process_stored_event (app/services/stripe_events.py)
redacts an event stored later about an account whose erasure was requested
(_redact_if_owner_erased).
"""
import json
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.order import Order
from app.models.payment import Payment, WebhookEvent
from app.models.user import User
from app.services.payment.attempts import _PENDING_INTENT_ID_PREFIX


def _object_id(value: Any) -> str | None:
    """An object reference in an event payload: an id string, or the
    expanded object carrying one."""
    if isinstance(value, dict):
        value = value.get("id")
    return value if isinstance(value, str) and value else None



# What is kept of a stored event once its customer is erased: the ids that tie
# it to our payment rows, the amounts and states the handlers read, and the
# order id. Everything else a Stripe event can carry about a person
# (customer_details, billing_details, shipping, receipt_email, customer_email,
# payment_method_details, receipt_url, free-text descriptions ...) is dropped.
# An allow-list, not a deny-list: a field Stripe adds later is erased too.
_KEPT_OBJECT_FIELDS = (
    "id", "object", "payment_intent", "amount", "amount_total", "amount_received",
    "amount_refunded", "currency", "status", "payment_status", "reason",
    "failure_reason",
)
_KEPT_EVENT_FIELDS = ("id", "object", "type", "created", "api_version", "livemode")


def _redact_object(obj: dict[str, Any]) -> dict[str, Any]:
    kept = {key: obj[key] for key in _KEPT_OBJECT_FIELDS if key in obj}
    if "payment_intent" in kept:
        kept["payment_intent"] = _object_id(kept["payment_intent"])
    metadata = obj.get("metadata")
    if isinstance(metadata, dict) and "order_id" in metadata:
        # payment_id too: it is how a payment_intent.* event finds its Checkout attempt.
        kept["metadata"] = {
            key: metadata[key] for key in ("order_id", "payment_id") if key in metadata
        }
    refunds = obj.get("refunds")
    if isinstance(refunds, dict) and isinstance(refunds.get("data"), list):
        kept["refunds"] = {
            "data": [_redact_object(r) for r in refunds["data"] if isinstance(r, dict)]
        }
    return kept


def redact_event_payload(payload: str) -> str:
    """The payload with the personal data removed (see _KEPT_OBJECT_FIELDS),
    marked `"redacted": true`. Redacting a redacted payload changes nothing."""
    event = json.loads(payload)
    kept = {key: event[key] for key in _KEPT_EVENT_FIELDS if key in event}
    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    kept["data"] = {"object": _redact_object(obj) if isinstance(obj, dict) else {}}
    kept["redacted"] = True
    return json.dumps(kept)


def _event_references(payload: dict[str, Any]) -> set[str]:
    data = payload.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    if not isinstance(obj, dict):
        return set()
    refs = {obj.get("id"), _object_id(obj.get("payment_intent"))}
    metadata = obj.get("metadata")
    if isinstance(metadata, dict):
        refs.add(f"order:{metadata.get('order_id')}")
    return {ref for ref in refs if isinstance(ref, str)}


def _redact_if_owner_erased(db: Session, row: WebhookEvent) -> None:
    """
    Redact the stored payload of an event about a payment of an account
    whose erasure was requested (committed with the event's processing).
    The anonymisation task redacts the events stored before it ran, once
    (redact_users_events), and then skips the account for good: an event
    stored later - a redelivery, a refund or dispute months later, or one
    that raced the task - kept the customer's personal data forever.
    """
    try:
        payload = json.loads(row.payload)  # type: ignore[arg-type]  # None raises TypeError, handled here
    except (TypeError, ValueError):
        return
    if not isinstance(payload, dict) or payload.get("redacted"):
        return
    refs = _event_references(payload)
    # An order id outside the column's range names no order of ours (and
    # would overflow the query parameter), as in _order_from_metadata.
    order_ids = [
        int(r[6:]) for r in refs
        if r.startswith("order:") and r[6:].isdigit() and 0 < int(r[6:]) <= 2**31 - 1
    ]
    provider_ids = [r for r in refs if not r.startswith("order:")]
    about = []
    if order_ids:
        about.append(Order.id.in_(order_ids))
    if provider_ids:
        about.append(Payment.payment_intent_id.in_(provider_ids))
        about.append(Payment.checkout_session_id.in_(provider_ids))
    if not about:
        return
    erased = db.scalars(
        select(User.id)
        .join(Order, Order.user_id == User.id)
        .outerjoin(Payment, Payment.order_id == Order.id)
        .where(User.data_deletion_requested, or_(*about))
        .limit(1)
    ).first()
    if erased is not None:
        assert row.payload is not None, "json.loads above has parsed it"
        row.payload = redact_event_payload(row.payload)


def redact_users_events(db: Session, user_ids: list[int]) -> int:
    """
    Redacts the stored payload of every webhook event about these users'
    payments, and returns how many it changed. An event is about a user
    when its object is, or points (payment_intent) at, one of the
    PaymentIntents or Checkout Sessions of the user's orders, or carries one
    of those order ids in its metadata. The event row stays, with its id,
    type, status and timestamps, as the audit trail of what the shop did.
    Does not commit. Safe to repeat: already redacted payloads are skipped.
    """
    if not user_ids:
        return 0
    payments = db.execute(
        select(Payment.payment_intent_id, Payment.checkout_session_id, Payment.order_id)
        .join(Order, Order.id == Payment.order_id)
        .where(Order.user_id.in_(user_ids))
    ).all()
    wanted: set[str] = set()
    for intent_id, session_id, order_id in payments:
        if intent_id and not intent_id.startswith(_PENDING_INTENT_ID_PREFIX):
            wanted.add(intent_id)
        if session_id:
            wanted.add(session_id)
        wanted.add(f"order:{order_id}")
    if not wanted:
        return 0

    needles = [ref for ref in wanted if not ref.startswith("order:")]
    needles += [
        f'"order_id":{sep}"{ref[6:]}"' for ref in wanted if ref.startswith("order:")
        for sep in ("", " ")
    ]
    rows = db.scalars(
        select(WebhookEvent).where(
            WebhookEvent.payload.is_not(None),
            or_(*(WebhookEvent.payload.contains(n, autoescape=True) for n in needles)),
        )
    ).all()
    redacted = 0
    for row in rows:
        assert row.payload is not None, "query filtered for non-null payload"
        try:
            payload = json.loads(row.payload)
        except ValueError:
            continue
        if not isinstance(payload, dict) or payload.get("redacted"):
            continue
        if _event_references(payload) & wanted:
            row.payload = redact_event_payload(row.payload)
            redacted += 1
    return redacted
