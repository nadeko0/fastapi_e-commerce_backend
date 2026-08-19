"""
StripePaymentProvider - a mock Stripe integration.

This project has no real Stripe account and never will (portfolio project).
This class mirrors the real `stripe` Python SDK's call shapes as closely as
reasonable (PaymentIntent.create/confirm/retrieve, Refund.create,
Webhook.construct_event) so it demonstrates how a real integration would be
structured, but the outbound "network call" (`_simulate_network_call`) is a
no-op stub - no HTTP request is ever made, and no `stripe` PyPI package is
required. Tests monkeypatch `_simulate_network_call` to inject a transport
failure (see tests/test_payment.py's timeout test).

Deterministic mock outcomes are selected by a small set of sentinel
`payment_method` tokens, deliberately named after Stripe's own published
test card tokens (pm_card_visa, pm_card_chargeDeclined,
pm_card_authenticationRequired) so the mock reads like real Stripe test-mode
usage: https://docs.stripe.com/testing
"""
import hashlib
import hmac
import json
import time
import uuid
from typing import Any, Dict, Optional

from app.services.payment.base import PaymentProvider
from app.services.payment.exceptions import (
    CardError,
    IdempotencyError,
    InvalidRequestError,
    SignatureVerificationError,
)
from app.services.payment.types import (
    PaymentIntent,
    PaymentIntentStatus,
    Refund,
    RefundStatus,
    WebhookEventData,
)

# Sentinel payment_method tokens that pick a deterministic mock outcome,
# named after Stripe's real test-mode tokens for familiarity.
TOKEN_DECLINE = "pm_card_chargeDeclined"
TOKEN_REQUIRES_ACTION = "pm_card_authenticationRequired"
TOKEN_SUCCESS_DEFAULT = "pm_card_visa"

DEFAULT_WEBHOOK_TOLERANCE_SECONDS = 300

# Real Stripe amounts are always a non-negative integer number of the
# smallest currency unit and are rejected outright (InvalidRequestError) if
# they are a float/Decimal/string, negative, or zero (a $0 PaymentIntent is
# not chargeable - see https://docs.stripe.com/api/payment_intents/create).
# We additionally reject anything implausibly large as a sanity ceiling
# against integer-overflow-adjacent input; this is not Stripe's exact
# per-currency maximum (which varies), just a conservative guard.
MAX_AMOUNT = 999_999_999_99  # 999,999,999.99 in major units

# A representative subset of real ISO 4217 currency codes Stripe supports,
# used to reject arbitrary/typo'd currency strings ("XYZ", "usd1", "dollars")
# rather than silently accepting anything matching a 3-letter shape. Not
# exhaustive (Stripe supports ~135 currencies) but enough to catch the
# common real-world mistake of a made-up or malformed currency code slipping
# past a naive regex-only check.
VALID_CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY", "SEK", "NZD",
    "MXN", "SGD", "HKD", "NOK", "KRW", "TRY", "RUB", "INR", "BRL", "ZAR",
    "DKK", "PLN", "THB", "IDR", "HUF", "CZK", "ILS", "CLP", "PHP", "AED",
    "COP", "SAR", "MYR", "RON",
}


def _require_valid_amount(amount: Any) -> None:
    """Mirrors real Stripe's validation of the `amount` parameter: must be a
    plain int (never bool, float, Decimal, or numeric string - those are the
    classic "cents vs dollars"/float-rounding integration mistakes), a
    positive number of the smallest currency unit, and below our sanity
    ceiling."""
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise InvalidRequestError(
            f"Invalid integer: amount must be an int in the smallest currency "
            f"unit (e.g. cents), got {amount!r} ({type(amount).__name__})",
            param="amount",
            code="parameter_invalid_integer",
        )
    if amount <= 0:
        raise InvalidRequestError(
            f"Invalid positive integer: amount must be > 0, got {amount}",
            param="amount",
            code="parameter_invalid_integer",
        )
    if amount > MAX_AMOUNT:
        raise InvalidRequestError(
            f"Amount {amount} exceeds the maximum allowed amount ({MAX_AMOUNT})",
            param="amount",
            code="parameter_invalid_integer",
        )


