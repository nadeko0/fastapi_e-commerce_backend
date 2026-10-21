from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator, validator

from app.core.config import settings


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
    variant_id: Optional[int] = None
    quantity: int = Field(..., gt=0)
    price_at_time: Decimal = Field(..., ge=0, decimal_places=2)

    @validator('price_at_time')
    def validate_price(cls, v):
        return Decimal(str(v)).quantize(Decimal('0.01'))

class OrderItemInDB(OrderItemBase):
    id: int
    order_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class OrderItemResponse(OrderItemInDB):
    product_name: str
    product_image: str
    # None for the common no-variant line - only set when this line was for
    # a specific ProductVariant (app/models/product.py).
    variant_sku: Optional[str] = None

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
                "variant_id": values.variant_id,
                "quantity": values.quantity,
                "price_at_time": values.price_at_time,
                "created_at": values.created_at,
                "product_name": product.name,
                "product_image": product.images[0] if product.images else "",
                "variant_sku": values.variant_sku,
            }
        return values

class OrderBase(BaseModel):
    shipping_address_id: int
    delivery_instructions: Optional[str] = Field(None, max_length=500)

class OrderInDB(OrderBase):
    id: int
    user_id: int
    status: OrderStatus
    payment_status: PaymentStatus
    total_amount: Decimal
    currency: str
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

# Order.total_amount is Numeric(10, 2), so no amount paid or refunded for an
# order has more digits than that. Checked by the amount fields themselves,
# before their validators quantize: quantize raises decimal.InvalidOperation
# past its 28-digit context, which pydantic does not turn into a 422, so
# {"amount": 1e30} used to answer 500.
ORDER_AMOUNT_MAX_DIGITS = 10

class PaymentCreate(BaseModel):
    """Schema for payment processing"""
    order_id: int
    payment_method: str = Field(..., pattern="^(stripe|paypal)$")
    amount: Decimal = Field(..., max_digits=ORDER_AMOUNT_MAX_DIGITS)
    # Optional: the order is always charged in its own currency
    # (Order.currency). A client may still send the code it expects to pay
    # in; one that differs is rejected with 400, the same as an amount that
    # differs from the order total, rather than ignored, so a client that
    # believes it is paying in another currency finds out before any charge.
    currency: Optional[str] = Field(None, pattern="^[A-Z]{3}$")
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

def _url_origin(url: str) -> Optional[tuple]:
    """(scheme, host, port) of an http(s) URL, the port filled in from the
    scheme when absent; None for anything else, including a URL carrying
    userinfo or a port that is not a number."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ('http', 'https') or not parts.hostname or '@' in parts.netloc:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    return scheme, parts.hostname.lower(), port or (443 if scheme == 'https' else 80)


class CheckoutSessionCreate(BaseModel):
    """
    Optional overrides for creating a Stripe Checkout Session for an order
    (the hosted-redirect payment flow - see /orders/{id}/checkout-session).
    Unlike PaymentCreate, the amount is never client-supplied: it is always
    derived from the order's own total_amount server-side, since the entire
    point of this flow is that the client never handles payment details.
    The currency is the order's too; as in PaymentCreate, one sent that
    differs from it is rejected (400).
    """
    currency: Optional[str] = Field(None, pattern="^[A-Z]{3}$")
    # Where Stripe sends the customer after the hosted page. Omit to use the
    # frontend's /checkout/success and /checkout/cancel (built on
    # settings.FRONTEND_URL in create_checkout_session).
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None

    # Client-controlled redirect targets, followed by the customer's browser
    # from a Stripe-hosted page they trust, right after paying. Any http(s)
    # URL used to be accepted, which made that page an open redirect to an
    # address of the API caller's choosing. Only the frontend's own origin
    # (scheme, host, port of settings.FRONTEND_URL) is accepted now, 422
    # otherwise. Whitespace, control characters, backslashes and userinfo
    # ("user@host") are refused outright rather than parsed: browsers and
    # urllib disagree on where the host of such a URL is
    # ("http://evil.example\@shop" is evil.example to a browser).
    @validator('success_url', 'cancel_url')
    def validate_redirect_on_frontend(cls, v):
        if v is None:
            return v
        if any(c.isspace() or c == '\\' or ord(c) < 0x20 for c in v):
            raise ValueError('must be a plain URL on the frontend origin')
        origin = _url_origin(v)
        if origin is None:
            raise ValueError('must be an http:// or https:// URL')
        if origin != _url_origin(settings.FRONTEND_URL):
            raise ValueError(f'must be on the frontend origin {settings.FRONTEND_URL}')
        return v

class CheckoutSessionResponse(BaseModel):
    order_id: int
    payment_id: int
    checkout_session_id: str
    url: str
    status: str
    amount: Decimal
    currency: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class RefundCreate(BaseModel):
    """Schema for issuing a refund on a paid order."""
    amount: Optional[Decimal] = Field(
        None, gt=0, max_digits=ORDER_AMOUNT_MAX_DIGITS,
        description="Amount to refund; omit for a full refund",
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
