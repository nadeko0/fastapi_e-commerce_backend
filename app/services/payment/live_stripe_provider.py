"""
LiveStripePaymentProvider - a real, network-calling Stripe integration.

Unlike StripePaymentProvider (the deterministic, dependency-free mock used by
the default/fast test suite - see stripe_provider.py), this class makes real
HTTPS calls to api.stripe.com via the `stripe` PyPI SDK, authenticated with a
real STRIPE_SECRET_KEY. It exists to be exercised by
tests/integration/test_payment_live_stripe.py, a separate suite that only
runs when a real (non-placeholder) STRIPE_SECRET_KEY is present in the
environment - see that file's skipif condition. It is never wired into
get_payment_provider() (app/services/payment/__init__.py), so it has zero
effect on the app's normal request path or the default test suite.

Implements the exact same PaymentProvider contract (app/services/payment/base.py)
as the mock: same method signatures, same dataclass return types
(PaymentIntent/Refund/WebhookEventData), and the same exception hierarchy
(CardError/InvalidRequestError/IdempotencyError/SignatureVerificationError/
PaymentProviderTimeoutError) - achieved by translating the real `stripe`
SDK's exceptions (stripe.error.*) onto our exceptions at the boundary of
every method, so callers (app/api/v1/orders.py) never need to know or care
which provider is in use.

Per-instance api_key (passed as a request-level kwarg to every SDK call,
never assigned to the global `stripe.api_key`) so multiple providers using
different keys can coexist safely in the same process - relevant for tests.
"""
from typing import Any, Dict, Optional

import stripe
from stripe import error as stripe_error