def _require_valid_currency(currency: Any) -> None:
    """Rejects anything that is not a real, known 3-letter ISO 4217 currency
    code - not just anything matching a `^[A-Z]{3}$`-shaped string."""
    if not isinstance(currency, str) or currency.upper() not in VALID_CURRENCIES:
        raise InvalidRequestError(
            f"Invalid currency: {currency!r} is not a supported ISO 4217 "
            "currency code",
            param="currency",
            code="parameter_invalid_string",
        )


class StripePaymentProvider(PaymentProvider):
    """
    Idempotency-key cache is intentionally a *class* attribute, not an
    instance attribute: on the real Stripe API, idempotency dedup happens on
    Stripe's servers (shared/global), not in our process. A fresh
    StripePaymentProvider() per request (the FastAPI Depends() pattern used
    elsewhere in this codebase, e.g. RedisService) must still see keys
    created by a previous request/instance.

    Tests must call `StripePaymentProvider.reset_mock_state()` between test
    cases to avoid idempotency keys leaking across tests.
    """

    _intents_by_key: Dict[str, PaymentIntent] = {}
    _intents_by_id: Dict[str, PaymentIntent] = {}

    @classmethod
    def reset_mock_state(cls) -> None:
        cls._intents_by_key.clear()
        cls._intents_by_id.clear()

    def _simulate_network_call(self) -> None:
        """No-op hook standing in for the real HTTP call to api.stripe.com.
        Tests monkeypatch this to raise PaymentProviderTimeoutError."""
        return None

    # -- PaymentIntent -----------------------------------------------------

    def create_payment_intent(
        self,
        *,
        amount: int,
        currency: str,
        idempotency_key: str,
        payment_method: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentIntent:
        # Validate before the (simulated) network call and before touching
        # the idempotency cache: a structurally invalid request was never
        # "sent to the processor" and must not consume/pollute an
        # idempotency key or return a cached response.
        _require_valid_amount(amount)
        _require_valid_currency(currency)

        self._simulate_network_call()

        cached = self._intents_by_key.get(idempotency_key)
        if cached is not None:
            # Same key: real Stripe returns the original response as-is if
            # parameters match, or raises IdempotencyError if they differ.
            if cached.amount != amount or cached.currency != currency:
                raise IdempotencyError(
                    f"Idempotency key '{idempotency_key}' has already been used "
                    "with different request parameters"
                )
            return cached

        intent = PaymentIntent(
            id=f"pi_{uuid.uuid4().hex[:24]}",
            amount=amount,
            currency=currency,
            status=PaymentIntentStatus.REQUIRES_CONFIRMATION,
            client_secret=f"pi_{uuid.uuid4().hex[:24]}_secret_{uuid.uuid4().hex[:16]}",
            payment_method=payment_method,
            idempotency_key=idempotency_key,
            metadata=dict(metadata or {}),
        )
        self._intents_by_key[idempotency_key] = intent
        self._intents_by_id[intent.id] = intent
        return intent

    def confirm_payment_intent(
        self,
        payment_intent_id: str,
        *,
        idempotency_key: Optional[str] = None,
    ) -> PaymentIntent:
        self._simulate_network_call()

        intent = self._intents_by_id.get(payment_intent_id)
        if intent is None:
            raise CardError(
                f"No such payment_intent: '{payment_intent_id}'", code="resource_missing"
            )

        if intent.status == PaymentIntentStatus.SUCCEEDED:
            # Already confirmed - confirming again is a no-op that returns
            # the same intent (defends against a client retrying a confirm
            # call whose response it never received).
            return intent

        if intent.status == PaymentIntentStatus.REQUIRES_ACTION:
            # Simulates the customer having completed 3D Secure
            # authentication out-of-band; a follow-up confirm finalizes it.
            intent.status = PaymentIntentStatus.SUCCEEDED
            intent.next_action = None
            return intent

        token = intent.payment_method or TOKEN_SUCCESS_DEFAULT
        if token == TOKEN_DECLINE:
            intent.status = PaymentIntentStatus.REQUIRES_PAYMENT_METHOD
            intent.last_payment_error = {
                "code": "card_declined",
                "decline_code": "generic_decline",
                "message": "Your card was declined.",
            }
            raise CardError(
                "Your card was declined.", code="card_declined", decline_code="generic_decline"
            )
        if token == TOKEN_REQUIRES_ACTION:
            intent.status = PaymentIntentStatus.REQUIRES_ACTION
            intent.next_action = {
                "type": "use_stripe_sdk",
                "use_stripe_sdk": {"type": "three_d_secure_redirect"},
            }
            return intent

        intent.status = PaymentIntentStatus.SUCCEEDED
        return intent

    def retrieve_payment_intent(self, payment_intent_id: str) -> PaymentIntent:
        self._simulate_network_call()
        intent = self._intents_by_id.get(payment_intent_id)
        if intent is None:
            raise CardError(
                f"No such payment_intent: '{payment_intent_id}'", code="resource_missing"
            )
        return intent

    # -- Refund --------------------------------------------------------

    def create_refund(
        self,
        *,
        payment_intent_id: str,
        amount: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> Refund:
        if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
            raise InvalidRequestError(
                f"Invalid integer: amount must be an int in the smallest currency "
                f"unit (e.g. cents), got {amount!r} ({type(amount).__name__})",
                param="amount",
                code="parameter_invalid_integer",
            )

        self._simulate_network_call()

        intent = self._intents_by_id.get(payment_intent_id)
        if intent is None:
            raise CardError(
                f"No such payment_intent: '{payment_intent_id}'", code="resource_missing"
            )
        if intent.status != PaymentIntentStatus.SUCCEEDED:
            raise CardError(
                "Only a succeeded payment_intent can be refunded", code="invalid_request"
            )

        refundable = intent.amount - intent.amount_refunded
        refund_amount = amount if amount is not None else refundable
        if refund_amount <= 0 or refund_amount > refundable:
            raise CardError(
                f"Refund amount ({refund_amount}) exceeds the refundable amount "
                f"({refundable})",
                code="invalid_request",
            )

        intent.amount_refunded += refund_amount
        return Refund(
            id=f"re_{uuid.uuid4().hex[:24]}",
            payment_intent=payment_intent_id,
            amount=refund_amount,
            currency=intent.currency,
            status=RefundStatus.SUCCEEDED,
        )

    # -- Webhooks --------------------------------------------------------

    @staticmethod
    def sign_payload(payload: bytes, secret: str, timestamp: Optional[int] = None) -> str:
        """
        Builds a Stripe-Signature header value the same way the real Stripe
        API does: HMAC-SHA256 over "{timestamp}.{payload}", formatted as
        "t={timestamp},v1={signature}". Used by tests to build a validly
        signed mock webhook request body.
        """
        ts = timestamp if timestamp is not None else int(time.time())
        signed_payload = f"{ts}.".encode() + payload
        signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
        return f"t={ts},v1={signature}"

    def construct_webhook_event(
        self,
        payload: bytes,
        sig_header: Optional[str],
        webhook_secret: str,
        tolerance_seconds: int = DEFAULT_WEBHOOK_TOLERANCE_SECONDS,
    ) -> WebhookEventData:
        if not sig_header:
            raise SignatureVerificationError("Missing Stripe-Signature header")

        parts: Dict[str, str] = {}
        for item in sig_header.split(","):
            if "=" not in item:
                continue
            key, _, value = item.partition("=")
            parts.setdefault(key.strip(), value.strip())

        timestamp_raw = parts.get("t")
        signature = parts.get("v1")
        if not timestamp_raw or not signature:
            raise SignatureVerificationError(
                "Unable to extract timestamp and signature from header", sig_header
            )

        try:
            timestamp = int(timestamp_raw)
        except ValueError:
            raise SignatureVerificationError("Invalid timestamp in signature header", sig_header)

        expected = self.sign_payload(payload, webhook_secret, timestamp=timestamp).split(
            ",v1="
        )[1]
        if not hmac.compare_digest(expected, signature):
            raise SignatureVerificationError(
                "Signature does not match the expected value for this payload", sig_header
            )

        if abs(time.time() - timestamp) > tolerance_seconds:
            # Defends against replay attacks: an old, previously-valid,
            # captured request being resubmitted long after the fact.
            raise SignatureVerificationError(
                "Timestamp outside the tolerance zone", sig_header
            )

        try:
            body = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise SignatureVerificationError("Payload is not valid JSON", sig_header)

        event_id = body.get("id") or f"evt_{uuid.uuid4().hex[:24]}"
        event_type = body.get("type", "")
        data = body.get("data", {})
        created = body.get("created", timestamp)
        return WebhookEventData(id=event_id, type=event_type, data=data, created=created)
