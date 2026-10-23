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
test card tokens (pm_card_visa, pm_card_visa_chargeDeclined,
pm_card_authenticationRequired) so the mock reads like real Stripe test-mode
usage: https://docs.stripe.com/testing
"""
import hashlib
import hmac
import time
import uuid
from typing import Any, ClassVar

from app.core.currencies import STRIPE_PRESENTMENT_CURRENCIES
from app.services.payment.base import PaymentProvider, webhook_event_from_payload
from app.services.payment.exceptions import (
    CardError,
    IdempotencyError,
    InvalidRequestError,
    SignatureVerificationError,
)
from app.services.payment.types import (
    CheckoutSession,
    CheckoutSessionStatus,
    PaymentIntent,
    PaymentIntentStatus,
    Refund,
    RefundStatus,
    WebhookEventData,
)

# The tokens tests use most, named after Stripe's test cards (see the tables
# below for all of them). Stripe's generic decline card is
# pm_card_visa_chargeDeclined; the mock used to know only
# pm_card_chargeDeclined, which is not a Stripe test card, and charged
# Stripe's own decline card successfully.
TOKEN_DECLINE = "pm_card_visa_chargeDeclined"
TOKEN_REQUIRES_ACTION = "pm_card_authenticationRequired"
TOKEN_SUCCESS_DEFAULT = "pm_card_visa"
TOKEN_PENDING_REFUND = "pm_card_pendingRefund"
TOKEN_REFUND_FAIL = "pm_card_refundFail"

# -- Stripe's test PaymentMethods -----------------------------------------------
#
# The mock answers the test PaymentMethods of https://docs.stripe.com/testing
# (read in October 2026) the way that page says Stripe does, keyed by the same
# pm_ ids, so a test reads like Stripe's guide. Error codes, decline codes and
# statuses are the guide's; the messages are the mock's own wording.

# Cards whose charge succeeds without asking for anything ("Cards by brand",
# co-branded, "Cards by country", the 3D Secure "Supported" and "Not
# supported" rows, and the asynchronous-refund cards). Any token not named in
# the tables below behaves the same - a PaymentMethod id from Stripe.js in
# test mode too - so this set is the documented list, not a gate.
SUCCEEDING_TOKENS = frozenset({
    "pm_card_visa", "pm_card_visa_debit", "pm_card_mastercard", "pm_card_mastercard_debit",
    "pm_card_mastercard_prepaid", "pm_card_amex", "pm_card_discover", "pm_card_diners",
    "pm_card_jcb", "pm_card_unionpay", "pm_card_visa_cartesBancaires",
    "pm_card_mastercard_cartesBancaires", "pm_card_visa_debit_eftposAuCoBranded",
    "pm_card_mastercard_debit_eftposAuCoBranded",
    "pm_card_us", "pm_card_ar", "pm_card_br", "pm_card_ca", "pm_card_cl", "pm_card_co",
    "pm_card_cr", "pm_card_ec", "pm_card_mx", "pm_card_pa", "pm_card_py", "pm_card_pe",
    "pm_card_uy", "pm_card_ae", "pm_card_ae_mastercard", "pm_card_at", "pm_card_be",
    "pm_card_bg", "pm_card_by", "pm_card_hr", "pm_card_cy", "pm_card_cz", "pm_card_dk",
    "pm_card_ee", "pm_card_fi", "pm_card_fr", "pm_card_de", "pm_card_gi", "pm_card_gr",
    "pm_card_hu", "pm_card_ie", "pm_card_it", "pm_card_lv", "pm_card_li", "pm_card_lt",
    "pm_card_lu", "pm_card_mt", "pm_card_nl", "pm_card_no", "pm_card_pl", "pm_card_pt",
    "pm_card_ro", "pm_card_si", "pm_card_sk", "pm_card_es", "pm_card_se", "pm_card_ch",
    "pm_card_gb", "pm_card_gb_debit", "pm_card_gb_mastercard", "pm_card_au", "pm_card_cn",
    "pm_card_hk", "pm_card_in", "pm_card_jp", "pm_card_my", "pm_card_nz", "pm_card_sg",
    "pm_card_tw", "pm_card_th_credit", "pm_card_th_debit",
    "pm_card_threeDSecureOptional", "pm_card_amex_threeDSecureNotSupported",
    "pm_card_pendingRefund", "pm_card_refundFail",
})

_DECLINED = "Your card was declined."

# Cards whose confirmation is declined: token -> (error code, decline code,
# message). "Declined payments" table; decline code None where the guide
# gives none.
DECLINING_TOKENS: dict[str, tuple[str, str | None, str]] = {
    "pm_card_visa_chargeDeclined": ("card_declined", "generic_decline", _DECLINED),
    "pm_card_visa_chargeDeclinedInsufficientFunds": (
        "card_declined", "insufficient_funds", "Your card has insufficient funds."),
    "pm_card_visa_chargeDeclinedLostCard": ("card_declined", "lost_card", _DECLINED),
    "pm_card_visa_chargeDeclinedStolenCard": ("card_declined", "stolen_card", _DECLINED),
    "pm_card_chargeDeclinedExpiredCard": ("expired_card", None, "Your card has expired."),
    "pm_card_chargeDeclinedIncorrectCvc": (
        "incorrect_cvc", None, "Your card's security code is incorrect."),
    "pm_card_chargeDeclinedProcessingError": (
        "processing_error", None, "An error occurred while processing your card."),
    "pm_card_visa_chargeDeclinedVelocityLimitExceeded": (
        "card_declined", "card_velocity_exceeded", _DECLINED),
    # "Blocked payments return card errors with an error code of fraud"
    # (Fraud prevention). Stripe's error-code list has no "fraud"; its
    # decline-code list has "fraudulent" ("declined because Stripe suspects
    # that it's fraudulent"), which is what the mock answers, under the
    # card_declined code every decline carries.
    "pm_card_radarBlock": ("card_declined", "fraudulent", _DECLINED),
    # 3D Secure "Required | Error": the 3D Secure lookup fails and "payments
    # are declined with a card_declined failure code" - at confirmation,
    # before any authentication.
    "pm_card_threeDSecureRequiredProcessingError": (
        "card_declined", "processing_error", _DECLINED),
    # Not a Stripe test card (Stripe's generic decline is
    # pm_card_visa_chargeDeclined, above): this mock's own decline token
    # before it followed the guide, kept so callers written against it still
    # get a decline. Against Stripe it does not exist.
    "pm_card_chargeDeclined": ("card_declined", "generic_decline", _DECLINED),
}

# Cards that need 3D Secure: confirming returns requires_action; once the
# customer has authenticated (a second confirm, in the mock) the payment
# succeeds (None) or is declined with the given (code, decline code,
# message). "3D Secure" table and "Always authenticate".
AUTHENTICATING_TOKENS: dict[str, tuple[str, str | None, str] | None] = {
    "pm_card_authenticationRequired": None,
    "pm_card_threeDSecure2Required": None,
    # "declined with a card_declined failure code after authentication";
    # generic_decline as the decline code, the guide giving none.
    "pm_card_threeDSecureRequiredChargeDeclined": ("card_declined", "generic_decline", _DECLINED),
}

# Asynchronous refunds: token -> (status a refund starts in, status it ends
# in "some time later", the event Stripe sends then). "Refunds" table.
ASYNC_REFUND_TOKENS: dict[str, tuple[RefundStatus, RefundStatus, str]] = {
    "pm_card_pendingRefund": (RefundStatus.PENDING, RefundStatus.SUCCEEDED, "refund.updated"),
    "pm_card_refundFail": (RefundStatus.SUCCEEDED, RefundStatus.FAILED, "refund.failed"),
}

DEFAULT_WEBHOOK_TOLERANCE_SECONDS = 300

# Real Stripe amounts are always a non-negative integer number of the
# smallest currency unit and are rejected outright (InvalidRequestError) if
# they are a float/Decimal/string, negative, or zero (a $0 PaymentIntent is
# not chargeable - see https://docs.stripe.com/api/payment_intents/create).
# We additionally reject anything implausibly large as a sanity ceiling
# against integer-overflow-adjacent input; this is not Stripe's exact
# per-currency maximum (which varies), just a conservative guard.
MAX_AMOUNT = 999_999_999_99  # 999,999,999.99 in major units

# Every currency Stripe takes for payments: the presentment currencies
# https://docs.stripe.com/currencies lists for any account country (see
# app/core/currencies.py). It used to be a 38-code subset, while the
# configuration accepts every two-decimal one of them: a KES or AMD store
# started and the mock refused every payment. Anything else ("XYZ",
# "usd1", "dollars") is still refused rather than accepted for being
# three letters long.
VALID_CURRENCIES = STRIPE_PRESENTMENT_CURRENCIES


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

    _intents_by_key: ClassVar[dict[str, PaymentIntent]] = {}
    _intents_by_id: ClassVar[dict[str, PaymentIntent]] = {}
    _sessions_by_key: ClassVar[dict[str, CheckoutSession]] = {}
    _sessions_by_id: ClassVar[dict[str, CheckoutSession]] = {}
    _refunds_by_key: ClassVar[dict[str, Refund]] = {}
    _refunds_by_id: ClassVar[dict[str, Refund]] = {}

    @classmethod
    def reset_mock_state(cls) -> None:
        cls._intents_by_key.clear()
        cls._intents_by_id.clear()
        cls._sessions_by_key.clear()
        cls._sessions_by_id.clear()
        cls._refunds_by_key.clear()
        cls._refunds_by_id.clear()

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
        payment_method: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PaymentIntent:
        # Validate before the (simulated) network call and before touching
        # the idempotency cache: a structurally invalid request was never
        # "sent to the processor" and must not consume/pollute an
        # idempotency key or return a cached response.
        _require_valid_amount(amount)
        _require_valid_currency(currency)
        # Reported in upper case whatever case it was sent in, like every
        # currency in this API (orders, and the live provider, which
        # upper-cases Stripe's lower-case answers).
        currency = currency.upper()

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
        idempotency_key: str | None = None,
        return_url: str,
    ) -> PaymentIntent:
        self._simulate_network_call()

        # Real Stripe ties an idempotency key to the first request it was
        # sent with, endpoint included: a key already used to *create* a
        # PaymentIntent cannot be reused to *confirm* one. Mirrored here
        # because the mock used to accept it, which hid a bug that made
        # every /pay fail against the real API (see app/api/v1/orders.py).
        if idempotency_key is not None and idempotency_key in self._intents_by_key:
            raise IdempotencyError(
                f"Idempotency key '{idempotency_key}' has already been used "
                "with different request parameters"
            )

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
            # authentication out-of-band; a follow-up confirm finalizes it:
            # the payment succeeds, or is declined for a card the guide
            # declines after authentication.
            intent.next_action = None
            after_authentication = AUTHENTICATING_TOKENS.get(intent.payment_method)
            if after_authentication is not None:
                self._decline(intent, *after_authentication)
            intent.status = PaymentIntentStatus.SUCCEEDED
            intent.amount_received = intent.amount
            return intent

        token = intent.payment_method or TOKEN_SUCCESS_DEFAULT
        if token in DECLINING_TOKENS:
            self._decline(intent, *DECLINING_TOKENS[token])
        if token in AUTHENTICATING_TOKENS:
            intent.status = PaymentIntentStatus.REQUIRES_ACTION
            # The shape Stripe returns when a return_url is confirmed with
            # (seen by tests/integration/test_payment_live_stripe.py against
            # Stripe's test mode): a redirect to the authentication page,
            # which sends the customer back to return_url. The mock used to
            # answer "use_stripe_sdk" and drop the return_url.
            intent.next_action = {
                "type": "redirect_to_url",
                "redirect_to_url": {
                    "url": f"https://checkout.stripe.com/mock/authenticate/{intent.id}",
                    "return_url": return_url,
                },
            }
            return intent

        intent.status = PaymentIntentStatus.SUCCEEDED
        intent.amount_received = intent.amount
        return intent

    @staticmethod
    def _decline(
        intent: PaymentIntent, code: str, decline_code: str | None, message: str
    ) -> None:
        """Declines `intent` as Stripe does: the intent stays, waiting for
        another payment method, with the error recorded on it, and the
        confirm call raises a card error."""
        intent.status = PaymentIntentStatus.REQUIRES_PAYMENT_METHOD
        intent.last_payment_error = {
            "code": code,
            "decline_code": decline_code,
            "message": message,
        }
        raise CardError(message, code=code, decline_code=decline_code)

    def retrieve_payment_intent(self, payment_intent_id: str) -> PaymentIntent:
        self._simulate_network_call()
        intent = self._intents_by_id.get(payment_intent_id)
        if intent is None:
            raise CardError(
                f"No such payment_intent: '{payment_intent_id}'", code="resource_missing"
            )
        return intent

    # -- Checkout Session ------------------------------------------------

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
        # Same validate-before-touching-state ordering as create_payment_intent:
        # a structurally invalid request never "reached Stripe" and must not
        # consume an idempotency key.
        _require_valid_amount(amount)
        _require_valid_currency(currency)
        if not success_url or not cancel_url:
            raise InvalidRequestError(
                "success_url and cancel_url are required to create a Checkout Session",
                param="success_url" if not success_url else "cancel_url",
                code="parameter_missing",
            )
        currency = currency.upper()  # as in create_payment_intent

        self._simulate_network_call()

        cached = self._sessions_by_key.get(idempotency_key)
        if cached is not None:
            if cached.amount_total != amount or cached.currency != currency:
                raise IdempotencyError(
                    f"Idempotency key '{idempotency_key}' has already been used "
                    "with different request parameters"
                )
            return cached

        session_id = f"cs_test_{uuid.uuid4().hex}"
        session = CheckoutSession(
            id=session_id,
            url=f"https://checkout.stripe.com/mock/pay/{session_id}",
            status=CheckoutSessionStatus.OPEN,
            payment_status="unpaid",
            amount_total=amount,
            currency=currency,
            payment_intent=None,
            idempotency_key=idempotency_key,
            metadata=dict(metadata or {}),
        )
        self._sessions_by_key[idempotency_key] = session
        self._sessions_by_id[session.id] = session
        return session

    def retrieve_checkout_session(self, session_id: str) -> CheckoutSession:
        self._simulate_network_call()
        session = self._sessions_by_id.get(session_id)
        if session is None:
            raise CardError(
                f"No such checkout.session: '{session_id}'", code="resource_missing"
            )
        return session

    def expire_checkout_session(self, session_id: str) -> CheckoutSession:
        self._simulate_network_call()
        session = self._sessions_by_id.get(session_id)
        if session is None:
            raise CardError(
                f"No such checkout.session: '{session_id}'", code="resource_missing"
            )
        if session.status != CheckoutSessionStatus.OPEN:
            # Mirrors real Stripe: only an open Session can be expired -
            # already-complete or already-expired is a no-op error, not a
            # silent success.
            raise CardError(
                f"Checkout Session '{session_id}' cannot be expired from status "
                f"'{session.status.value}'",
                code="checkout_session_expire_failed",
            )
        session.status = CheckoutSessionStatus.EXPIRED
        return session

    # -- Refund --------------------------------------------------------

    def create_refund(
        self,
        *,
        payment_intent_id: str,
        amount: int | None = None,
        idempotency_key: str | None = None,
    ) -> Refund:
        if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
            raise InvalidRequestError(
                f"Invalid integer: amount must be an int in the smallest currency "
                f"unit (e.g. cents), got {amount!r} ({type(amount).__name__})",
                param="amount",
                code="parameter_invalid_integer",
            )

        self._simulate_network_call()

        # Same idempotency-cache pattern as create_payment_intent/
        # create_checkout_session above: without this, a caller that retries
        # a refund request (client timeout, double-click "Refund") with the
        # same Idempotency-Key would create a second, distinct Refund each
        # time it retries - real Stripe dedupes by idempotency key on its
        # servers, so the mock must too, or tests (and callers) relying on
        # that guarantee would pass against the mock while a real
        # integration double-refunds.
        if idempotency_key is not None:
            cached = self._refunds_by_key.get(idempotency_key)
            if cached is not None:
                if cached.payment_intent != payment_intent_id or (
                    amount is not None and cached.amount != amount
                ):
                    raise IdempotencyError(
                        f"Idempotency key '{idempotency_key}' has already been used "
                        "with different request parameters"
                    )
                return cached

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
        # A card from the guide's "Refunds" table starts its refund in the
        # status the guide gives (pending for pm_card_pendingRefund); every
        # other refund succeeds at once. settle_refund moves it on.
        first_status = ASYNC_REFUND_TOKENS.get(
            intent.payment_method, (RefundStatus.SUCCEEDED,)
        )[0]
        refund = Refund(
            id=f"re_{uuid.uuid4().hex[:24]}",
            payment_intent=payment_intent_id,
            amount=refund_amount,
            currency=intent.currency,
            status=first_status,
        )
        self._refunds_by_id[refund.id] = refund
        if idempotency_key is not None:
            self._refunds_by_key[idempotency_key] = refund
        return refund

    def list_refunds(self, payment_intent_id: str) -> list[Refund]:
        self._simulate_network_call()
        return [
            Refund(
                id=r.id, payment_intent=r.payment_intent, amount=r.amount,
                currency=r.currency, status=r.status, provider_status=r.status.value,
            )
            for r in self._refunds_by_id.values()
            if r.payment_intent == payment_intent_id
        ]

    @classmethod
    def settle_refund(cls, refund_id: str) -> dict[str, Any]:
        """Test hook for Stripe finishing an asynchronous refund "some time
        later" (docs.stripe.com/testing, "Refunds"): pm_card_pendingRefund's
        refund goes from pending to succeeded and Stripe sends
        refund.updated; pm_card_refundFail's from succeeded to failed, the
        amount becomes refundable again, and Stripe sends refund.failed.

        Updates the refund the mock holds and returns the event Stripe would
        send, as the webhook's parsed JSON body (Stripe's wire format: a
        lower-case currency). To post it:
        body = json.dumps(event).encode(); header = sign_payload(body, secret).
        """
        refund = cls._refunds_by_id.get(refund_id)
        if refund is None:
            raise CardError(f"No such refund: '{refund_id}'", code="resource_missing")
        intent = cls._intents_by_id[refund.payment_intent]
        plan = ASYNC_REFUND_TOKENS.get(intent.payment_method)
        if plan is None or refund.status != plan[0]:
            raise ValueError(f"Refund {refund_id} has nothing left to settle")
        _, final_status, event_type = plan
        refund.status = final_status
        failure_reason = None
        if final_status == RefundStatus.FAILED:
            # The money did not go back to the customer: it can be refunded
            # again. "unknown" is one of the Refund object's documented
            # failure_reason values; the guide does not say which this card
            # reports.
            intent.amount_refunded -= refund.amount
            failure_reason = "unknown"
        return cls.webhook_event(event_type, {
            "id": refund.id,
            "object": "refund",
            "amount": refund.amount,
            "currency": refund.currency.lower(),
            "payment_intent": refund.payment_intent,
            "status": refund.status.value,
            "failure_reason": failure_reason,
            "created": int(time.time()),
        })

    @staticmethod
    def webhook_event(event_type: str, data_object: dict[str, Any]) -> dict[str, Any]:
        """The body of a webhook Stripe would send about `data_object`: a v1
        snapshot event (https://docs.stripe.com/api/events/object)."""
        return {
            "id": f"evt_{uuid.uuid4().hex[:24]}",
            "object": "event",
            "type": event_type,
            "created": int(time.time()),
            "livemode": False,
            "data": {"object": data_object},
        }

    # -- Webhooks --------------------------------------------------------

    @staticmethod
    def sign_payload(payload: bytes, secret: str, timestamp: int | None = None) -> str:
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
        sig_header: str | None,
        webhook_secret: str,
        tolerance_seconds: int = DEFAULT_WEBHOOK_TOLERANCE_SECONDS,
    ) -> WebhookEventData:
        if not sig_header:
            raise SignatureVerificationError("Missing Stripe-Signature header")
        if not webhook_secret:
            # An HMAC with an empty key is one anybody can compute: with
            # STRIPE_WEBHOOK_SECRET left empty, the mock used to accept any
            # event "signed" with "". Stripe's library refuses to verify
            # without a secret; so does the mock.
            raise SignatureVerificationError("No webhook secret configured", sig_header)
        if not sig_header.isascii():
            # Anyone can send this (Starlette decodes header bytes as
            # latin-1), and hmac.compare_digest below raises TypeError on
            # text that is not ASCII: the webhook answered 500, not 400.
            raise SignatureVerificationError("Malformed Stripe-Signature header", sig_header)

        # Every v1 signature is kept, not just the first: while a webhook
        # secret is being rolled, Stripe signs each event with the old and
        # the new secret and sends both, and one matching is enough (as in
        # stripe.WebhookSignature.verify_header). Only the first used to be
        # checked, so a valid event whose matching signature came second
        # was refused. Nothing is stripped, as in the library: a header with
        # spaces in it is not one Stripe sent, and the mock used to accept it.
        timestamp_raw: str | None = None
        signatures = []
        for item in sig_header.split(","):
            if "=" not in item:
                continue
            key, _, value = item.partition("=")
            if key == "t" and timestamp_raw is None:
                timestamp_raw = value
            elif key == "v1" and value:
                signatures.append(value)

        if not timestamp_raw or not signatures:
            raise SignatureVerificationError(
                "Unable to extract timestamp and signature from header", sig_header
            )

        try:
            timestamp = int(timestamp_raw)
        except ValueError:
            raise SignatureVerificationError("Invalid timestamp in signature header", sig_header) from None

        expected = self.sign_payload(payload, webhook_secret, timestamp=timestamp).split(
            ",v1="
        )[1]
        if not any(hmac.compare_digest(expected, signature) for signature in signatures):
            raise SignatureVerificationError(
                "Signature does not match the expected value for this payload", sig_header
            )

        if abs(time.time() - timestamp) > tolerance_seconds:
            # Defends against replay attacks: an old, previously-valid,
            # captured request being resubmitted long after the fact.
            raise SignatureVerificationError(
                "Timestamp outside the tolerance zone", sig_header
            )

        return webhook_event_from_payload(payload, sig_header, timestamp)
