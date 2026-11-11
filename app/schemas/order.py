import re
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from app.core.config import settings
from app.models.enums import OrderStatus
from app.models.payment import PAYMENT_STATUSES
from app.models.payment_ledger import (
    DISPUTE_REASONS,
    DISPUTE_STATUSES,
    REFUND_SOURCES,
    REFUND_STATUSES,
)
from app.schemas.common import (
    MONEY_OUTPUT_SCHEMA,
    Int32,
    RequestBody,
    ResponseBody,
    ResponseMoney,
    published_values,
)

# Order.total_amount is Numeric(10, 2), so no amount paid or refunded for an
# order has more digits than that. Checked by the amount fields themselves,
# before their validators quantize: quantize raises decimal.InvalidOperation
# past its 28-digit context, which pydantic does not turn into a 422, so
# {"amount": 1e30} used to answer 500.
ORDER_AMOUNT_MAX_DIGITS = 10
# Whole cents only: a fraction of a cent used to be rounded away by those
# validators after gt=0 had passed, so a refund of 0.001 went out as 0.00
# and 99.995 was taken as 100.00. Trailing zeros do not count (10.500 is
# 10.50), and a JSON number is read by its shortest form (19.99, not the
# float's binary expansion).
ORDER_AMOUNT_DECIMAL_PLACES = 2
# The largest total an order can have: what its payment can record, as
# Payment.amount holds cents in an INTEGER column. Order.total_amount, Numeric(10, 2),
# would take more, and such an order could never be paid (a 500 on /pay).
ORDER_TOTAL_MAX = Decimal("21474836.47")


def _to_cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


# What a client may send for an amount: digits, and at most two fraction
# digits after a point (trailing zeros do not count: 10.500 is 10.50, as
# ORDER_AMOUNT_DECIMAL_PLACES says). A sign is let through so that a negative
# amount is refused by its bound (with the message of the bound).
_MONEY_UNSIGNED = r"[0-9]+(\.[0-9]{1,2}0*)?"
_MONEY_PATTERN = "-?" + _MONEY_UNSIGNED
_MONEY_INPUT = re.compile(_MONEY_PATTERN)
# The same, for the OpenAPI document only, with the whole-part budget of the
# field spelled out: max_digits=10 less decimal_places=2 leaves 8 digits before
# the point (leading zeros are not digits). Not used to check a value: the
# field does that, and its message must not change.
_MONEY_SCHEMA_UNSIGNED = r"0*[0-9]{1,8}(?:\.[0-9]{1,2}0*)?"
# For an amount that must be greater than zero: some digit other than 0.
_NOT_ZERO = r"(?=[0.]*[1-9])"


def _decimal_string_only(value):
    """A JSON number is a binary float in most clients (0.1 + 0.2 is
    0.30000000000000004), and an integer is a money amount whose unit the
    client may have got wrong, so an amount is accepted only as a decimal
    string ("10.50"). A Decimal instance passes: it is what a database row
    or the code itself hands to the model, and JSON cannot produce one.
    Pydantic offers no switch for "string but not number" on a Decimal: its
    strict mode accepts only Decimal instances in Python mode and has no
    effect on JSON input (conversion table, Decimal), hence a before
    validator, the documented hook for checking the raw input."""
    if isinstance(value, bool | int | float):
        raise ValueError('must be a decimal string such as "10.50", not a JSON number')
    if isinstance(value, str) and not _MONEY_INPUT.fullmatch(value):
        raise ValueError('must be a decimal string with at most two fraction digits, such as "10.50"')
    return value


def _money_input_schema(pattern: str, description: str) -> WithJsonSchema:
    # What a client may send, as OpenAPI sees it: Pydantic's own schema for a
    # Decimal in input mode is "number or string". Responses keep Pydantic's
    # (a string).
    return WithJsonSchema(
        {"type": "string", "pattern": pattern, "examples": ["10.50"], "description": description},
        mode="validation",
    )


