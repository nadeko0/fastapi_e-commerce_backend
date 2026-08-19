"""
Small data classes mirroring the shape of the real Stripe SDK's resource
objects (stripe.PaymentIntent, stripe.Refund, stripe.Event). Amounts are
always integers in the smallest currency unit (cents for USD/EUR), matching
Stripe's actual API - never Decimal dollars. Converting to/from Decimal
dollars is the caller's (app/api/v1/orders.py's) responsibility, done once
at the API boundary.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class PaymentIntentStatus(str, Enum):
    """Mirrors the real PaymentIntent.status lifecycle (subset we model)."""

    REQUIRES_PAYMENT_METHOD = "requires_payment_method"
    REQUIRES_CONFIRMATION = "requires_confirmation"
    REQUIRES_ACTION = "requires_action"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    CANCELED = "canceled"


class RefundStatus(str, Enum):
    SUCCEEDED = "succeeded"
    PENDING = "pending"
    FAILED = "failed"


class CheckoutSessionStatus(str, Enum):
    """Mirrors the real checkout.Session.status lifecycle - see
    https://docs.stripe.com/api/checkout/sessions/object."""

    OPEN = "open"
    COMPLETE = "complete"
    EXPIRED = "expired"


@dataclass
class PaymentIntent:
    id: str
    amount: int  # smallest currency unit (e.g. cents)
    currency: str
    status: PaymentIntentStatus
    client_secret: str
    payment_method: Optional[str] = None
    idempotency_key: Optional[str] = None
    last_payment_error: Optional[Dict[str, Any]] = None
    next_action: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    amount_refunded: int = 0


@dataclass
class Refund:
    id: str
    payment_intent: str
    amount: int  # smallest currency unit
    currency: str
    status: RefundStatus


@dataclass
class CheckoutSession:
    """Mirrors stripe.checkout.Session - the fully-hosted-redirect payment
    flow (distinct from PaymentIntent's server-side confirm flow above).
    `payment_intent` is None until Stripe attaches one, which does not
    happen at creation time (a fresh Session's payment_intent is null) -
    only once the customer actually completes the hosted page, at which
    point it is a real PaymentIntent id."""

    id: str
    url: Optional[str]
    status: CheckoutSessionStatus
    # Mirrors Stripe's own "paid" / "unpaid" / "no_payment_required" string
    # verbatim rather than a closed enum - Stripe documents this as an open
    # set of values, unlike `status`.
    payment_status: str
    amount_total: int  # smallest currency unit (e.g. cents)
    currency: str
    payment_intent: Optional[str] = None
    idempotency_key: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WebhookEventData:
    """Mirrors stripe.Event - the deserialized, signature-verified body of a webhook."""

    id: str
    type: str
    data: Dict[str, Any]
    created: int
