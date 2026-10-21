"""
LiveStripePaymentProvider without the network: every Stripe SDK call it
makes is replaced with a stub, so these tests check what the adapter does
around the SDK - the parameters it sends (api_key per request, idempotency
keys, card-only payment methods, return_url), how SDK responses become our
dataclasses, and how every SDK exception becomes ours. The real API is
exercised separately by tests/integration/test_payment_live_stripe.py,
which runs only with real test-mode keys.
"""
import hashlib
import hmac
import json
import time

import pytest
import stripe
from stripe import error as stripe_error

from app.services.payment.exceptions import (
    CardError,
    IdempotencyError,
    InvalidRequestError,
    PaymentProviderTimeoutError,
    SignatureVerificationError,
)
from app.services.payment.live_stripe_provider import LiveStripePaymentProvider
from app.services.payment.types import (
    CheckoutSessionStatus,
    PaymentIntentStatus,
    RefundStatus,
)

API_KEY = "sk_test_unit_only"
RETURN_URL = "https://shop.test/checkout/return?order_id=7"


class _SdkObject:
    """Stands in for a stripe.StripeObject: the adapter only calls to_dict()."""

    def __init__(self, **values):
        self._values = values

    def to_dict(self):
        return dict(self._values)


class _Recorder:
    """A stub SDK method: records its calls, returns or raises what it is given."""

    def __init__(self, result=None, raises=None):
        self.result, self.raises, self.calls = result, raises, []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.result


def _intent(**overrides):
    values = dict(id="pi_123", amount=1999, currency="usd", status="succeeded",
                  client_secret="pi_123_secret", payment_method="pm_1")
    values.update(overrides)
    return _SdkObject(**values)


def _session(**overrides):
    values = dict(id="cs_123", url="https://checkout.stripe.com/c/cs_123", status="open",
                  payment_status="unpaid", amount_total=1999, currency="usd",
                  payment_intent=None, metadata={"order_id": "7"})
    values.update(overrides)
    return _SdkObject(**values)


def _card_error(decline_code="generic_decline", code="card_declined"):
    return stripe_error.CardError(
        "Your card was declined.", None, code,
        json_body={"error": {"message": "Your card was declined.", "code": code,
                             "decline_code": decline_code}},
    )


def _invalid(code=None, param="amount", message="Invalid request"):
    return stripe_error.InvalidRequestError(message, param, code=code)


@pytest.fixture
def provider():
    return LiveStripePaymentProvider(api_key=API_KEY)


@pytest.fixture
def sdk(monkeypatch):
    """Install a stub for one SDK method: sdk(stripe.PaymentIntent, "create", result=...)."""
    def install(owner, name, result=None, raises=None):
        stub = _Recorder(result=result, raises=raises)
        monkeypatch.setattr(owner, name, stub)
        return stub
    return install


def test_requires_a_key():
    with pytest.raises(ValueError):
        LiveStripePaymentProvider(api_key="")


# -- PaymentIntent.create ------------------------------------------------------

def test_create_payment_intent_sends_card_only_with_the_per_request_key(provider, sdk):
    create = sdk(stripe.PaymentIntent, "create", result=_intent(status="requires_confirmation"))

    intent = provider.create_payment_intent(
        amount=1999, currency="usd", idempotency_key="pay-order-7:k",
        payment_method="pm_card_visa", metadata={"order_id": "7"},
    )

    _, params = create.calls[0]
    assert params == {
        "amount": 1999, "currency": "usd", "idempotency_key": "pay-order-7:k",
        "api_key": API_KEY, "allowed_payment_method_types": ["card"],
        "payment_method": "pm_card_visa", "metadata": {"order_id": "7"},
    }
    assert stripe.api_key != API_KEY  # never set globally
    assert (intent.id, intent.amount, intent.status) == (
        "pi_123", 1999, PaymentIntentStatus.REQUIRES_CONFIRMATION
    )


def test_create_payment_intent_leaves_out_unset_optionals(provider, sdk):
    create = sdk(stripe.PaymentIntent, "create", result=_intent())
    provider.create_payment_intent(amount=100, currency="usd", idempotency_key="k")
    _, params = create.calls[0]
    assert "payment_method" not in params and "metadata" not in params


@pytest.mark.parametrize(
    "raised,expected",
    [
        (stripe_error.IdempotencyError("Keys for idempotent requests..."), IdempotencyError),
        (_card_error(), CardError),
        (_invalid(code="parameter_invalid_integer"), InvalidRequestError),
        (stripe_error.APIConnectionError("network down"), PaymentProviderTimeoutError),
    ],
)
def test_create_payment_intent_translates_sdk_errors(provider, sdk, raised, expected):
    sdk(stripe.PaymentIntent, "create", raises=raised)
    with pytest.raises(expected):
        provider.create_payment_intent(amount=100, currency="usd", idempotency_key="k")


