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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import utc_now
from app.core.config import settings
from app.models.base import Base
from app.models.enums import OrderStatus, PaymentStatus

if TYPE_CHECKING:
    from app.models.address import Address
    from app.models.order_items import OrderItem
    from app.models.payment import Payment
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

    @property
    def shipping_address_shown(self):
        """The address the order was placed with, copied at checkout: editing or
        erasing the customer's address must not rewrite a past order. An order
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
            is_default=live.is_default,
            created_at=live.created_at,
            updated_at=live.updated_at,
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
        Index(
            'uix_order_user_idempotency_key',
            'user_id', 'idempotency_key',
            unique=True,
            postgresql_where=Column('idempotency_key').isnot(None),
        ),
    )
