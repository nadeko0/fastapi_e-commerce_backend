"""
PaymentProvider abstraction. Structurally close to the real `stripe` Python
SDK's top-level resource methods (PaymentIntent.create/confirm/retrieve,
Refund.create, Webhook.construct_event) so that swapping the mock provider
for the real `stripe` SDK later is mostly a matter of writing a thin adapter,
not redesigning the call sites in app/api/v1/orders.py.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from app.services.payment.types import PaymentIntent, Refund, WebhookEventData


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
    ) -> PaymentIntent:
        """Mirrors stripe.PaymentIntent.confirm(id, ...)."""
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
