"""
PaymentProvider abstraction. Structurally close to the real `stripe` Python
SDK's top-level resource methods (PaymentIntent.create/confirm/retrieve,
Refund.create, Webhook.construct_event) so that swapping the mock provider
for the real `stripe` SDK later is mostly a matter of writing a thin adapter,
not redesigning the call sites in app/api/v1/orders.py.
"""
import json
import uuid
from abc import ABC, abstractmethod
from typing import Any

from app.services.payment.exceptions import SignatureVerificationError
from app.services.payment.types import (
    CheckoutSession,
    PaymentIntent,
    Refund,
    WebhookEventData,
)


def webhook_event_from_payload(
    payload: bytes, sig_header: str | None, signed_at: int
) -> WebhookEventData:
    """The event in a webhook body, read the same way by both providers once
    its signature is verified. A body that is not a UTF-8 JSON object is
    refused as SignatureVerificationError (400 at the webhook route), not
    left to raise whatever json or the SDK raises (a 500): the live provider
    let UnicodeDecodeError, JSONDecodeError and AttributeError through, the
    mock the last one. Missing fields get the same defaults in both; Stripe
    always sends them."""
    try:
        body = json.loads(payload.decode("utf-8"))
    except ValueError:  # UnicodeDecodeError and JSONDecodeError are both ValueErrors
        raise SignatureVerificationError("Payload is not valid JSON", sig_header)
    if not isinstance(body, dict):
        raise SignatureVerificationError("Payload is not a JSON object", sig_header)
    return WebhookEventData(
        id=body.get("id") or f"evt_{uuid.uuid4().hex[:24]}",
        type=body.get("type", ""),
        data=body.get("data", {}),
        created=body.get("created", signed_at),
    )


class PaymentProvider(ABC):
    @abstractmethod
    def create_payment_intent(
        self,
        *,
        amount: int,
        currency: str,
        idempotency_key: str,
        payment_method: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PaymentIntent:
        """Mirrors stripe.PaymentIntent.create(...)."""
        raise NotImplementedError

    @abstractmethod
    def confirm_payment_intent(
        self,
        payment_intent_id: str,
        *,
        idempotency_key: str | None = None,
        return_url: str,
    ) -> PaymentIntent:
        """Mirrors stripe.PaymentIntent.confirm(id, ...).

        return_url is required by Stripe whenever a confirmation could
        produce a redirect-based next_action (e.g. 3D Secure), even for a
        plain card payment method - see
        https://docs.stripe.com/payments/3d-secure/authentication-flow.
        It is where the customer lands after authenticating, so the caller
        must always give it: no provider has a fallback of its own.
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
        amount: int | None = None,
        idempotency_key: str | None = None,
    ) -> Refund:
        """Mirrors stripe.Refund.create(payment_intent=..., amount=...).
        amount=None means a full refund of whatever has not yet been refunded."""
        raise NotImplementedError

    @abstractmethod
    def list_refunds(self, payment_intent_id: str) -> list[Refund]:
        """Mirrors stripe.Refund.list(payment_intent=...): every refund of the
        PaymentIntent, whatever its status, each with `provider_status` set.
        A PaymentIntent with none gives []. Read only; used by the daily
        reconciliation (app/services/reconciliation.py)."""
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
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
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
        sig_header: str | None,
        webhook_secret: str,
        tolerance_seconds: int = 300,
    ) -> WebhookEventData:
        """Mirrors stripe.Webhook.construct_event(payload, sig_header, secret).
        Must verify the signature and raise SignatureVerificationError on
        any mismatch, missing header, or expired timestamp."""
        raise NotImplementedError
