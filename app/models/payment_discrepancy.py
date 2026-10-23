"""
What the daily reconciliation (app/services/reconciliation.py) found to
differ between our payment records and the provider's: one row per
difference, so an admin sees what differs, since when, and what each side
says. Nothing in the payment flow reads this table; it only reports.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    ForeignKey,
    Index,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import utc_now
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.payment import Payment

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

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"))
    kind: Mapped[str]
    # See the KIND_* note above. Never NULL, so that the unique constraint
    # below holds (NULLs are all distinct to a unique index).
    subject: Mapped[str] = mapped_column(default="")
    # What each side says, as text ("succeeded", "1999", "EUR"); None where
    # that side has nothing (a refund only one side knows).
    ours: Mapped[str | None]
    theirs: Mapped[str | None]
    # When this difference was first seen, and last seen, by a run. A
    # difference that went away and came back starts again (detected_at is
    # reset), so detected_at is always "since when".
    detected_at: Mapped[datetime] = mapped_column(default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(default=utc_now)
    # Set by the first run that no longer sees the difference; None while
    # it is open.
    resolved_at: Mapped[datetime | None]

    payment: Mapped["Payment"] = relationship(back_populates="discrepancies")

    __table_args__ = (
        UniqueConstraint("payment_id", "kind", "subject", name="uix_payment_discrepancy"),
        Index("idx_payment_discrepancy_payment_id", "payment_id"),
        Index("idx_payment_discrepancy_resolved_at", "resolved_at"),
    )