# An amount of money: at most ORDER_AMOUNT_MAX_DIGITS digits and whole cents
# (both checked before the AfterValidator, so quantize never sees a value it
# cannot handle), then written with exactly two decimal places (10.5 -> 10.50).
# On input it is a decimal string only (_decimal_string_only).
# A bound (ge/gt) has to sit before the AfterValidator, where pydantic can
# still fold it into the decimal schema; after it, it would not be enforced
# and would leak into the OpenAPI schema. Hence one alias per bound.
Money = Annotated[
    Decimal,
    Field(max_digits=ORDER_AMOUNT_MAX_DIGITS, decimal_places=ORDER_AMOUNT_DECIMAL_PLACES),
    BeforeValidator(_decimal_string_only),
    AfterValidator(_to_cents),
    _money_input_schema(
        f"^-?{_MONEY_SCHEMA_UNSIGNED}$", "Decimal string, at most two fraction digits."
    ),
    MONEY_OUTPUT_SCHEMA,
]
NonNegativeMoney = Annotated[
    Decimal,
    Field(
        ge=0, max_digits=ORDER_AMOUNT_MAX_DIGITS, decimal_places=ORDER_AMOUNT_DECIMAL_PLACES
    ),
    BeforeValidator(_decimal_string_only),
    AfterValidator(_to_cents),
    _money_input_schema(
        f"^{_MONEY_SCHEMA_UNSIGNED}$", "Decimal string, at most two fraction digits, not negative."
    ),
    MONEY_OUTPUT_SCHEMA,
]
PositiveMoney = Annotated[
    Decimal,
    Field(
        gt=0, max_digits=ORDER_AMOUNT_MAX_DIGITS, decimal_places=ORDER_AMOUNT_DECIMAL_PLACES
    ),
    BeforeValidator(_decimal_string_only),
    AfterValidator(_to_cents),
    _money_input_schema(
        f"^{_NOT_ZERO}{_MONEY_SCHEMA_UNSIGNED}$", "Decimal string, at most two fraction digits, greater than zero."
    ),
    MONEY_OUTPUT_SCHEMA,
]
# An amount in a response: a Decimal with exactly two places, serialized by
# Pydantic as a string ("10.50"). No input rules: rows and cache entries
# written before 4.0.0 are read through it.
OutputMoney = Annotated[Decimal, AfterValidator(_to_cents), MONEY_OUTPUT_SCHEMA]


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
    variant_id: int | None = None
    quantity: int = Field(..., gt=0)
    # Whole cents but, unlike Money, no digit limit: this is read from the DB.
    price_at_time: Annotated[
        Decimal, Field(ge=0, decimal_places=2), AfterValidator(_to_cents), MONEY_OUTPUT_SCHEMA
    ]

class OrderItemInDB(OrderItemBase):
    id: int
    order_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class OrderItemResponse(ResponseBody, OrderItemInDB):
    product_name: str
    product_image: str
    # None for the common no-variant line - only set when this line was for
    # a specific ProductVariant (app/models/product.py).
    variant_sku: str | None = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="before")
    @classmethod
    def populate_product_fields_from_relationship(cls, values):
        # app.models.order_items.OrderItem has no product_name/product_image
        # columns - only product_id and a `product` relationship. Every
        # OrderResponse.model_validate(order) (create_order, get_order,
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
                "product_name": values.product_name,
                "product_image": product.images[0] if product.images else "",
                "variant_sku": values.variant_sku,
            }
        return values

class OrderBase(BaseModel):
    shipping_address_id: int
    delivery_instructions: str | None = None

class OrderInDB(OrderBase):
    id: int
    user_id: int
    status: OrderStatus
    payment_status: PaymentStatus
    total_amount: OutputMoney
    currency: str
    created_at: datetime
    updated_at: datetime
    items: list[OrderItemInDB]

    model_config = ConfigDict(from_attributes=True)

class OrderResponse(ResponseBody, OrderInDB):
    # Read from an order through Order.shipping_address_shown, the address it
    # was placed with; from a dict or JSON by its own name.
    shipping_address: "AddressResponse" = Field(
        validation_alias=AliasChoices("shipping_address_shown", "shipping_address")
    )
    # A narrower item type than OrderInDB's: Pydantic validates each class by
    # its own annotation, mypy sees an incompatible override of a mutable list.
    items: list[OrderItemResponse]  # type: ignore[assignment]
    dispute_status: Annotated[str, published_values(DISPUTE_STATUSES)] | None = Field(
        None,
        description=(
            "Status of the latest payment dispute (chargeback or inquiry) on this order, null when "
            "there is none. needs_response, under_review and the warning_* values except warning_closed "
            "mean the order is on hold; lost means the money went back to the customer through their "
            "bank (the order is then shown as refunded). A lost dispute does not cancel the order or "
            "return its stock. Stripe may add values."
        ),
    )

    model_config = ConfigDict(from_attributes=True)

