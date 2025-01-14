from datetime import datetime
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Numeric, Text, CheckConstraint
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
    shipping_address = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    user = relationship("User", back_populates="orders")
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
    )