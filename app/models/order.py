from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import relationship

from app.core.clock import UTCDateTime, utc_now
from app.core.config import settings
from app.models.base import Base
from app.models.enums import OrderStatus, PaymentStatus, create_string_enum


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    status = Column(create_string_enum(OrderStatus, "status"), nullable=False, server_default="new")
    payment_status = Column(create_string_enum(PaymentStatus, "payment_status"), nullable=False, server_default="pending")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    total_amount = Column(Numeric(10, 2), nullable=False)
    # ISO 4217 code total_amount is in, fixed when the order is placed
    # (settings.STORE_CURRENCY at that moment). Every payment attempt for
    # the order is made, and every provider report of one checked, in this
    # currency; a client never chooses it.
    currency = Column(String(3), nullable=False, default=lambda: settings.STORE_CURRENCY)
    shipping_address_id = Column(Integer, ForeignKey("addresses.id"), nullable=False)
    # Client-supplied key (Idempotency-Key header) for safe checkout retries.
    # Unique per user: the same key from two different users is not a clash.
    idempotency_key = Column(String, nullable=True)
    created_at = Column(UTCDateTime, default=utc_now, nullable=False)
    updated_at = Column(UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False)

    user = relationship("User", back_populates="orders")
    shipping_address = relationship("Address")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")

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
