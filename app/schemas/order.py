from datetime import datetime
from typing import List, Optional
from decimal import Decimal
from pydantic import BaseModel, Field, ConfigDict, validator
from enum import Enum

class OrderStatus(str, Enum):
    NEW = "new"
    CONFIRMED = "confirmed"
    PROCESSING = "processing"
    SENT = "sent"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"

class PaymentStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"

class OrderItemBase(BaseModel):
    product_id: int
    quantity: int = Field(..., gt=0)
    price_at_time: Decimal = Field(..., ge=0, decimal_places=2)

    @validator('price_at_time')
    def validate_price(cls, v):
        return Decimal(str(v)).quantize(Decimal('0.01'))

class OrderItemCreate(OrderItemBase):
    pass

class OrderItemUpdate(BaseModel):
    quantity: int = Field(..., gt=0)

class OrderItemInDB(OrderItemBase):
    id: int
    order_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class OrderItemResponse(OrderItemInDB):
    product_name: str
    product_image: str

    model_config = ConfigDict(from_attributes=True)

class OrderBase(BaseModel):
    shipping_address_id: int
    delivery_instructions: Optional[str] = Field(None, max_length=500)

class OrderCreate(OrderBase):
    items: List[OrderItemCreate]

    @validator('items')
    def validate_items(cls, v):
        if not v:
            raise ValueError('Order must contain at least one item')
        # Check for duplicates
        product_ids = [item.product_id for item in v]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError('Duplicate products in order')
        return v

class OrderUpdate(BaseModel):
    status: Optional[OrderStatus] = None
    payment_status: Optional[PaymentStatus] = None
    delivery_instructions: Optional[str] = Field(None, max_length=500)

class OrderInDB(OrderBase):
    id: int
    user_id: int
    status: OrderStatus
    payment_status: PaymentStatus
    total_amount: Decimal
    created_at: datetime
    updated_at: datetime
    items: List[OrderItemInDB]

    model_config = ConfigDict(from_attributes=True)

    @validator('total_amount')
    def validate_total(cls, v):
        return Decimal(str(v)).quantize(Decimal('0.01'))

class OrderResponse(OrderInDB):
    shipping_address: "AddressResponse"  # Forward reference
    items: List[OrderItemResponse]

    model_config = ConfigDict(from_attributes=True)

class OrderListResponse(BaseModel):
    """Schema for list of orders with pagination"""
    items: List[OrderResponse]
    total: int
    page: int
    size: int
    has_more: bool

    model_config = ConfigDict(from_attributes=True)

class OrderFilter(BaseModel):
    """Schema for order filtering"""
    status: Optional[OrderStatus] = None
    payment_status: Optional[PaymentStatus] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    min_amount: Optional[Decimal] = Field(None, ge=0)
    max_amount: Optional[Decimal] = Field(None, ge=0)
    page: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)

    @validator('end_date')
    def validate_date_range(cls, v, values):
        if v and 'start_date' in values and values['start_date']:
            if v < values['start_date']:
                raise ValueError('end_date must be after start_date')
        return v

    @validator('max_amount')
    def validate_amount_range(cls, v, values):
        if v and 'min_amount' in values and values['min_amount']:
            if v < values['min_amount']:
                raise ValueError('max_amount must be greater than min_amount')
        return v

class PaymentCreate(BaseModel):
    """Schema for payment processing"""
    order_id: int
    payment_method: str = Field(..., pattern="^(stripe|paypal)$")
    amount: Decimal
    currency: str = Field(..., pattern="^[A-Z]{3}$")

    @validator('amount')
    def validate_amount(cls, v):
        return Decimal(str(v)).quantize(Decimal('0.01'))

class PaymentResponse(PaymentCreate):
    id: int
    status: PaymentStatus
    created_at: datetime
    transaction_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

# Circular imports are resolved at runtime
from app.schemas.address import AddressResponse  # noqa: E402