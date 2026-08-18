from app.services.payment.base import PaymentProvider
from app.services.payment.exceptions import (
    CardError,
    IdempotencyError,
    PaymentProviderError,
    PaymentProviderTimeoutError,
    SignatureVerificationError,
)
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.payment.types import (
    PaymentIntent,
    PaymentIntentStatus,
    Refund,
    RefundStatus,
    WebhookEventData,
)


def get_payment_provider() -> PaymentProvider:
    """FastAPI dependency factory - swap this to switch payment providers."""
    return StripePaymentProvider()


__all__ = [
    "PaymentProvider",
    "StripePaymentProvider",
    "get_payment_provider",
    "PaymentIntent",
    "PaymentIntentStatus",
    "Refund",
    "RefundStatus",
    "WebhookEventData",
    "PaymentProviderError",
    "CardError",
    "IdempotencyError",
    "SignatureVerificationError",
    "PaymentProviderTimeoutError",
]
