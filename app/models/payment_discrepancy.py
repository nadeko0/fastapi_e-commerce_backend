"""
What the daily reconciliation (app/services/reconciliation.py) found to
differ between our payment records and the provider's: one row per
difference, so an admin sees what differs, since when, and what each side
says. Nothing in the payment flow reads this table; it only reports.
"""
from datetime import datetime

from sqlalchemy import (
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

# Discrepancy.kind values. "Subject" is the refund id for the refund kinds
# and "" for the others.
KIND_PAYMENT_MISSING = "payment_missing"          # the provider has no such payment
KIND_STATUS = "status"                            # our status vs the PaymentIntent's
KIND_AMOUNT = "amount"                            # amount charged / received
KIND_CURRENCY = "currency"
KIND_REFUNDED_TOTAL = "refunded_total"            # our amount_refunded vs the sum of its refunds
KIND_REFUND_STATUS = "refund_status"              # one refund's status
KIND_REFUND_AMOUNT = "refund_amount"              # one refund's amount
KIND_REFUND_NOT_RECORDED = "refund_not_recorded"  # the provider has a refund we do not
KIND_REFUND_UNKNOWN = "refund_unknown_to_provider"  # we record a refund the provider does not have


class PaymentDiscrepancy(Base):
    __tablename__ = "payment_discrepancies"

    id = Column(Integer, primary_key=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=False)
    kind = Column(String, nullable=False)
    # See the KIND_* note above. Never NULL, so that the unique constraint
    # below holds (NULLs are all distinct to a unique index).
    subject = Column(String, nullable=False, default="")
    # What each side says, as text ("succeeded", "1999", "EUR"); None where
    # that side has nothing (a refund only one side knows).
    ours = Column(String, nullable=True)
    theirs = Column(String, nullable=True)
    # When this difference was first seen, and last seen, by a run. A
    # difference that went away and came back starts again (detected_at is
    # reset), so detected_at is always "since when".
    detected_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Set by the first run that no longer sees the difference; None while
    # it is open.
    resolved_at = Column(DateTime, nullable=True)

    payment = relationship("Payment", backref="discrepancies")

    __table_args__ = (
        UniqueConstraint("payment_id", "kind", "subject", name="uix_payment_discrepancy"),
        Index("idx_payment_discrepancy_payment_id", "payment_id"),
        Index("idx_payment_discrepancy_resolved_at", "resolved_at"),
    )