def test_invalid_request_without_a_code_gets_a_generic_one(provider, sdk):
    sdk(stripe.PaymentIntent, "create", raises=_invalid(code=None))
    with pytest.raises(InvalidRequestError) as caught:
        provider.create_payment_intent(amount=100, currency="usd", idempotency_key="k")
    assert caught.value.code == "parameter_invalid"
    assert caught.value.param == "amount"


def test_card_error_keeps_the_decline_code(provider, sdk):
    sdk(stripe.PaymentIntent, "create", raises=_card_error(decline_code="insufficient_funds"))
    with pytest.raises(CardError) as caught:
        provider.create_payment_intent(amount=100, currency="usd", idempotency_key="k")
    assert (caught.value.code, caught.value.decline_code) == ("card_declined", "insufficient_funds")


# -- PaymentIntent mapping -------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("requires_payment_method", PaymentIntentStatus.REQUIRES_PAYMENT_METHOD),
        ("requires_action", PaymentIntentStatus.REQUIRES_ACTION),
        ("processing", PaymentIntentStatus.PROCESSING),
        ("canceled", PaymentIntentStatus.CANCELED),
        # A status this codebase does not model folds into processing.
        ("requires_capture", PaymentIntentStatus.PROCESSING),
    ],
)
def test_payment_intent_statuses_map_onto_ours(provider, sdk, raw, expected):
    sdk(stripe.PaymentIntent, "retrieve", result=_intent(status=raw))
    assert provider.retrieve_payment_intent("pi_123").status == expected


def test_expanded_payment_method_and_errors_are_flattened(provider, sdk):
    sdk(stripe.PaymentIntent, "retrieve", result=_intent(
        payment_method={"id": "pm_expanded"}, client_secret=None,
        last_payment_error={"code": "card_declined"},
        next_action={"type": "use_stripe_sdk"}, metadata=None,
    ))

    intent = provider.retrieve_payment_intent("pi_123")

    assert intent.payment_method == "pm_expanded"
    assert intent.client_secret == ""
    assert intent.last_payment_error == {"code": "card_declined"}
    assert intent.next_action == {"type": "use_stripe_sdk"}
    assert intent.metadata == {}


# -- PaymentIntent.confirm / retrieve --------------------------------------------

def test_confirm_sends_the_return_url_and_the_key(provider, sdk):
    confirm = sdk(stripe.PaymentIntent, "confirm", result=_intent(status="requires_action"))

    intent = provider.confirm_payment_intent(
        "pi_123", idempotency_key="k:confirm", return_url=RETURN_URL
    )

    args, params = confirm.calls[0]
    assert args == ("pi_123",)
    assert params == {"api_key": API_KEY, "idempotency_key": "k:confirm",
                      "return_url": RETURN_URL}
    assert intent.status == PaymentIntentStatus.REQUIRES_ACTION


def test_confirm_sends_no_key_when_none(provider, sdk):
    confirm = sdk(stripe.PaymentIntent, "confirm", result=_intent())
    provider.confirm_payment_intent("pi_123", return_url=RETURN_URL)
    _, params = confirm.calls[0]
    assert params == {"api_key": API_KEY, "return_url": RETURN_URL}


def test_confirm_has_no_placeholder_return_url(provider, sdk):
    # It fell back to https://example.com/checkout/return: a caller that
    # forgot the URL sent real customers to example.com after 3D Secure.
    # The caller must say where the customer returns to.
    confirm = sdk(stripe.PaymentIntent, "confirm", result=_intent())
    with pytest.raises(TypeError):
        provider.confirm_payment_intent("pi_123")
    assert confirm.calls == []


@pytest.mark.parametrize(
    "raised,expected,code",
    [
        (stripe_error.IdempotencyError("reused"), IdempotencyError, None),
        (_card_error(), CardError, "card_declined"),
        (_invalid(code="resource_missing"), CardError, "resource_missing"),
        (_invalid(code="payment_intent_unexpected_state"), InvalidRequestError,
         "payment_intent_unexpected_state"),
        (stripe_error.APIConnectionError("down"), PaymentProviderTimeoutError, None),
    ],
)
def test_confirm_translates_sdk_errors(provider, sdk, raised, expected, code):
    sdk(stripe.PaymentIntent, "confirm", raises=raised)
    with pytest.raises(expected) as caught:
        provider.confirm_payment_intent("pi_123", return_url=RETURN_URL)
    if code:
        assert caught.value.code == code