class OrderListResponse(BaseModel):
    """Schema for list of orders with pagination"""
    items: list[OrderResponse]
    total: int
    page: int
    size: int
    has_more: bool

    model_config = ConfigDict(from_attributes=True)

class PaymentCreate(RequestBody):
    """The body of POST /orders/{order_id}/pay. It names neither the order
    (the path does) nor a payment method type (every charge goes through
    the Stripe provider)."""
    amount: Money = Field(
        description="Must equal the order's total_amount, else 400 (the order is not charged)."
    )
    # Optional: the order is always charged in its own currency
    # (Order.currency). A client may still send the code it expects to pay
    # in; one that differs is rejected with 400, the same as an amount that
    # differs from the order total, rather than ignored, so a client that
    # believes it is paying in another currency finds out before any charge.
    currency: str | None = Field(
        None, pattern="^[A-Z]{3}$",
        description="Optional; if sent it must be the order's currency (STORE_CURRENCY), else 400.",
    )
    # Required: Stripe cannot confirm a PaymentIntent without a payment
    # method. While it was optional, the mock charged a default card and
    # live Stripe refused the confirm (a failed attempt and a generic 400).
    payment_method_token: str = Field(
        min_length=1,
        description="Stripe PaymentMethod id collected by Stripe.js or Elements, for example pm_card_visa.",
    )

class PaymentResponse(ResponseBody):
    order_id: Int32 = Field(description="The order paid: the one in the path.")
    payment_method: Annotated[str, published_values(["stripe"])] = Field(
        description="Always stripe: every charge goes through Stripe."
    )
    amount: Money = Field(description="The amount charged: the order's total_amount.")
    currency: str | None = Field(None, pattern="^[A-Z]{3}$", description="The order's currency.")
    payment_method_token: str | None = Field(
        None,
        description=(
            "The PaymentMethod id sent with this request; null when status is pending "
            "or when the answer repeats an earlier attempt (same Idempotency-Key)."
        ),
    )
    id: int
    status: PaymentStatus = Field(
        description=(
            "paid; requires_action (3D Secure pending: complete it with client_secret and next_action); "
            "pending (accepted, not settled yet: the outcome arrives by webhook); failed (only when the "
            "key of a declined attempt is repeated)."
        )
    )
    created_at: datetime
    transaction_id: str | None = None
    # Client-side secret needed to complete 3D Secure authentication via
    # Stripe.js when status == requires_action; mirrors the real
    # PaymentIntent.client_secret / next_action shape.
    client_secret: str | None = None
    requires_action: bool = False
    next_action: dict[str, Any] | None = None

    model_config = ConfigDict(from_attributes=True)

def _url_origin(url: str) -> tuple[str, str, int] | None:
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


class OrderCreate(RequestBody):
    """Body of POST /orders."""
    shipping_address_id: Int32 = Field(
        description="An active address of the current user; any other id is a 404."
    )

    model_config = ConfigDict(json_schema_extra={"examples": [{"shipping_address_id": 1}]})


class OrderStatusUpdate(RequestBody):
    """Body of PUT /orders/{id}/status."""
    status: OrderStatus = Field(
        description=(
            "The status to move to. Allowed: new to confirmed or cancelled, confirmed to processing "
            "or cancelled, processing to sent or cancelled, sent to delivered or cancelled; "
            "confirmed, processing, sent and delivered need a paid order."
        )
    )


