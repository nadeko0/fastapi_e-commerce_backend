from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.core.clock import UTCDateTime, utc_now
from app.models.base import Base


class Payment(Base):
    """
    One row per payment *attempt* against an order (not one row per order -
    a declined attempt followed by a successful retry is two rows). Amounts
    are stored as integers in the smallest currency unit (cents), matching
    the Stripe PaymentIntent they mirror - never Decimal dollars.
    """

    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    provider = Column(String, nullable=False, default="stripe")
    payment_intent_id = Column(String, nullable=False, unique=True, index=True)
    # Client-supplied Idempotency-Key header for this payment attempt.
    # Unique per order: replaying the same key for the same order must
    # return the original attempt instead of creating a new PaymentIntent.
    idempotency_key = Column(String, nullable=True)
    amount = Column(Integer, nullable=False)  # smallest currency unit
    amount_refunded = Column(Integer, nullable=False, default=0)
    currency = Column(String(3), nullable=False)
    status = Column(String, nullable=False, default="processing")
    client_secret = Column(String, nullable=True)
    # Set only for the Checkout Session flow (create_checkout_session) - a
    # second, independent way to pay this attempt, distinct from the
    # PaymentIntent-based /pay flow above. NULL for every /pay attempt.
    # Unique (not composite with order_id) because Stripe session ids are
    # already globally unique - mirrors payment_intent_id's own uniqueness.
    checkout_session_id = Column(String, nullable=True, unique=True, index=True)
    checkout_session_url = Column(String, nullable=True)
    # Set when a webhook reports an outcome that conflicts with the current
    # order state (e.g. "succeeded" for an order already cancelled) - the
    # payment record is kept truthful but the order is NOT silently
    # overridden; this flag marks the row for manual reconciliation.
    requires_manual_review = Column(Boolean, nullable=False, default=False)
    created_at = Column(UTCDateTime, default=utc_now, nullable=False)
    updated_at = Column(UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False)

    order = relationship("Order", backref="payments")

    __table_args__ = (
        # See Product.__table_args__. amount >= 0 rather than > 0: the row
        # is written before the provider is asked, and the provider is what
        # turns down a zero amount (with a 4xx, not a database error).
        CheckConstraint("amount >= 0", name="ck_payment_amount_non_negative"),
        CheckConstraint("amount_refunded >= 0", name="ck_payment_refunded_non_negative"),
        CheckConstraint("amount_refunded <= amount", name="ck_payment_refunded_within_amount"),
        UniqueConstraint(
            "order_id", "idempotency_key", name="uix_payment_order_idempotency_key"
        ),
        Index("idx_payment_order_id", "order_id"),
    )


# WebhookEvent.status values. An event is stored as "received" by the webhook
# route and applied later by the process_stripe_event task:
#   received   stored, not applied yet
#   processing a worker holds the event right now. The claim is the row lock
#              (SELECT ... FOR UPDATE SKIP LOCKED) of the worker's open
#              transaction, which also carries the state changes, so this
#              value is never committed: a crash rolls everything back and
#              leaves the event as it was. Part of the vocabulary for
#              readers of the column (and for a future claim that is
#              committed first).
#   processed  applied (or acknowledged as nothing to apply)
#   retrying   an attempt failed, or the event overtook its payment attempt;
#              the task retries it with a backoff
#   failed     out of retries; an admin can replay it
WEBHOOK_EVENT_RECEIVED = "received"
WEBHOOK_EVENT_PROCESSING = "processing"
WEBHOOK_EVENT_PROCESSED = "processed"
WEBHOOK_EVENT_RETRYING = "retrying"
WEBHOOK_EVENT_FAILED = "failed"
WEBHOOK_EVENT_STATUSES = (
    WEBHOOK_EVENT_RECEIVED,
    WEBHOOK_EVENT_PROCESSING,
    WEBHOOK_EVENT_PROCESSED,
    WEBHOOK_EVENT_RETRYING,
    WEBHOOK_EVENT_FAILED,
)


class WebhookEvent(Base):
    """
    Every Stripe webhook event the route accepted, stored with its payload
    before anything is applied. Stripe guarantees at-least-once delivery and
    retries for up to 72 hours, so the unique constraint on event_id is the
    idempotency guard: a duplicate delivery is answered without being stored
    or applied again. The route only stores and enqueues; the
    process_stripe_event task applies the event and marks it processed in
    the same transaction as what it changed.
    """

    __tablename__ = "webhook_events"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String, nullable=False, unique=True, index=True)
    event_type = Column(String, nullable=False)
    # The event as JSON text (id, type, created, data). NULL only for rows
    # recorded before events were stored; those are all processed.
    payload = Column(Text, nullable=True)
    status = Column(String, nullable=False, default=WEBHOOK_EVENT_RECEIVED)
    # Attempts that did not apply the event (an error, or an event that
    # overtook its payment attempt).
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    processed_at = Column(UTCDateTime, nullable=True)
    created_at = Column(UTCDateTime, default=utc_now, nullable=False)
    updated_at = Column(UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (
        # The sweeper looks for old events still received or retrying, and
        # the admin list filters by status, newest first.
        Index("idx_webhook_event_status_updated", "status", "updated_at"),
    )
