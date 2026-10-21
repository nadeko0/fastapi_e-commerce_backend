"""
What happens to a charge after it succeeded: its refunds and its disputes,
one row per provider object (Stripe Refund re_..., Dispute du_...), keyed
by the provider's id so that the same object reported twice - by the
/refund call that created it and by its webhook, or by two webhooks - is
recorded once.

Payment.amount_refunded stays the running total the rest of the code
reads: the sum of this charge's refunds recorded here.
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import relationship

from app.models.base import Base

# Dispute.status values of a dispute that is over; any other is open
# (Stripe: warning_needs_response, warning_under_review, needs_response,
# under_review).
DISPUTE_CLOSED_STATUSES = ("won", "lost", "warning_closed", "prevented")


class PaymentRefund(Base):
    __tablename__ = "payment_refunds"

    id = Column(Integer, primary_key=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=False)
    # The provider's Refund id (re_...).
    refund_id = Column(String, nullable=False, unique=True)
    amount = Column(Integer, nullable=False)  # smallest currency unit
    currency = Column(String(3), nullable=False)
    # Stripe's Refund.status as last reported: pending, requires_action,
    # succeeded, failed or canceled.
    status = Column(String, nullable=False)
    # "admin" for a refund made through POST /orders/{id}/refund,
    # "provider" for one first seen in a webhook (made in the Stripe
    # Dashboard, or by another integration).
    source = Column(String, nullable=False)
    failure_reason = Column(String, nullable=True)
    # Set when the refund failed or was canceled after it had been
    # recorded: the customer did not get this money back and a person has
    # to arrange it another way.
    requires_manual_review = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    payment = relationship("Payment", backref="refunds")

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payment_refund_amount_positive"),
        Index("idx_payment_refund_payment_id", "payment_id"),
    )


class PaymentDispute(Base):
    __tablename__ = "payment_disputes"

    id = Column(Integer, primary_key=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=False)
    # The provider's Dispute id (du_...).
    dispute_id = Column(String, nullable=False, unique=True)
    amount = Column(Integer, nullable=False)  # smallest currency unit
    currency = Column(String(3), nullable=False)
    # Stripe's Dispute.status as last reported. Once it is one of
    # DISPUTE_CLOSED_STATUSES it no longer changes: a late, out-of-order
    # event for the dispute does not reopen it.
    status = Column(String, nullable=False)
    reason = Column(String, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    payment = relationship("Payment", backref="disputes")

    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_payment_dispute_amount_non_negative"),
        Index("idx_payment_dispute_payment_id", "payment_id"),
    )
