from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

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
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    order = relationship("Order", backref="payments")

    __table_args__ = (
        UniqueConstraint(
            "order_id", "idempotency_key", name="uix_payment_order_idempotency_key"
        ),
        Index("idx_payment_order_id", "order_id"),
    )


class WebhookEvent(Base):
    """
    Records every processed Stripe webhook event id, so a duplicate delivery
    of the same event (Stripe guarantees at-least-once delivery and retries
    for up to 72 hours) is detected and short-circuited before any state
    mutation - the unique constraint on event_id is the idempotency guard.
    """

    __tablename__ = "webhook_events"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String, nullable=False, unique=True, index=True)
    event_type = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
