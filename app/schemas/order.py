from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator, validator


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
    # Not a valid value for Order.payment_status (the orders table's check
    # constraint, derived from app.models.enums.PaymentStatus, intentionally
    # does not include this) - only used to represent a single payment
    # attempt's state in PaymentResponse while 3D Secure / SCA is pending.
    REQUIRES_ACTION = "requires_action"

class OrderItemBase(BaseModel):
    product_id: int
    quantity: int = Field(..., gt=0)
    price_at_time: Decimal = Field(..., ge=0, decimal_places=2)

    @validator('price_at_time')
    def validate_price(cls, v):
        return Decimal(str(v)).quantize(Decimal('0.01'))

class OrderItemCreate(OrderItemBase):
    pass

class OrderItemInDB(OrderItemBase):
    id: int
    order_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class OrderItemResponse(OrderItemInDB):
    product_name: str
    product_image: str

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="before")
    @classmethod
    def populate_product_fields_from_relationship(cls, values):
        # app.models.order_items.OrderItem has no product_name/product_image
        # columns - only product_id and a `product` relationship. Every
        # OrderResponse.from_orm(order) (create_order, get_order,
        # update_order_status, and the order-listing endpoints) always
        # raised "product_name: Field required" / "product_image: Field
        # required" for any order that actually has items, i.e. always.
        # mode="before" hands us the raw ORM instance (not a GetterDict),
        # so the `product` relationship is reachable directly; pull the
        # display fields from it. Dict input (already-shaped, e.g. from
        # tests) is left untouched.
        if isinstance(values, dict):
            return values
        product = getattr(values, "product", None)
        if product is not None:
            return {
                "id": values.id,
                "order_id": values.order_id,
                "product_id": values.product_id,
                "quantity": values.quantity,
                "price_at_time": values.price_at_time,
                "created_at": values.created_at,
                "product_name": product.name,
                "product_image": product.images[0] if product.images else "",
            }
        return values

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

class PaymentCreate(BaseModel):
    """Schema for payment processing"""
    order_id: int
    payment_method: str = Field(..., pattern="^(stripe|paypal)$")
    amount: Decimal
    currency: str = Field(..., pattern="^[A-Z]{3}$")
    # Stripe PaymentMethod token to confirm the intent with (e.g.
    # "pm_card_visa"). Optional and defaults to a successful mock card in
    # the provider - a real integration would require this (the client's
    # Stripe.js/Elements collects it), but making it optional here keeps
    # the existing request shape backward compatible.
    payment_method_token: Optional[str] = None

    @validator('amount')
    def validate_amount(cls, v):
        return Decimal(str(v)).quantize(Decimal('0.01'))

class PaymentResponse(PaymentCreate):
    id: int
    status: PaymentStatus
    created_at: datetime
    transaction_id: Optional[str] = None
    # Client-side secret needed to complete 3D Secure authentication via
    # Stripe.js when status == requires_action; mirrors the real
    # PaymentIntent.client_secret / next_action shape.
    client_secret: Optional[str] = None
    requires_action: bool = False
    next_action: Optional[dict] = None

    model_config = ConfigDict(from_attributes=True)

class RefundCreate(BaseModel):
    """Schema for issuing a refund on a paid order."""
    amount: Optional[Decimal] = Field(
        None, gt=0, description="Amount to refund; omit for a full refund"
    )

    @validator('amount')
    def validate_amount(cls, v):
        if v is None:
            return v
        return Decimal(str(v)).quantize(Decimal('0.01'))

class RefundResponse(BaseModel):
    id: str
    order_id: int
    amount: Decimal
    currency: str
    status: str
    payment_status: PaymentStatus

    model_config = ConfigDict(from_attributes=True)

# Circular imports are resolved at runtime
from app.schemas.address import AddressResponse  # noqa: E402
