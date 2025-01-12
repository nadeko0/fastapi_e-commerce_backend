from datetime import datetime
import enum
from sqlalchemy import Column, Integer, String, ForeignKey, Enum, DateTime, Numeric, Text
from sqlalchemy.orm import relationship

from app.models.base import Base

class OrderStatus(str, enum.Enum):
    NEW = "new"
    CONFIRMED = "confirmed"
    PROCESSING = "processing"
    SENT = "sent"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"

class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"

class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    status = Column(Enum(OrderStatus), nullable=False, default=OrderStatus.NEW)
    payment_status = Column(Enum(PaymentStatus), nullable=False, default=PaymentStatus.PENDING)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    total_amount = Column(Numeric(10, 2), nullable=False)
    shipping_address = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    user = relationship("User", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")