@pytest.mark.parametrize(
    "raised,expected,code",
    [
        (_invalid(code="resource_missing"), CardError, "resource_missing"),
        (_invalid(code="parameter_invalid"), InvalidRequestError, "parameter_invalid"),
        (stripe_error.APIConnectionError("down"), PaymentProviderTimeoutError, None),
    ],
)
def test_retrieve_translates_sdk_errors(provider, sdk, raised, expected, code):
    sdk(stripe.PaymentIntent, "retrieve", raises=raised)
    with pytest.raises(expected) as caught:
        provider.retrieve_payment_intent("pi_missing")
    if code:
        assert caught.value.code == code


def test_retrieve_sends_the_key(provider, sdk):
    retrieve = sdk(stripe.PaymentIntent, "retrieve", result=_intent())
    provider.retrieve_payment_intent("pi_123")
    assert retrieve.calls[0] == (("pi_123",), {"api_key": API_KEY})


# -- Checkout Session ------------------------------------------------------------

def test_create_checkout_session_builds_one_line_for_the_amount(provider, sdk):
    create = sdk(stripe.checkout.Session, "create", result=_session())

    session = provider.create_checkout_session(
        amount=1999, currency="usd", idempotency_key="checkout-order-7:k",
        success_url="https://shop.example/ok", cancel_url="https://shop.example/no",
        description="Order #7", metadata={"order_id": "7"},
    )

    _, params = create.calls[0]
    assert params == {
        "mode": "payment",
        "line_items": [{"price_data": {"currency": "usd", "unit_amount": 1999,
                                       "product_data": {"name": "Order #7"}},
                        "quantity": 1}],
        "success_url": "https://shop.example/ok", "cancel_url": "https://shop.example/no",
        "idempotency_key": "checkout-order-7:k", "api_key": API_KEY,
        "metadata": {"order_id": "7"},
    }
    assert (session.id, session.status, session.amount_total, session.payment_intent) == (
        "cs_123", CheckoutSessionStatus.OPEN, 1999, None
    )


def test_create_checkout_session_defaults_the_line_name(provider, sdk):
    create = sdk(stripe.checkout.Session, "create", result=_session())
    provider.create_checkout_session(amount=1, currency="usd", idempotency_key="k",
                                     success_url="https://a/", cancel_url="https://a/")
    _, params = create.calls[0]
    assert params["line_items"][0]["price_data"]["product_data"] == {"name": "Order payment"}
    assert "metadata" not in params


def test_session_mapping_fills_gaps_and_flattens_the_intent(provider, sdk):
    sdk(stripe.checkout.Session, "retrieve", result=_session(
        status="complete", payment_status=None, amount_total=None, currency=None,
        payment_intent={"id": "pi_9"}, metadata=None, url=None,
    ))

    session = provider.retrieve_checkout_session("cs_123")

    assert session.status == CheckoutSessionStatus.COMPLETE
    assert (session.payment_status, session.amount_total, session.currency) == ("unpaid", 0, "")
    assert session.payment_intent == "pi_9"
    assert session.metadata == {} and session.url is None


@pytest.mark.parametrize(
    "raised,expected",
    [
        (stripe_error.IdempotencyError("reused"), IdempotencyError),
        (_invalid(code="url_invalid", param="success_url"), InvalidRequestError),
        (stripe_error.APIConnectionError("down"), PaymentProviderTimeoutError),
    ],
)
def test_create_checkout_session_translates_sdk_errors(provider, sdk, raised, expected):
    sdk(stripe.checkout.Session, "create", raises=raised)
    with pytest.raises(expected):
        provider.create_checkout_session(amount=1, currency="usd", idempotency_key="k",
                                         success_url="https://a/", cancel_url="https://a/")


@pytest.mark.parametrize(
    "method,raised,expected,code",
    [
        ("retrieve", _invalid(code="resource_missing"), CardError, "resource_missing"),
        ("retrieve", _invalid(code="parameter_invalid"), InvalidRequestError, "parameter_invalid"),
        ("retrieve", stripe_error.APIConnectionError("down"), PaymentProviderTimeoutError, None),
        ("expire", _invalid(code="resource_missing"), CardError, "resource_missing"),
        # Expiring a session that is not open: CardError, as in the mock.
        ("expire", _invalid(code="checkout_session_expire_failed"), CardError,
         "checkout_session_expire_failed"),
        ("expire", _invalid(code=None), CardError, "invalid_request"),
        ("expire", stripe_error.APIConnectionError("down"), PaymentProviderTimeoutError, None),
    ],
)
def test_session_retrieve_and_expire_translate_sdk_errors(provider, sdk, method, raised,
                                                          expected, code):
    sdk(stripe.checkout.Session, method, raises=raised)
    call = getattr(provider, f"{method}_checkout_session")
    with pytest.raises(expected) as caught:
        call("cs_123")
    if code:
        assert caught.value.code == code


