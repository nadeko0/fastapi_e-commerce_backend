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
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import utc_now
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.payment import Payment

# Dispute.status values of a dispute that is over; any other is open
# (Stripe: warning_needs_response, warning_under_review, needs_response,
# under_review).
DISPUTE_CLOSED_STATUSES = ("won", "lost", "warning_closed", "prevented")

# The values below are Stripe's own (stripe-python 16.0.0, Refund.status,
# Dispute.status and Dispute.reason), stored as received. Stripe documents them
# as open sets, so the columns stay free text; these are the values published
# in the OpenAPI document.
REFUND_STATUSES = ("pending", "requires_action", "succeeded", "failed", "canceled")
# Who recorded the refund: an admin's /refund call, or an event/read from the provider.
REFUND_SOURCES = ("admin", "provider")
DISPUTE_STATUSES = (
    "warning_needs_response",
    "warning_under_review",
    "warning_closed",
    "needs_response",
    "under_review",
    "won",
    "lost",
    "prevented",
)
DISPUTE_REASONS = (
    "bank_cannot_process",
    "check_returned",
    "credit_not_processed",
    "customer_initiated",
    "debit_not_authorized",
    "duplicate",
    "fraudulent",
    "general",
    "incorrect_account_details",
    "insufficient_funds",
    "noncompliant",
    "product_not_received",
    "product_unacceptable",
    "subscription_canceled",
    "unrecognized",
)


class PaymentRefund(Base):
    __tablename__ = "payment_refunds"

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"))
    # The provider's Refund id (re_...).
    refund_id: Mapped[str] = mapped_column(unique=True)
    amount: Mapped[int]  # smallest currency unit
    currency: Mapped[str] = mapped_column(String(3))
    # Stripe's Refund.status as last reported: pending, requires_action,
    # succeeded, failed or canceled.
    status: Mapped[str]
    # "admin" for a refund made through POST /orders/{id}/refund,
    # "provider" for one first seen in a webhook (made in the Stripe
    # Dashboard, or by another integration).
    source: Mapped[str]
    failure_reason: Mapped[str | None]
    # Set when the refund failed or was canceled after it had been
    # recorded: the customer did not get this money back and a person has
    # to arrange it another way.
    requires_manual_review: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)

    payment: Mapped["Payment"] = relationship(back_populates="refunds")

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payment_refund_amount_positive"),
        Index("idx_payment_refund_payment_id", "payment_id"),
    )


class PaymentDispute(Base):
    __tablename__ = "payment_disputes"

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"))
    # The provider's Dispute id (du_...).
    dispute_id: Mapped[str] = mapped_column(unique=True)
    amount: Mapped[int]  # smallest currency unit
    currency: Mapped[str] = mapped_column(String(3))
    # Stripe's Dispute.status as last reported. Once it is one of
    # DISPUTE_CLOSED_STATUSES it no longer changes: a late, out-of-order
    # event for the dispute does not reopen it.
    status: Mapped[str]
    reason: Mapped[str | None]
    closed_at: Mapped[datetime | None]
    # Dispute.evidence_details as last reported; None until an event carries
    # it (disputes stored before migration 804f43988360).
    evidence_due_by: Mapped[datetime | None]
    evidence_has_evidence: Mapped[bool | None]
    evidence_past_due: Mapped[bool | None]
    evidence_submission_count: Mapped[int | None]
    # "manual", "smart_disputes" or "not_submitted" in API 2026-10-28.endive;
    # stored as reported, so a value Stripe adds later is kept too.
    evidence_submission_method: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)

    payment: Mapped["Payment"] = relationship(back_populates="disputes")

    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_payment_dispute_amount_non_negative"),
        Index("idx_payment_dispute_payment_id", "payment_id"),
    )