from app.services.payment.base import PaymentProvider
from app.services.payment.exceptions import (
    CardError,
    IdempotencyError,
    InvalidRequestError,
    PaymentProviderTimeoutError,
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

# Real Stripe PaymentIntent.status values this codebase doesn't otherwise
# model (e.g. "requires_capture", used only for manual-capture intents this
# codebase never creates) fold into PROCESSING rather than raising, since an
# unrecognized-but-non-terminal status is closer to "still processing" than
# to any of our other states.
_STATUS_MAP = {
    "requires_payment_method": PaymentIntentStatus.REQUIRES_PAYMENT_METHOD,
    "requires_confirmation": PaymentIntentStatus.REQUIRES_CONFIRMATION,
    "requires_action": PaymentIntentStatus.REQUIRES_ACTION,
    "processing": PaymentIntentStatus.PROCESSING,
    "succeeded": PaymentIntentStatus.SUCCEEDED,
    "canceled": PaymentIntentStatus.CANCELED,
}


def _map_status(raw: str) -> PaymentIntentStatus:
    return _STATUS_MAP.get(raw, PaymentIntentStatus.PROCESSING)


def _to_payment_intent(pi: Any) -> PaymentIntent:
    # stripe-python's StripeObject deliberately isn't a dict (attribute
    # access raises AttributeError, not a dict method) - .to_dict() gives us
    # a plain dict so .get()/[] work the same way regardless of which fields
    # a given PaymentIntent response happens to include.
    pi = pi.to_dict()
    payment_method = pi.get("payment_method")
    if payment_method is not None and not isinstance(payment_method, str):
        payment_method = payment_method.get("id")
    return PaymentIntent(
        id=pi["id"],
        amount=pi["amount"],
        currency=pi["currency"],
        status=_map_status(pi["status"]),
        client_secret=pi.get("client_secret") or "",
        payment_method=payment_method,
        last_payment_error=(
            dict(pi["last_payment_error"]) if pi.get("last_payment_error") else None
        ),
        next_action=dict(pi["next_action"]) if pi.get("next_action") else None,
        metadata=dict(pi.get("metadata") or {}),
    )


# Real checkout.Session.status this codebase doesn't otherwise model would
# only ever be one of these three - see
# https://docs.stripe.com/api/checkout/sessions/object - so no fallback
# bucket is needed here (unlike _map_status above).
_SESSION_STATUS_MAP = {
    "open": CheckoutSessionStatus.OPEN,
    "complete": CheckoutSessionStatus.COMPLETE,
    "expired": CheckoutSessionStatus.EXPIRED,
}


def _to_checkout_session(s: Any) -> CheckoutSession:
    s = s.to_dict()
    payment_intent = s.get("payment_intent")
    if payment_intent is not None and not isinstance(payment_intent, str):
        payment_intent = payment_intent.get("id")
    return CheckoutSession(
        id=s["id"],
        url=s.get("url"),
        status=_SESSION_STATUS_MAP.get(s["status"], CheckoutSessionStatus.OPEN),
        payment_status=s.get("payment_status") or "unpaid",
        amount_total=s.get("amount_total") or 0,
        currency=s.get("currency") or "",
        payment_intent=payment_intent,
        metadata=dict(s.get("metadata") or {}),
    )


def _to_refund(r: Any) -> Refund:
    r = r.to_dict()
    status_raw = r.get("status") or "pending"
    status = {
        "succeeded": RefundStatus.SUCCEEDED,
        "pending": RefundStatus.PENDING,
        "failed": RefundStatus.FAILED,
    }.get(status_raw, RefundStatus.PENDING)
    return Refund(
        id=r["id"],
        payment_intent=r["payment_intent"],
        amount=r["amount"],
        currency=r["currency"],
        status=status,
    )


class LiveStripePaymentProvider(PaymentProvider):
    """Real Stripe-backed implementation. See module docstring."""

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("LiveStripePaymentProvider requires a real Stripe secret key")
        self._api_key = api_key

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
        params: Dict[str, Any] = {
            "amount": amount,
            "currency": currency,
            "idempotency_key": idempotency_key,
            "api_key": self._api_key,
            # This codebase only ever deals in card payments (see
            # PaymentProvider's card-shaped exception hierarchy: CardError,
            # decline_code, etc). Pinning payment_method_types=["card"]
            # avoids Stripe's default "automatic_payment_methods" behavior,
            # which pulls in whatever redirect-based methods are enabled on
            # the connected Dashboard account and then requires a
            # `return_url` at confirm time - irrelevant/unwanted for a
            # server-only, no-redirect integration like this one.
            "payment_method_types": ["card"],
        }
        if payment_method is not None:
            params["payment_method"] = payment_method
        if metadata:
            params["metadata"] = metadata
        try:
            pi = stripe.PaymentIntent.create(**params)
        except stripe_error.IdempotencyError as e:
            raise IdempotencyError(str(e.user_message or e))
        except stripe_error.CardError as e:
            raise self._translate_card_error(e)
        except stripe_error.InvalidRequestError as e:
            raise InvalidRequestError(
                str(e.user_message or e), param=e.param, code=e.code or "parameter_invalid"
            )
        except stripe_error.APIConnectionError as e:
            raise PaymentProviderTimeoutError(str(e.user_message or e))
        return _to_payment_intent(pi)

    def confirm_payment_intent(
        self,
        payment_intent_id: str,
        *,
        idempotency_key: Optional[str] = None,
        return_url: Optional[str] = None,
    ) -> PaymentIntent:
        # Stripe requires `return_url` at confirm time for any PaymentIntent
        # that could produce a redirect-based next_action - and 3DS
        # authentication (pm_card_authenticationRequired) is exactly that,
        # even though payment_method_types=["card"] was pinned at creation.
        # Pinning payment_method_types only exempts you from Dashboard-driven
        # *alternative* redirect payment methods (iDEAL etc.), not from a
        # card's own 3DS challenge/return flow. Confirmed via a real Stripe
        # account notice after this suite's 3DS test ran without one.
        # This backend has no real frontend redirect target yet, so callers
        # without a real post-payment page fall back to a placeholder -
        # replace with the actual frontend return URL once one exists.
        params: Dict[str, Any] = {
            "api_key": self._api_key,
            "return_url": return_url or "https://example.com/checkout/return",
        }
        if idempotency_key is not None:
            params["idempotency_key"] = idempotency_key
        try:
            pi = stripe.PaymentIntent.confirm(payment_intent_id, **params)
        except stripe_error.IdempotencyError as e:
            raise IdempotencyError(str(e.user_message or e))
        except stripe_error.CardError as e:
            raise self._translate_card_error(e)
        except stripe_error.InvalidRequestError as e:
            if e.code == "resource_missing":
                raise CardError(
                    f"No such payment_intent: '{payment_intent_id}'", code="resource_missing"
                )
            raise InvalidRequestError(
                str(e.user_message or e), param=e.param, code=e.code or "parameter_invalid"
            )
        except stripe_error.APIConnectionError as e:
            raise PaymentProviderTimeoutError(str(e.user_message or e))
        return _to_payment_intent(pi)

    def retrieve_payment_intent(self, payment_intent_id: str) -> PaymentIntent:
        try:
            pi = stripe.PaymentIntent.retrieve(payment_intent_id, api_key=self._api_key)
        except stripe_error.InvalidRequestError as e:
            if e.code == "resource_missing":
                raise CardError(
                    f"No such payment_intent: '{payment_intent_id}'", code="resource_missing"
                )
            raise InvalidRequestError(
                str(e.user_message or e), param=e.param, code=e.code or "parameter_invalid"
            )
        except stripe_error.APIConnectionError as e:
            raise PaymentProviderTimeoutError(str(e.user_message or e))
        return _to_payment_intent(pi)

    # -- Checkout Session ------------------------------------------------

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
        params: Dict[str, Any] = {
            "mode": "payment",
            "line_items": [
                {
                    "price_data": {
                        "currency": currency,
                        "unit_amount": amount,
                        "product_data": {"name": description or "Order payment"},
                    },
                    "quantity": 1,
                }
            ],
            # Mandatory Stripe-side params for a hosted Checkout Session -
            # see base.py's create_checkout_session docstring. This backend
            # has no real frontend (same situation as confirm_payment_intent's
            # return_url above), so callers without a real success/cancel
            # page fall back to placeholders at the API boundary
            # (app/api/v1/orders.py), not here - this method always requires
            # both, matching real Stripe's own requirement.
            "success_url": success_url,
            "cancel_url": cancel_url,
            "idempotency_key": idempotency_key,
            "api_key": self._api_key,
        }
        if metadata:
            params["metadata"] = metadata
        try:
            session = stripe.checkout.Session.create(**params)
        except stripe_error.IdempotencyError as e:
            raise IdempotencyError(str(e.user_message or e))
        except stripe_error.InvalidRequestError as e:
            raise InvalidRequestError(
                str(e.user_message or e), param=e.param, code=e.code or "parameter_invalid"
            )
        except stripe_error.APIConnectionError as e:
            raise PaymentProviderTimeoutError(str(e.user_message or e))
        return _to_checkout_session(session)

    def retrieve_checkout_session(self, session_id: str) -> CheckoutSession:
        try:
            session = stripe.checkout.Session.retrieve(session_id, api_key=self._api_key)
        except stripe_error.InvalidRequestError as e:
            if e.code == "resource_missing":
                raise CardError(
                    f"No such checkout.session: '{session_id}'", code="resource_missing"
                )
            raise InvalidRequestError(
                str(e.user_message or e), param=e.param, code=e.code or "parameter_invalid"
            )
        except stripe_error.APIConnectionError as e:
            raise PaymentProviderTimeoutError(str(e.user_message or e))
        return _to_checkout_session(session)

    def expire_checkout_session(self, session_id: str) -> CheckoutSession:
        try:
            session = stripe.checkout.Session.expire(session_id, api_key=self._api_key)
        except stripe_error.InvalidRequestError as e:
            if e.code == "resource_missing":
                raise CardError(
                    f"No such checkout.session: '{session_id}'", code="resource_missing"
                )
            # Real Stripe rejects expiring an already-complete/already-expired
            # Session via InvalidRequestError - surfaced as CardError to
            # match the mock provider's contract for the same condition.
            raise CardError(str(e.user_message or e), code=e.code or "invalid_request")
        except stripe_error.APIConnectionError as e:
            raise PaymentProviderTimeoutError(str(e.user_message or e))
        return _to_checkout_session(session)

    # -- Refund --------------------------------------------------------

    def create_refund(
        self,
        *,
        payment_intent_id: str,
        amount: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> Refund:
        params: Dict[str, Any] = {
            "payment_intent": payment_intent_id,
            "api_key": self._api_key,
        }
        if amount is not None:
            params["amount"] = amount
        if idempotency_key is not None:
            params["idempotency_key"] = idempotency_key
        try:
            refund = stripe.Refund.create(**params)
        except stripe_error.IdempotencyError as e:
            raise IdempotencyError(str(e.user_message or e))
        except stripe_error.InvalidRequestError as e:
            if e.code == "resource_missing":
                raise CardError(
                    f"No such payment_intent: '{payment_intent_id}'", code="resource_missing"
                )
            # Real Stripe rejects over-refunds and refunds of a non-succeeded
            # intent via InvalidRequestError (e.g. code="charge_already_refunded"
            # / "invalid_request_error" on amount) - surfaced as CardError to
            # match the mock provider's contract for these same conditions.
            raise CardError(str(e.user_message or e), code=e.code or "invalid_request")
        except stripe_error.CardError as e:
            raise self._translate_card_error(e)
        except stripe_error.APIConnectionError as e:
            raise PaymentProviderTimeoutError(str(e.user_message or e))
        return _to_refund(refund)

    # -- Webhooks --------------------------------------------------------

    def construct_webhook_event(
        self,
        payload: bytes,
        sig_header: Optional[str],
        webhook_secret: str,
        tolerance_seconds: int = 300,
    ) -> WebhookEventData:
        if not sig_header:
            raise SignatureVerificationError("Missing Stripe-Signature header")
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, webhook_secret, tolerance=tolerance_seconds
            )
        except stripe_error.SignatureVerificationError as e:
            raise SignatureVerificationError(str(e.user_message or e), sig_header)
        event = event.to_dict()
        return WebhookEventData(
            id=event["id"],
            type=event["type"],
            data=dict(event["data"]),
            created=event["created"],
        )

    @staticmethod
    def _translate_card_error(e: "stripe_error.CardError") -> CardError:
        decline_code = None
        if e.error is not None:
            decline_code = getattr(e.error, "decline_code", None)
        return CardError(
            str(e.user_message or e),
            code=e.code or "card_declined",
            decline_code=decline_code,
        )
