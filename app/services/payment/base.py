"""
PaymentProvider abstraction. Structurally close to the real `stripe` Python
SDK's top-level resource methods (PaymentIntent.create/confirm/retrieve,
Refund.create, Webhook.construct_event) so that swapping the mock provider
for the real `stripe` SDK later is mostly a matter of writing a thin adapter,
not redesigning the call sites in app/api/v1/orders.py.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from app.services.payment.types import (
    CheckoutSession,
    PaymentIntent,
    Refund,
    WebhookEventData,
)


class PaymentProvider(ABC):
    @abstractmethod
    def create_payment_intent(
        self,
        *,
        amount: int,
        currency: str,
        idempotency_key: str,
        payment_method: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentIntent:
        """Mirrors stripe.PaymentIntent.create(...)."""
        raise NotImplementedError

    @abstractmethod
    def confirm_payment_intent(
        self,
        payment_intent_id: str,
        *,
        idempotency_key: Optional[str] = None,
        return_url: Optional[str] = None,
    ) -> PaymentIntent:
        """Mirrors stripe.PaymentIntent.confirm(id, ...).

        return_url is required by Stripe whenever a confirmation could
        produce a redirect-based next_action (e.g. 3D Secure), even for a
        plain card payment method - see
        https://docs.stripe.com/payments/3d-secure/authentication-flow.
        """
        raise NotImplementedError

    @abstractmethod
    def retrieve_payment_intent(self, payment_intent_id: str) -> PaymentIntent:
        """Mirrors stripe.PaymentIntent.retrieve(id)."""
        raise NotImplementedError

    @abstractmethod
    def create_refund(
        self,
        *,
        payment_intent_id: str,
        amount: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> Refund:
        """Mirrors stripe.Refund.create(payment_intent=..., amount=...).
        amount=None means a full refund of whatever has not yet been refunded."""
        raise NotImplementedError

    # -- Checkout Session ---------------------------------------------------
    # A second, independent payment flow: instead of confirming a
    # PaymentIntent server-side with a client-collected payment_method_token
    # (create_payment_intent/confirm_payment_intent above), the caller
    # redirects the customer to Stripe's own hosted page (`url`, below) and
    # Stripe handles card entry/3DS/etc entirely on its side, reporting the
    # result back asynchronously via webhook events (checkout.session.*).

    @abstractmethod
    def create_checkout_session(
        self,
        *,
        amount: int,
        currency: str,
        idempotency_key: str,
        success_url: str,
        cancel_url: str,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CheckoutSession:
        """Mirrors stripe.checkout.Session.create(mode="payment", line_items=[...],
        success_url=..., cancel_url=..., ...). success_url/cancel_url are
        mandatory Stripe-side params for a hosted Checkout Session - Stripe
        has no way to send the customer anywhere on completion/cancellation
        otherwise (see https://docs.stripe.com/api/checkout/sessions/create).
        """
        raise NotImplementedError

    @abstractmethod
    def retrieve_checkout_session(self, session_id: str) -> CheckoutSession:
        """Mirrors stripe.checkout.Session.retrieve(id)."""
        raise NotImplementedError

    @abstractmethod
    def expire_checkout_session(self, session_id: str) -> CheckoutSession:
        """Mirrors stripe.checkout.Session.expire(id) - forces an open
        Session to expired immediately rather than waiting for its natural
        expires_at."""
        raise NotImplementedError

    @abstractmethod
    def construct_webhook_event(
        self,
        payload: bytes,
        sig_header: Optional[str],
        webhook_secret: str,
        tolerance_seconds: int = 300,
    ) -> WebhookEventData:
        """Mirrors stripe.Webhook.construct_event(payload, sig_header, secret).
        Must verify the signature and raise SignatureVerificationError on
        any mismatch, missing header, or expired timestamp."""
        raise NotImplementedError