def test_expire_returns_the_expired_session(provider, sdk):
    expire = sdk(stripe.checkout.Session, "expire", result=_session(status="expired"))
    assert provider.expire_checkout_session("cs_123").status == CheckoutSessionStatus.EXPIRED
    assert expire.calls[0] == (("cs_123",), {"api_key": API_KEY})


# -- Refund ------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [("succeeded", RefundStatus.SUCCEEDED), ("pending", RefundStatus.PENDING),
     ("failed", RefundStatus.FAILED), ("requires_action", RefundStatus.PENDING),
     (None, RefundStatus.PENDING)],
)
def test_create_refund_maps_the_result(provider, sdk, raw, expected):
    create = sdk(stripe.Refund, "create", result=_SdkObject(
        id="re_1", payment_intent="pi_123", amount=500, currency="usd", status=raw))

    refund = provider.create_refund(payment_intent_id="pi_123", amount=500,
                                    idempotency_key="refund-order-7:k")

    assert create.calls[0][1] == {"payment_intent": "pi_123", "api_key": API_KEY,
                                  "amount": 500, "idempotency_key": "refund-order-7:k"}
    assert (refund.id, refund.amount, refund.status) == ("re_1", 500, expected)


def test_full_refund_sends_no_amount_and_no_key(provider, sdk):
    create = sdk(stripe.Refund, "create", result=_SdkObject(
        id="re_1", payment_intent="pi_123", amount=1999, currency="usd", status="succeeded"))
    provider.create_refund(payment_intent_id="pi_123")
    assert create.calls[0][1] == {"payment_intent": "pi_123", "api_key": API_KEY}


@pytest.mark.parametrize(
    "raised,expected,code",
    [
        (stripe_error.IdempotencyError("reused"), IdempotencyError, None),
        (_invalid(code="resource_missing"), CardError, "resource_missing"),
        # Over-refund / already refunded: CardError, as in the mock.
        (_invalid(code="charge_already_refunded"), CardError, "charge_already_refunded"),
        (_invalid(code=None), CardError, "invalid_request"),
        (_card_error(), CardError, "card_declined"),
        (stripe_error.APIConnectionError("down"), PaymentProviderTimeoutError, None),
    ],
)
def test_create_refund_translates_sdk_errors(provider, sdk, raised, expected, code):
    sdk(stripe.Refund, "create", raises=raised)
    with pytest.raises(expected) as caught:
        provider.create_refund(payment_intent_id="pi_123", amount=100)
    if code:
        assert caught.value.code == code


# -- Webhooks (real SDK verification, no stub) --------------------------------------

WEBHOOK_SECRET = "whsec_unit_only"


def _signed(payload: bytes, secret=WEBHOOK_SECRET, timestamp=None):
    ts = timestamp or int(time.time())
    signature = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={signature}"


def _event_payload():
    return json.dumps({
        "id": "evt_1", "object": "event", "type": "payment_intent.succeeded",
        "created": 1700000000, "data": {"object": {"id": "pi_123", "object": "payment_intent"}},
    }).encode()


def test_webhook_event_verified_by_the_sdk_is_returned_as_ours(provider):
    payload = _event_payload()

    event = provider.construct_webhook_event(payload, _signed(payload), WEBHOOK_SECRET)

    assert (event.id, event.type, event.created) == ("evt_1", "payment_intent.succeeded",
                                                     1700000000)
    assert event.data["object"]["id"] == "pi_123"


@pytest.mark.parametrize(
    "header",
    [None, "", "t=1,v1=deadbeef", "not-a-signature"],
)
def test_webhook_with_a_missing_or_wrong_signature_is_rejected(provider, header):
    with pytest.raises(SignatureVerificationError):
        provider.construct_webhook_event(_event_payload(), header, WEBHOOK_SECRET)


def test_webhook_signed_with_another_secret_or_too_long_ago_is_rejected(provider):
    payload = _event_payload()
    with pytest.raises(SignatureVerificationError):
        provider.construct_webhook_event(payload, _signed(payload, secret="whsec_other"),
                                         WEBHOOK_SECRET)
    with pytest.raises(SignatureVerificationError):
        provider.construct_webhook_event(
            payload, _signed(payload, timestamp=int(time.time()) - 3600), WEBHOOK_SECRET,
            tolerance_seconds=300,
        )
