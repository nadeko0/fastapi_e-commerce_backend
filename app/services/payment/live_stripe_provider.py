"""
LiveStripePaymentProvider - a real, network-calling Stripe integration.

Unlike StripePaymentProvider (the deterministic, dependency-free mock used by
the default/fast test suite - see stripe_provider.py), this class makes real
HTTPS calls to api.stripe.com via the `stripe` PyPI SDK, authenticated with a
real STRIPE_SECRET_KEY. get_payment_provider() (app/services/payment/__init__.py)
hands it out when PAYMENT_PROVIDER is "stripe", which production requires
(app/core/config.py). Against the real API it is exercised by
tests/integration/test_payment_live_stripe.py, a separate suite that only
runs when a real (non-placeholder) STRIPE_SECRET_KEY is present in the
environment - see that file's skipif condition; the default test suite
runs on the mock.

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
import logging
import time
from typing import Any

import stripe
from stripe import error as stripe_error

from app.core.config import settings
from app.services.payment.base import PaymentProvider, webhook_event_from_payload
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

logger = logging.getLogger(__name__)


def _provider_unavailable(error: stripe_error.StripeError) -> PaymentProviderTimeoutError:
    """
    What a StripeError that is none of the specific ones each method handles
    (card, invalid request, idempotency) becomes: PaymentProviderTimeoutError,
    the "outcome unknown, retry" error the routes answer with 503. Stripe:
    a dropped connection, a 429 and a 5xx all leave it unknown whether the
    request was carried out (retry with the same idempotency key; for a 5xx,
    do not start a new operation before the first is resolved). The
    hierarchy has no common base below StripeError for them, so this is the
    catch-all that follows the specific clauses. A refused API key is ours to
    fix, not the customer's to retry, so it is also logged as critical.
    """
    if isinstance(error, (stripe_error.AuthenticationError, stripe_error.PermissionError)):
        logger.critical(
            "Stripe refused our API key (%s); payments cannot be taken until it is fixed",
            type(error).__name__,
        )
    return PaymentProviderTimeoutError(str(error.user_message or error))


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
        # Stripe answers in lower case; this API's currencies (orders, the
        # mock) are upper case. Refunds reported "usd" against a "USD" order.
        currency=pi["currency"].upper(),
        status=_map_status(pi["status"]),
        client_secret=pi.get("client_secret") or "",
        payment_method=payment_method,
        last_payment_error=(
            dict(pi["last_payment_error"]) if pi.get("last_payment_error") else None
        ),
        next_action=dict(pi["next_action"]) if pi.get("next_action") else None,
        metadata=dict(pi.get("metadata") or {}),
        amount_received=pi.get("amount_received") or 0,
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
        currency=(s.get("currency") or "").upper(),
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
        currency=r["currency"].upper(),
        status=status,
        provider_status=status_raw,
    )


# Refunds asked for per page (the most Stripe allows) and pages read at most
# per call: 500 refunds of one PaymentIntent is far beyond anything a store
# makes, so reaching the cap means something is wrong, and list_refunds says
# so instead of returning a partial list.
_REFUND_PAGE_SIZE = 100
_REFUND_MAX_PAGES = 5


def _signed_at(sig_header: str) -> int:
    """The t= timestamp of a Stripe-Signature header the SDK has already
    verified (so it is there and is an integer), read the way the SDK reads
    it: the first t= item, split on "=" and nothing stripped."""
    return int(next(item.split("=", 1)[1] for item in sig_header.split(",")
                    if item.split("=", 1)[0] == "t"))


# The HTTP client this module last installed as stripe.default_http_client,
# and the settings it was built from (see _configure_http_client).
_http_client: stripe.RequestsClient | None = None
_http_client_settings: tuple | None = None


def _configure_http_client() -> None:
    """
    Give the Stripe SDK an explicit timeout and retry count from settings.
    The SDK reads stripe.default_http_client and stripe.max_network_retries
    on every request, and its own default is a timeout of 80 s per try
    (RequestsClient(timeout=80)), far longer than a worker thread should be
    held on a hung connection. The timeout is a (connect, read) pair as in
    `requests`. One client is shared by every provider instance (a new one is
    made per request), so its connection pool is kept; it is rebuilt only if
    the settings or the installed client changed.
    """
    global _http_client, _http_client_settings
    wanted = (
        settings.STRIPE_CONNECT_TIMEOUT_SECONDS,
        settings.STRIPE_READ_TIMEOUT_SECONDS,
        settings.STRIPE_MAX_NETWORK_RETRIES,
    )
    if _http_client is None or _http_client_settings != wanted or (
        stripe.default_http_client is not _http_client
    ):
        _http_client = stripe.RequestsClient(timeout=(wanted[0], wanted[1]))
        _http_client_settings = wanted
        stripe.default_http_client = _http_client
        stripe.max_network_retries = wanted[2]


class LiveStripePaymentProvider(PaymentProvider):
    """Real Stripe-backed implementation. See module docstring."""

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("LiveStripePaymentProvider requires a real Stripe secret key")
        self._api_key = api_key
        _configure_http_client()

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
        params: dict[str, Any] = {
            "amount": amount,
            "currency": currency,
            "idempotency_key": idempotency_key,
            "api_key": self._api_key,
            # This codebase only ever deals in card payments (see
            # PaymentProvider's card-shaped exception hierarchy: CardError,
            # decline_code, etc). allowed_payment_method_types=["card"]
            # avoids Stripe's default "automatic_payment_methods" behavior,
            # which pulls in whatever redirect-based methods are enabled on
            # the connected Dashboard account and then requires a
            # `return_url` at confirm time - irrelevant/unwanted for a
            # server-only, no-redirect integration like this one.
            #
            # This used to be payment_method_types=["card"]. API version
            # 2026-09-30.endive (pinned by stripe-python 16) removed that as
            # a writable parameter: passing it now returns a 400
            # `payment_method_types_no_longer_supported`. Stripe's documented
            # replacement for "only accept this list of types" is
            # allowed_payment_method_types (mutually exclusive with
            # automatic_payment_methods), see
            # https://docs.stripe.com/changelog/endive/2026-09-30/removes-the-payment-method-types-parameter-from-payment-intents-and-setup-intents
            "allowed_payment_method_types": ["card"],
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
        except stripe_error.StripeError as e:
            raise _provider_unavailable(e)
        return _to_payment_intent(pi)

    def confirm_payment_intent(
        self,
        payment_intent_id: str,
        *,
        idempotency_key: str | None = None,
        return_url: str,
    ) -> PaymentIntent:
        # Stripe requires `return_url` at confirm time for any PaymentIntent
        # that could produce a redirect-based next_action - and 3DS
        # authentication (pm_card_authenticationRequired) is exactly that,
        # even though allowed_payment_method_types=["card"] was set at creation.
        # Restricting the types only exempts you from Dashboard-driven
        # *alternative* redirect payment methods (iDEAL etc.), not from a
        # card's own 3DS challenge/return flow. Confirmed via a real Stripe
        # account notice after this suite's 3DS test ran without one.
        # Required, with no fallback: it used to default to a placeholder
        # page on a domain that is not ours, where 3D Secure would have
        # sent real customers. /pay builds it on FRONTEND_URL.
        params: dict[str, Any] = {
            "api_key": self._api_key,
            "return_url": return_url,
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
        except stripe_error.StripeError as e:
            raise _provider_unavailable(e)
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
        except stripe_error.StripeError as e:
            raise _provider_unavailable(e)
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
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CheckoutSession:
        params: dict[str, Any] = {
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
            # see base.py's create_checkout_session docstring. Callers
            # without their own success/cancel page get the frontend's
            # (FRONTEND_URL) at the API boundary (app/api/v1/orders.py), not
            # here - this method always requires both, matching real
            # Stripe's own requirement.
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
        except stripe_error.StripeError as e:
            raise _provider_unavailable(e)
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
        except stripe_error.StripeError as e:
            raise _provider_unavailable(e)
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
        except stripe_error.StripeError as e:
            raise _provider_unavailable(e)
        return _to_checkout_session(session)

    # -- Refund --------------------------------------------------------

    def create_refund(
        self,
        *,
        payment_intent_id: str,
        amount: int | None = None,
        idempotency_key: str | None = None,
    ) -> Refund:
        params: dict[str, Any] = {
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
        except stripe_error.StripeError as e:
            raise _provider_unavailable(e)
        return _to_refund(refund)

    def list_refunds(self, payment_intent_id: str) -> list[Refund]:
        refunds: list[Refund] = []
        params: dict[str, Any] = {
            "payment_intent": payment_intent_id,
            "limit": _REFUND_PAGE_SIZE,
            "api_key": self._api_key,
        }
        for _ in range(_REFUND_MAX_PAGES):
            try:
                page = stripe.Refund.list(**params)
            except stripe_error.InvalidRequestError as e:
                raise InvalidRequestError(
                    str(e.user_message or e), param=e.param, code=e.code or "parameter_invalid"
                )
            except stripe_error.StripeError as e:
                raise _provider_unavailable(e)
            refunds.extend(_to_refund(r) for r in page.data)
            if not page.has_more:
                return refunds
            params["starting_after"] = refunds[-1].id
        raise InvalidRequestError(
            f"More than {_REFUND_PAGE_SIZE * _REFUND_MAX_PAGES} refunds on "
            f"{payment_intent_id}; not read",
            code="too_many_refunds",
        )

    # -- Webhooks --------------------------------------------------------

    def construct_webhook_event(
        self,
        payload: bytes,
        sig_header: str | None,
        webhook_secret: str,
        tolerance_seconds: int = 300,
    ) -> WebhookEventData:
        if not sig_header:
            raise SignatureVerificationError("Missing Stripe-Signature header")
        if not sig_header.isascii():
            # Anyone can send this (Starlette decodes header bytes as
            # latin-1). The SDK compares signatures with hmac.compare_digest
            # on text, which raises TypeError for anything not ASCII - an
            # exception the webhook route does not expect, so a 500.
            raise SignatureVerificationError("Malformed Stripe-Signature header", sig_header)
        # The SDK decodes the body as UTF-8 before it looks at the signature,
        # so a body that is not UTF-8 raised UnicodeDecodeError out of it -
        # for anyone, signed or not. Checked first, and refused like a bad
        # signature.
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError:
            raise SignatureVerificationError("Payload is not valid UTF-8", sig_header)
        # Only the signature check is the SDK's (verify_header, which
        # stripe.Webhook.construct_event calls first); the body is then read
        # by the same code as the mock's, which refuses one that is not a
        # JSON object instead of raising JSONDecodeError or AttributeError
        # as construct_event did.
        try:
            stripe.WebhookSignature.verify_header(
                payload, sig_header, webhook_secret, tolerance_seconds
            )
        except stripe_error.SignatureVerificationError as e:
            raise SignatureVerificationError(str(e.user_message or e), sig_header)
        # The SDK checks the timestamp on one side only (older than the
        # tolerance) and not at all when the tolerance is 0, so a signature
        # dated in the future stayed valid until that date plus the
        # tolerance, and a tolerance set to 0 turned replay protection off.
        # Both sides, in epoch seconds, as the mock does: a tolerance of 0
        # is a window of zero seconds.
        signed_at = _signed_at(sig_header)
        if abs(time.time() - signed_at) > tolerance_seconds:
            raise SignatureVerificationError("Timestamp outside the tolerance zone", sig_header)
        return webhook_event_from_payload(payload, sig_header, signed_at)

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
