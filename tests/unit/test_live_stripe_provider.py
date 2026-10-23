"""
LiveStripePaymentProvider without the network: every Stripe SDK call it
makes is replaced with a stub, so these tests check what the adapter does
around the SDK - the parameters it sends (the client's key, idempotency
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
    return stripe.CardError(
        "Your card was declined.", None, code,
        json_body={"error": {"message": "Your card was declined.", "code": code,
                             "decline_code": decline_code}},
    )


def _invalid(code=None, param="amount", message="Invalid request"):
    return stripe.InvalidRequestError(message, param, code=code)


@pytest.fixture
def provider():
    return LiveStripePaymentProvider(api_key=API_KEY)


@pytest.fixture
def sdk(monkeypatch):
    """Install a stub for one SDK method: sdk(stripe.PaymentIntentService, "create", result=...)."""
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
    create = sdk(stripe.PaymentIntentService, "create", result=_intent(status="requires_confirmation"))

    intent = provider.create_payment_intent(
        amount=1999, currency="usd", idempotency_key="pay-order-7:k",
        payment_method="pm_card_visa", metadata={"order_id": "7"},
    )

    assert create.calls[0] == ((), {
        "params": {
            "amount": 1999, "currency": "usd", "allowed_payment_method_types": ["card"],
            "payment_method": "pm_card_visa", "metadata": {"order_id": "7"},
        },
        "options": {"idempotency_key": "pay-order-7:k"},
    })
    assert provider._client._requestor._options.api_key == API_KEY
    assert stripe.api_key != API_KEY  # never set globally
    assert (intent.id, intent.amount, intent.status) == (
        "pi_123", 1999, PaymentIntentStatus.REQUIRES_CONFIRMATION
    )


def test_create_payment_intent_leaves_out_unset_optionals(provider, sdk):
    create = sdk(stripe.PaymentIntentService, "create", result=_intent())
    provider.create_payment_intent(amount=100, currency="usd", idempotency_key="k")
    params = create.calls[0][1]["params"]
    assert "payment_method" not in params and "metadata" not in params


@pytest.mark.parametrize(
    "raised,expected",
    [
        (stripe.IdempotencyError("Keys for idempotent requests..."), IdempotencyError),
        (_card_error(), CardError),
        (_invalid(code="parameter_invalid_integer"), InvalidRequestError),
        (stripe.APIConnectionError("network down"), PaymentProviderTimeoutError),
    ],
)
def test_create_payment_intent_translates_sdk_errors(provider, sdk, raised, expected):
    sdk(stripe.PaymentIntentService, "create", raises=raised)
    with pytest.raises(expected):
        provider.create_payment_intent(amount=100, currency="usd", idempotency_key="k")


def test_invalid_request_without_a_code_gets_a_generic_one(provider, sdk):
    sdk(stripe.PaymentIntentService, "create", raises=_invalid(code=None))
    with pytest.raises(InvalidRequestError) as caught:
        provider.create_payment_intent(amount=100, currency="usd", idempotency_key="k")
    assert caught.value.code == "parameter_invalid"
    assert caught.value.param == "amount"


def test_card_error_keeps_the_decline_code(provider, sdk):
    sdk(stripe.PaymentIntentService, "create", raises=_card_error(decline_code="insufficient_funds"))
    with pytest.raises(CardError) as caught:
        provider.create_payment_intent(amount=100, currency="usd", idempotency_key="k")
    assert (caught.value.code, caught.value.decline_code) == ("card_declined", "insufficient_funds")


def test_a_card_error_without_a_body_or_code_is_a_plain_decline(provider, sdk):
    # The SDK builds e.error from the response body; with none (a decline
    # the SDK raised without one) there is no decline code to keep.
    sdk(stripe.PaymentIntentService, "confirm",
        raises=stripe.CardError("Your card was declined.", None, None))
    with pytest.raises(CardError) as caught:
        provider.confirm_payment_intent("pi_123", return_url=RETURN_URL)
    assert (caught.value.code, caught.value.decline_code) == ("card_declined", None)


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
    sdk(stripe.PaymentIntentService, "retrieve", result=_intent(status=raw))
    assert provider.retrieve_payment_intent("pi_123").status == expected


def test_expanded_payment_method_and_errors_are_flattened(provider, sdk):
    sdk(stripe.PaymentIntentService, "retrieve", result=_intent(
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
    confirm = sdk(stripe.PaymentIntentService, "confirm", result=_intent(status="requires_action"))

    intent = provider.confirm_payment_intent(
        "pi_123", idempotency_key="k:confirm", return_url=RETURN_URL
    )

    assert confirm.calls[0] == (("pi_123",), {
        "params": {"return_url": RETURN_URL}, "options": {"idempotency_key": "k:confirm"},
    })
    assert intent.status == PaymentIntentStatus.REQUIRES_ACTION


def test_confirm_sends_no_key_when_none(provider, sdk):
    confirm = sdk(stripe.PaymentIntentService, "confirm", result=_intent())
    provider.confirm_payment_intent("pi_123", return_url=RETURN_URL)
    assert confirm.calls[0] == (("pi_123",), {"params": {"return_url": RETURN_URL},
                                              "options": {}})


def test_confirm_has_no_placeholder_return_url(provider, sdk):
    # It fell back to https://example.com/checkout/return: a caller that
    # forgot the URL sent real customers to example.com after 3D Secure.
    # The caller must say where the customer returns to.
    confirm = sdk(stripe.PaymentIntentService, "confirm", result=_intent())
    with pytest.raises(TypeError):
        provider.confirm_payment_intent("pi_123")
    assert confirm.calls == []


@pytest.mark.parametrize(
    "raised,expected,code",
    [
        (stripe.IdempotencyError("reused"), IdempotencyError, None),
        (_card_error(), CardError, "card_declined"),
        (_invalid(code="resource_missing"), CardError, "resource_missing"),
        (_invalid(code="payment_intent_unexpected_state"), InvalidRequestError,
         "payment_intent_unexpected_state"),
        (stripe.APIConnectionError("down"), PaymentProviderTimeoutError, None),
    ],
)
def test_confirm_translates_sdk_errors(provider, sdk, raised, expected, code):
    sdk(stripe.PaymentIntentService, "confirm", raises=raised)
    with pytest.raises(expected) as caught:
        provider.confirm_payment_intent("pi_123", return_url=RETURN_URL)
    if code:
        assert caught.value.code == code


@pytest.mark.parametrize(
    "raised,expected,code",
    [
        (_invalid(code="resource_missing"), CardError, "resource_missing"),
        (_invalid(code="parameter_invalid"), InvalidRequestError, "parameter_invalid"),
        (stripe.APIConnectionError("down"), PaymentProviderTimeoutError, None),
    ],
)
def test_retrieve_translates_sdk_errors(provider, sdk, raised, expected, code):
    sdk(stripe.PaymentIntentService, "retrieve", raises=raised)
    with pytest.raises(expected) as caught:
        provider.retrieve_payment_intent("pi_missing")
    if code:
        assert caught.value.code == code


def test_retrieve_sends_the_key(provider, sdk):
    retrieve = sdk(stripe.PaymentIntentService, "retrieve", result=_intent())
    provider.retrieve_payment_intent("pi_123")
    assert retrieve.calls[0] == (("pi_123",), {})


# -- Checkout Session ------------------------------------------------------------

def test_create_checkout_session_builds_one_line_for_the_amount(provider, sdk):
    create = sdk(stripe.checkout.SessionService, "create", result=_session())

    session = provider.create_checkout_session(
        amount=1999, currency="usd", idempotency_key="checkout-order-7:k",
        success_url="https://shop.example/ok", cancel_url="https://shop.example/no",
        description="Order #7", metadata={"order_id": "7"},
    )

    assert create.calls[0][1]["options"] == {"idempotency_key": "checkout-order-7:k"}
    assert create.calls[0][1]["params"] == {
        "mode": "payment",
        "line_items": [{"price_data": {"currency": "usd", "unit_amount": 1999,
                                       "product_data": {"name": "Order #7"}},
                        "quantity": 1}],
        "success_url": "https://shop.example/ok", "cancel_url": "https://shop.example/no",
        "metadata": {"order_id": "7"},
    }
    assert (session.id, session.status, session.amount_total, session.payment_intent) == (
        "cs_123", CheckoutSessionStatus.OPEN, 1999, None
    )


def test_create_checkout_session_defaults_the_line_name(provider, sdk):
    create = sdk(stripe.checkout.SessionService, "create", result=_session())
    provider.create_checkout_session(amount=1, currency="usd", idempotency_key="k",
                                     success_url="https://a/", cancel_url="https://a/")
    params = create.calls[0][1]["params"]
    assert params["line_items"][0]["price_data"]["product_data"] == {"name": "Order payment"}
    assert "metadata" not in params


def test_session_mapping_fills_gaps_and_flattens_the_intent(provider, sdk):
    sdk(stripe.checkout.SessionService, "retrieve", result=_session(
        status="complete", payment_status=None, amount_total=None, currency=None,
        payment_intent={"id": "pi_9"}, metadata=None, url=None,
    ))

    session = provider.retrieve_checkout_session("cs_123")

    assert session.status == CheckoutSessionStatus.COMPLETE
    assert (session.payment_status, session.amount_total, session.currency) == ("unpaid", 0, "")
    assert session.payment_intent == "pi_9"
    assert session.metadata == {} and session.url is None


def test_currencies_come_back_in_upper_case_like_the_orders(provider, sdk):
    # Stripe answers in lower case ("usd"); orders, and every amount the API
    # returns, carry upper-case ISO codes ("USD"). RefundResponse.currency is
    # the refund's own: it said "usd" in production and "USD" on the mock.
    sdk(stripe.PaymentIntentService, "retrieve", result=_intent(currency="usd"))
    sdk(stripe.checkout.SessionService, "retrieve", result=_session(currency="eur"))
    sdk(stripe.RefundService, "create", result=_SdkObject(
        id="re_1", payment_intent="pi_123", amount=5, currency="pln", status="succeeded"))

    assert provider.retrieve_payment_intent("pi_123").currency == "USD"
    assert provider.retrieve_checkout_session("cs_123").currency == "EUR"
    assert provider.create_refund(payment_intent_id="pi_123").currency == "PLN"


@pytest.mark.parametrize(
    "raised,expected",
    [
        (stripe.IdempotencyError("reused"), IdempotencyError),
        (_invalid(code="url_invalid", param="success_url"), InvalidRequestError),
        (stripe.APIConnectionError("down"), PaymentProviderTimeoutError),
    ],
)
def test_create_checkout_session_translates_sdk_errors(provider, sdk, raised, expected):
    sdk(stripe.checkout.SessionService, "create", raises=raised)
    with pytest.raises(expected):
        provider.create_checkout_session(amount=1, currency="usd", idempotency_key="k",
                                         success_url="https://a/", cancel_url="https://a/")


@pytest.mark.parametrize(
    "method,raised,expected,code",
    [
        ("retrieve", _invalid(code="resource_missing"), CardError, "resource_missing"),
        ("retrieve", _invalid(code="parameter_invalid"), InvalidRequestError, "parameter_invalid"),
        ("retrieve", stripe.APIConnectionError("down"), PaymentProviderTimeoutError, None),
        ("expire", _invalid(code="resource_missing"), CardError, "resource_missing"),
        # Expiring a session that is not open: CardError, as in the mock.
        ("expire", _invalid(code="checkout_session_expire_failed"), CardError,
         "checkout_session_expire_failed"),
        ("expire", _invalid(code=None), CardError, "invalid_request"),
        ("expire", stripe.APIConnectionError("down"), PaymentProviderTimeoutError, None),
    ],
)
def test_session_retrieve_and_expire_translate_sdk_errors(provider, sdk, method, raised,
                                                          expected, code):
    sdk(stripe.checkout.SessionService, method, raises=raised)
    call = getattr(provider, f"{method}_checkout_session")
    with pytest.raises(expected) as caught:
        call("cs_123")
    if code:
        assert caught.value.code == code


def test_expire_returns_the_expired_session(provider, sdk):
    expire = sdk(stripe.checkout.SessionService, "expire", result=_session(status="expired"))
    assert provider.expire_checkout_session("cs_123").status == CheckoutSessionStatus.EXPIRED
    assert expire.calls[0] == (("cs_123",), {})


# -- Refund ------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [("succeeded", RefundStatus.SUCCEEDED), ("pending", RefundStatus.PENDING),
     ("failed", RefundStatus.FAILED), ("requires_action", RefundStatus.PENDING),
     (None, RefundStatus.PENDING)],
)
def test_create_refund_maps_the_result(provider, sdk, raw, expected):
    create = sdk(stripe.RefundService, "create", result=_SdkObject(
        id="re_1", payment_intent="pi_123", amount=500, currency="usd", status=raw))

    refund = provider.create_refund(payment_intent_id="pi_123", amount=500,
                                    idempotency_key="refund-order-7:k")

    assert create.calls[0] == ((), {
        "params": {"payment_intent": "pi_123", "amount": 500},
        "options": {"idempotency_key": "refund-order-7:k"},
    })
    assert (refund.id, refund.amount, refund.status) == ("re_1", 500, expected)


def test_full_refund_sends_no_amount_and_no_key(provider, sdk):
    create = sdk(stripe.RefundService, "create", result=_SdkObject(
        id="re_1", payment_intent="pi_123", amount=1999, currency="usd", status="succeeded"))
    provider.create_refund(payment_intent_id="pi_123")
    assert create.calls[0] == ((), {"params": {"payment_intent": "pi_123"}, "options": {}})


@pytest.mark.parametrize(
    "raised,expected,code",
    [
        (stripe.IdempotencyError("reused"), IdempotencyError, None),
        (_invalid(code="resource_missing"), CardError, "resource_missing"),
        # Over-refund / already refunded: CardError, as in the mock.
        (_invalid(code="charge_already_refunded"), CardError, "charge_already_refunded"),
        (_invalid(code=None), CardError, "invalid_request"),
        (_card_error(), CardError, "card_declined"),
        (stripe.APIConnectionError("down"), PaymentProviderTimeoutError, None),
    ],
)
def test_create_refund_translates_sdk_errors(provider, sdk, raised, expected, code):
    sdk(stripe.RefundService, "create", raises=raised)
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


# -- HTTP client: explicit timeout and retries -----------------------------------

@pytest.fixture
def fresh_stripe_client():
    """No client cached by an earlier test."""
    from app.services.payment.live_stripe_provider import _build_stripe_client

    _build_stripe_client.cache_clear()
    yield
    _build_stripe_client.cache_clear()


def _http_client(provider):
    return provider._client._requestor._client


def test_the_stripe_client_is_built_with_the_configured_timeout_and_retries(
    fresh_stripe_client, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "STRIPE_CONNECT_TIMEOUT_SECONDS", 3.5)
    monkeypatch.setattr(settings, "STRIPE_READ_TIMEOUT_SECONDS", 12.0)
    monkeypatch.setattr(settings, "STRIPE_MAX_NETWORK_RETRIES", 1)

    provider = LiveStripePaymentProvider(api_key=API_KEY)

    assert isinstance(_http_client(provider), stripe.RequestsClient)
    assert _http_client(provider)._timeout == (3.5, 12.0)
    assert provider._client._requestor._options.max_network_retries == 1


def test_the_default_timeout_is_far_below_the_sdks_80_seconds(fresh_stripe_client):
    provider = LiveStripePaymentProvider(api_key=API_KEY)

    assert _http_client(provider)._timeout == (5.0, 20.0)
    assert provider._client._requestor._options.max_network_retries == 2


def test_building_a_provider_touches_no_sdk_global(fresh_stripe_client):
    before = (stripe.api_key, stripe.default_http_client, stripe.max_network_retries)

    LiveStripePaymentProvider(api_key=API_KEY)

    assert (stripe.api_key, stripe.default_http_client, stripe.max_network_retries) == before


def test_providers_share_one_client_and_a_settings_change_builds_a_new_one(
    fresh_stripe_client, monkeypatch
):
    from app.core.config import settings

    first = LiveStripePaymentProvider(api_key=API_KEY)._client
    assert LiveStripePaymentProvider(api_key=API_KEY)._client is first
    assert LiveStripePaymentProvider(api_key="another-key")._client is not first

    monkeypatch.setattr(settings, "STRIPE_READ_TIMEOUT_SECONDS", 7.0)
    changed = LiveStripePaymentProvider(api_key=API_KEY)
    assert changed._client is not first
    assert _http_client(changed)._timeout == (5.0, 7.0)


def test_a_call_through_the_sdk_uses_the_configured_timeout(fresh_stripe_client, monkeypatch):
    # The SDK's own request path (not a stub of it): the requests session is
    # called with the (connect, read) pair.
    seen = {}

    class _Session:
        def request(self, method, url, **kwargs):
            seen.update(kwargs)
            raise __import__("requests").exceptions.ConnectTimeout("slow")

    from app.core.config import settings

    monkeypatch.setattr(settings, "STRIPE_MAX_NETWORK_RETRIES", 0)
    provider = LiveStripePaymentProvider(api_key=API_KEY)
    monkeypatch.setattr(_http_client(provider), "_session", _Session())

    with pytest.raises(PaymentProviderTimeoutError):
        provider.retrieve_payment_intent("pi_1")

    assert seen["timeout"] == (5.0, 20.0)
