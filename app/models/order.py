from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import relationship

from app.models.base import Base
from app.models.enums import OrderStatus, PaymentStatus, create_string_enum


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    status = Column(*create_string_enum(OrderStatus, "status")[0:1], nullable=False, server_default="new")
    payment_status = Column(*create_string_enum(PaymentStatus, "payment_status")[0:1], nullable=False, server_default="pending")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    total_amount = Column(Numeric(10, 2), nullable=False)
    shipping_address_id = Column(Integer, ForeignKey("addresses.id"), nullable=False)
    # Client-supplied key (Idempotency-Key header) for safe checkout retries.
    # Unique per user: the same key from two different users is not a clash.
    idempotency_key = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="orders")
    shipping_address = relationship("Address")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")

    __table_args__ = (
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
        Index(
            'uix_order_user_idempotency_key',
            'user_id', 'idempotency_key',
            unique=True,
            postgresql_where=Column('idempotency_key').isnot(None),
        ),
    )