class CheckoutSessionCreate(RequestBody):
    """
    Optional overrides for creating a Stripe Checkout Session for an order
    (the hosted-redirect payment flow - see /orders/{id}/checkout-session).
    Unlike PaymentCreate, the amount is never client-supplied: it is always
    derived from the order's own total_amount server-side, since the entire
    point of this flow is that the client never handles payment details.
    The currency is the order's too; as in PaymentCreate, one sent that
    differs from it is rejected (400).
    """
    currency: str | None = Field(
        None, pattern="^[A-Z]{3}$",
        description="Optional; if sent it must be the order's currency (STORE_CURRENCY), else 400.",
    )
    # Where Stripe sends the customer after the hosted page. Omit to use the
    # frontend's /checkout/success and /checkout/cancel (built on
    # settings.FRONTEND_URL in create_checkout_session).
    success_url: str | None = Field(
        None,
        description=(
            "Where the customer returns after paying. Must be an http(s) URL on the frontend origin "
            "(FRONTEND_URL), else 422; omit it for the frontend's /checkout/success page."
        ),
    )
    cancel_url: str | None = Field(
        None,
        description=(
            "Where the customer returns if they leave. Must be an http(s) URL on the frontend origin "
            "(FRONTEND_URL), else 422; omit it for the frontend's /checkout/cancel page."
        ),
    )

    # The one example valid under any configuration is the empty body: the
    # URLs have to be on FRONTEND_URL and the currency the order's own, which
    # a fixed example cannot know (and the document must not follow a .env).
    model_config = ConfigDict(json_schema_extra={"examples": [{}]})

    # Client-controlled redirect targets, followed by the customer's browser
    # from a Stripe-hosted page they trust, right after paying. Any http(s)
    # URL used to be accepted, which made that page an open redirect to an
    # address of the API caller's choosing. Only the frontend's own origin
    # (scheme, host, port of settings.FRONTEND_URL) is accepted now, 422
    # otherwise. Whitespace, control characters, backslashes and userinfo
    # ("user@host") are refused outright rather than parsed: browsers and
    # urllib disagree on where the host of such a URL is
    # ("http://evil.example\@shop" is evil.example to a browser).
    @field_validator('success_url', 'cancel_url')
    @classmethod
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
    status: Annotated[str, published_values(
        s.value for s in (PaymentStatus.PENDING, PaymentStatus.REQUIRES_ACTION, PaymentStatus.PAID, PaymentStatus.FAILED)
    )]
    amount: ResponseMoney
    currency: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class RefundCreate(RequestBody):
    """Schema for issuing a refund on a paid order."""
    amount: PositiveMoney | None = Field(
        None, description="Amount to refund; omit for a full refund"
    )

class RefundResponse(BaseModel):
    id: str
    order_id: int
    amount: ResponseMoney
    currency: str
    # The refund's status as recorded: Stripe's own value (a replay, or a
    # refund a webhook already reported, says where it is now).
    status: Annotated[str, published_values(REFUND_STATUSES)]
    payment_status: PaymentStatus

    model_config = ConfigDict(from_attributes=True)

class OrderPaymentRefund(ResponseBody):
    id: str
    amount: ResponseMoney
    currency: str
    status: Annotated[str, published_values(REFUND_STATUSES)] = Field(
        description="Stripe's Refund.status as last reported. Stripe documents the set as open and may add values."
    )
    source: Annotated[str, published_values(REFUND_SOURCES)] = Field(
        description="admin: issued through this API; provider: recorded from the provider's report of it."
    )
    failure_reason: str | None = Field(
        None, description="Stripe's Refund.failure_reason of a failed or canceled refund, else null."
    )
    requires_manual_review: bool
    created_at: datetime

class OrderPaymentDispute(ResponseBody):
    id: str
    amount: ResponseMoney
    currency: str
    status: Annotated[str, published_values(DISPUTE_STATUSES)] = Field(
        description=(
            "Stripe's Dispute.status as last reported (won, lost, warning_closed and prevented are "
            "final). Stripe documents the set as open and may add values."
        )
    )
    reason: Annotated[str, published_values(DISPUTE_REASONS)] | None = Field(
        None, description="Stripe's Dispute.reason, null when it was not reported. Stripe may add values."
    )
    closed_at: datetime | None = None
    created_at: datetime

class OrderPayment(ResponseBody):
    """One payment attempt of an order with its refunds and disputes (admin view)."""
    id: int
    status: Annotated[str, published_values(PAYMENT_STATUSES)] = Field(
        description=(
            "processing: the provider is being asked; unknown: it did not answer in time; "
            "requires_action: 3D Secure pending; succeeded; failed (declined, canceled or expired)."
        )
    )
    amount: ResponseMoney
    amount_refunded: ResponseMoney
    currency: str
    transaction_id: str | None = None
    checkout_session_id: str | None = None
    requires_manual_review: bool
    created_at: datetime
    refunds: list[OrderPaymentRefund]
    disputes: list[OrderPaymentDispute]

class OrderPaymentList(BaseModel):
    """Every payment attempt of an order. Not paginated: an order has few."""
    items: list[OrderPayment]

class StripeWebhookAck(BaseModel):
    """What Stripe is told about an event it delivered."""
    status: Literal["received", "already_processed"]

# Circular imports are resolved at runtime
from app.schemas.address import AddressResponse  # noqa: E402
