from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    String,
    func,
    select,
    text,
)
from sqlalchemy.orm import (
    Mapped,
    column_property,
    declared_attr,
    mapped_column,
    relationship,
)

from app.core.clock import utc_now
from app.core.config import settings
from app.models.base import Base
from app.models.enums import OrderStatus, PaymentStatus
from app.models.payment import Payment
from app.models.payment_ledger import PaymentDispute

if TYPE_CHECKING:
    from app.models.address import Address
    from app.models.order_items import OrderItem
    from app.models.user import User


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(server_default="new")
    payment_status: Mapped[str] = mapped_column(server_default="pending")
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    total_amount: Mapped[Decimal]
    # ISO 4217 code total_amount is in, fixed when the order is placed
    # (settings.STORE_CURRENCY at that moment). Every payment attempt for
    # the order is made, and every provider report of one checked, in this
    # currency; a client never chooses it.
    currency: Mapped[str] = mapped_column(String(3), default=lambda: settings.STORE_CURRENCY)
    shipping_address_id: Mapped[int] = mapped_column(ForeignKey("addresses.id"))
    ship_to_street: Mapped[str | None]
    ship_to_city: Mapped[str | None]
    ship_to_state: Mapped[str | None]
    ship_to_postal_code: Mapped[str | None]
    ship_to_country: Mapped[str | None]
    ship_to_address_type: Mapped[str | None]
    ship_to_delivery_phone: Mapped[str | None]
    ship_to_delivery_instructions: Mapped[str | None]
    # Client-supplied key (Idempotency-Key header) for safe checkout retries.
    # Unique per user: the same key from two different users is not a clash.
    idempotency_key: Mapped[str | None]
    # SHA-256 of the request that carried the key (app/core/idempotency.py);
    # NULL for orders stored before it existed, which replay without a check.
    idempotency_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)

    user: Mapped["User"] = relationship(back_populates="orders")
    shipping_address: Mapped["Address"] = relationship()
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    payments: Mapped[list["Payment"]] = relationship(back_populates="order")

    # Status of the latest dispute (chargeback or inquiry) on any of the
    # order's payments, NULL when there is none. Derived from
    # payment_disputes, read with the order (no table or column of its own,
    # so it cannot go stale): it is how a customer or an admin sees that an
    # order is on hold (open dispute) or was lost to a chargeback. A lost
    # dispute only flags the order; nothing cancels it or returns stock.
    @declared_attr
    def dispute_status(cls) -> Mapped[str | None]:
        # The latest dispute is the one with the highest id (a max(), not an
        # ORDER BY ... LIMIT 1: the order list's own ORDER BY stays the only one).
        # (A table alias, not aliased(): that would configure the mappers
        # while the models are still being imported.)
        # The inner select is nested two levels below the order's, and
        # auto-correlation only looks at the select directly around it (FROM
        # payment_disputes): without correlate_except it put `orders` into its
        # own FROM, and every order showed the newest dispute of any order.
        # Both lookups use an index (idx_payment_order_id,
        # idx_payment_dispute_payment_id).
        latest = PaymentDispute.__table__.alias("latest_dispute")
        latest_id = (
            select(func.max(latest.c.id))
            .join(Payment, Payment.id == latest.c.payment_id)
            .where(Payment.order_id == cls.id)
            .correlate_except(latest, Payment.__table__)
            .scalar_subquery()
        )
        return column_property(
            select(PaymentDispute.status).where(PaymentDispute.id == latest_id).scalar_subquery(),
            # Not expired when the order's own row is updated: an expired
            # attribute can only be reloaded by IO, and the response is built
            # on an AsyncSession where that raises MissingGreenlet. It is read
            # again with every load of the order.
            expire_on_flush=False,
        )

    @property
    def shipping_address_shown(self):
        """The address the order was placed with, copied at checkout: editing or
        erasing the customer's address must not rewrite a past order. The
        fields the copy does not hold are fixed, not read from the live row
        (which changes when the customer switches their default): a snapshot
        is never the default, and it was made when the order was. An order
        older than these columns shows its live address row."""
        live = self.shipping_address
        if self.ship_to_street is None:
            return live
        return SimpleNamespace(
            id=self.shipping_address_id,
            street=self.ship_to_street,
            city=self.ship_to_city,
            state=self.ship_to_state,
            postal_code=self.ship_to_postal_code,
            country=self.ship_to_country,
            address_type=self.ship_to_address_type or live.address_type,
            delivery_phone=self.ship_to_delivery_phone,
            delivery_instructions=self.ship_to_delivery_instructions,
            is_default=False,
            created_at=self.created_at,
            updated_at=self.created_at,
        )

    __table_args__ = (
        # See Product.__table_args__. Zero is allowed: a product can be free.
        CheckConstraint("total_amount >= 0", name="ck_order_total_non_negative"),
        CheckConstraint(
            f"status IN {tuple(s.value for s in OrderStatus)}",
            name="ck_order_status"
        ),
        CheckConstraint(
            f"payment_status IN {tuple(s.value for s in PaymentStatus)}",
            name="ck_payment_status"
        ),
        Index('idx_order_user_id', 'user_id'),
        Index('idx_order_status', 'status'),
        Index('idx_order_shipping_address_id', 'shipping_address_id'),
        # The unpaid-order expiry sweep (app/tasks.py): new orders placed
        # before a cutoff. New orders are few, so the index stays small.
        Index(
            'idx_order_new_created_at',
            'created_at',
            postgresql_where=text("status = 'new'"),
            sqlite_where=text("status = 'new'"),
        ),
        Index(
            'uix_order_user_idempotency_key',
            'user_id', 'idempotency_key',
            unique=True,
            postgresql_where=Column('idempotency_key').isnot(None),
        ),
    )
