from app.core.config import settings
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
    CheckoutSession,
    CheckoutSessionStatus,
    PaymentIntent,
    PaymentIntentStatus,
    Refund,
    RefundStatus,
    WebhookEventData,
)


def get_payment_provider() -> PaymentProvider:
    """FastAPI dependency factory: the provider named by
    settings.PAYMENT_PROVIDER. It used to return the mock unconditionally,
    so a deployment marked orders paid without taking any money.

    The live provider is imported here, not at module level, so the mock
    (tests, local development) never loads the Stripe SDK."""
    if settings.PAYMENT_PROVIDER == "stripe":
        from app.services.payment.live_stripe_provider import LiveStripePaymentProvider

        return LiveStripePaymentProvider(api_key=settings.STRIPE_SECRET_KEY.get_secret_value())
    return StripePaymentProvider()


__all__ = [
    "PaymentProvider",
    "StripePaymentProvider",
    "get_payment_provider",
    "PaymentIntent",
    "PaymentIntentStatus",
    "Refund",
    "RefundStatus",
    "CheckoutSession",
    "CheckoutSessionStatus",
    "WebhookEventData",
    "PaymentProviderError",
    "CardError",
    "IdempotencyError",
    "SignatureVerificationError",
    "PaymentProviderTimeoutError",
]
