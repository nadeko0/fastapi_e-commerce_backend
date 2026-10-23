"""
Tests written from mutation testing of the payment layer (mutmut): each one
pins a behaviour a mutant of app/services/payment/stripe_provider.py changed
without any existing test noticing. They state what the mock tells its caller
- the same things the live adapter's Stripe errors and events carry - so a
caller that reads error.message / error.code or an event's fields keeps
working.
"""
import hashlib
import hmac
import json
import re
import time

import pytest
import stripe
from pydantic import ValidationError
from stripe import error as stripe_error

import app.services.payment.live_stripe_provider as live_module
from app.core.config import settings
from app.schemas.order import CheckoutSessionCreate, _url_origin
from app.services.payment.exceptions import (
    CardError,
    IdempotencyError,
    InvalidRequestError,
    PaymentProviderTimeoutError,
    SignatureVerificationError,
)
from app.services.payment.live_stripe_provider import LiveStripePaymentProvider, _signed_at
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.payment.types import CheckoutSessionStatus

RETURN_URL = "http://localhost:3000/checkout/return?order_id=7"
SUCCESS_URL = "http://localhost:3000/checkout/success?session_id={CHECKOUT_SESSION_ID}"
CANCEL_URL = "http://localhost:3000/checkout/cancel"


@pytest.fixture
def provider():
    StripePaymentProvider.reset_mock_state()
    yield StripePaymentProvider()
    StripePaymentProvider.reset_mock_state()


def _session(provider):
    return provider.create_checkout_session(
        amount=1999,
        currency="EUR",
        idempotency_key="key-1",
        success_url=SUCCESS_URL,
        cancel_url=CANCEL_URL,
    )


def test_an_unknown_payment_intent_names_itself_in_the_error(provider):
    with pytest.raises(CardError) as missing:
        provider.retrieve_payment_intent("pi_nope")
    assert missing.value.code == "resource_missing"
    assert missing.value.message == "No such payment_intent: 'pi_nope'"


def test_an_unknown_checkout_session_names_itself_in_the_error(provider):
    with pytest.raises(CardError) as missing:
        provider.retrieve_checkout_session("cs_nope")
    assert missing.value.code == "resource_missing"
    assert missing.value.message == "No such checkout.session: 'cs_nope'"


def test_expiring_an_unknown_checkout_session_names_itself_in_the_error(provider):
    with pytest.raises(CardError) as missing:
        provider.expire_checkout_session("cs_nope")
    assert missing.value.code == "resource_missing"
    assert missing.value.message == "No such checkout.session: 'cs_nope'"


def test_expiring_a_session_twice_says_why_and_with_which_code(provider):
    session = _session(provider)
    assert (
        provider.expire_checkout_session(session.id).status == CheckoutSessionStatus.EXPIRED
    )

    with pytest.raises(CardError) as refused:
        provider.expire_checkout_session(session.id)

    assert refused.value.code == "checkout_session_expire_failed"
    assert refused.value.message == (
        f"Checkout Session '{session.id}' cannot be expired from status 'expired'"
    )


def test_settling_an_unknown_refund_names_it_in_the_error(provider):
    with pytest.raises(CardError) as missing:
        StripePaymentProvider.settle_refund("re_nope")
    assert missing.value.code == "resource_missing"
    assert missing.value.message == "No such refund: 're_nope'"


def test_settling_a_refund_with_nothing_left_to_settle_names_it(provider):
    intent = provider.create_payment_intent(
        amount=5000, currency="EUR", idempotency_key="pi-key", payment_method="pm_card_visa"
    )
    provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)
    refund = provider.create_refund(payment_intent_id=intent.id)

    with pytest.raises(ValueError) as nothing:
        StripePaymentProvider.settle_refund(refund.id)

    assert str(nothing.value) == f"Refund {refund.id} has nothing left to settle"


def test_a_webhook_event_has_stripes_v1_snapshot_shape():
    before = int(time.time())
    event = StripePaymentProvider.webhook_event("charge.dispute.created", {"id": "du_1"})
    after = int(time.time())

    assert set(event) == {"id", "object", "type", "created", "livemode", "data"}
    assert re.fullmatch(r"evt_[0-9a-f]{24}", event["id"])
    assert event["object"] == "event"
    assert event["type"] == "charge.dispute.created"
    assert before <= event["created"] <= after
    assert event["livemode"] is False
    assert event["data"] == {"object": {"id": "du_1"}}


# -- LiveStripePaymentProvider ----------------------------------------------------
#
# The Stripe SDK is stubbed; what is pinned is what the adapter makes of each
# SDK answer and exception. Every SDK exception below carries a request id, so
# str(error) ("Request req_1: <message>") differs from error.user_message
# (the message): the adapter must report the message, not the string form.

API_KEY = "stripe-secret-key-for-tests-not-a-real-key"
HEADERS = {"request-id": "req_1"}


class _SdkObject:
    def __init__(self, **values):
        self._values = values

    def to_dict(self):
        return dict(self._values)


def _raising(error):
    def stub(*args, **kwargs):
        raise error
    return stub


@pytest.fixture
def live():
    return LiveStripePaymentProvider(api_key=API_KEY)


def test_a_live_provider_without_a_key_says_what_it_needs():
    with pytest.raises(ValueError) as refused:
        LiveStripePaymentProvider(api_key="")
    assert str(refused.value) == "LiveStripePaymentProvider requires a real Stripe secret key"


def _create_intent(p):
    return p.create_payment_intent(amount=100, currency="usd", idempotency_key="k")


def _confirm_intent(p):
    return p.confirm_payment_intent("pi_1", return_url=RETURN_URL)


def _retrieve_intent(p):
    return p.retrieve_payment_intent("pi_1")


def _create_session(p):
    return p.create_checkout_session(
        amount=100, currency="usd", idempotency_key="k",
        success_url=SUCCESS_URL, cancel_url=CANCEL_URL,
    )


def _retrieve_session(p):
    return p.retrieve_checkout_session("cs_1")


def _expire_session(p):
    return p.expire_checkout_session("cs_1")


def _create_refund(p):
    return p.create_refund(payment_intent_id="pi_1", amount=50)


# call, SDK owner, SDK method, what "no such object" names (None: resource_missing
# is not special-cased), and what a refused request becomes: (class, code used
# when Stripe sent none).
_CALLS = {
    "create_payment_intent": (_create_intent, stripe.PaymentIntent, "create",
                              None, (InvalidRequestError, "parameter_invalid")),
    "confirm_payment_intent": (_confirm_intent, stripe.PaymentIntent, "confirm",
                               "payment_intent: 'pi_1'", (InvalidRequestError, "parameter_invalid")),
    "retrieve_payment_intent": (_retrieve_intent, stripe.PaymentIntent, "retrieve",
                                "payment_intent: 'pi_1'", (InvalidRequestError, "parameter_invalid")),
    "create_checkout_session": (_create_session, stripe.checkout.Session, "create",
                                None, (InvalidRequestError, "parameter_invalid")),
    "retrieve_checkout_session": (_retrieve_session, stripe.checkout.Session, "retrieve",
                                  "checkout.session: 'cs_1'", (InvalidRequestError, "parameter_invalid")),
    "expire_checkout_session": (_expire_session, stripe.checkout.Session, "expire",
                                "checkout.session: 'cs_1'", (CardError, "invalid_request")),
    "create_refund": (_create_refund, stripe.Refund, "create",
                      "payment_intent: 'pi_1'", (CardError, "invalid_request")),
}
_IDEMPOTENT = ["create_payment_intent", "confirm_payment_intent", "create_checkout_session",
               "create_refund"]
_CARD = ["create_payment_intent", "confirm_payment_intent", "create_refund"]


@pytest.mark.parametrize("name", list(_CALLS))
def test_a_connection_failure_becomes_a_timeout_with_stripes_message(live, monkeypatch, name):
    call, owner, method, _, _ = _CALLS[name]
    monkeypatch.setattr(owner, method, _raising(
        stripe_error.APIConnectionError("network down", headers=HEADERS)))

    with pytest.raises(PaymentProviderTimeoutError) as caught:
        call(live)

    assert caught.value.message == "network down"
    assert caught.value.code == "api_connection_error"


_UNAVAILABLE = [
    stripe_error.RateLimitError,
    stripe_error.APIError,
    stripe_error.AuthenticationError,
    stripe_error.PermissionError,
    stripe_error.StripeError,
]


@pytest.mark.parametrize("error_class", _UNAVAILABLE)
@pytest.mark.parametrize("name", list(_CALLS))
def test_any_other_stripe_error_is_a_provider_timeout_not_a_500(
    live, monkeypatch, name, error_class
):
    # Rate limit, a 5xx, a refused key: none says whether the request was
    # carried out, so each is the same "outcome unknown" as a dropped
    # connection and reaches the routes as PaymentProviderTimeoutError (503)
    # instead of escaping as a StripeError (500) that leaves the attempt
    # in flight.
    call, owner, method, _, _ = _CALLS[name]
    monkeypatch.setattr(owner, method, _raising(error_class("stripe words", headers=HEADERS)))

    with pytest.raises(PaymentProviderTimeoutError) as caught:
        call(live)

    assert caught.value.message == "stripe words"


@pytest.mark.parametrize("error_class", [
    stripe_error.AuthenticationError, stripe_error.PermissionError])
def test_a_refused_api_key_is_logged_as_critical(live, monkeypatch, caplog, error_class):
    monkeypatch.setattr(stripe.PaymentIntent, "create", _raising(error_class("bad key", headers=HEADERS)))

    with (
        caplog.at_level("CRITICAL", logger=live_module.logger.name),
        pytest.raises(PaymentProviderTimeoutError),
    ):
        _create_intent(live)

    assert [r.levelname for r in caplog.records] == ["CRITICAL"]
    assert error_class.__name__ in caplog.records[0].getMessage()


def test_a_rate_limit_is_not_logged_as_critical(live, monkeypatch, caplog):
    monkeypatch.setattr(stripe.PaymentIntent, "create",
                        _raising(stripe_error.RateLimitError("slow down", headers=HEADERS)))

    with (
        caplog.at_level("CRITICAL", logger=live_module.logger.name),
        pytest.raises(PaymentProviderTimeoutError),
    ):
        _create_intent(live)

    assert caplog.records == []


@pytest.mark.parametrize("name", _IDEMPOTENT)
def test_an_idempotency_conflict_keeps_stripes_message(live, monkeypatch, name):
    call, owner, method, _, _ = _CALLS[name]
    monkeypatch.setattr(owner, method, _raising(
        stripe_error.IdempotencyError("key reused", headers=HEADERS)))

    with pytest.raises(IdempotencyError) as caught:
        call(live)

    assert caught.value.message == "key reused"
    assert caught.value.code == "idempotency_key_in_use"


@pytest.mark.parametrize("name", [n for n in _CALLS if _CALLS[n][3]])
def test_a_missing_object_is_a_card_error_naming_it(live, monkeypatch, name):
    call, owner, method, what, _ = _CALLS[name]
    monkeypatch.setattr(owner, method, _raising(stripe_error.InvalidRequestError(
        "stripe words", "id", code="resource_missing", headers=HEADERS)))

    with pytest.raises(CardError) as caught:
        call(live)

    assert caught.value.code == "resource_missing"
    assert caught.value.message == f"No such {what}"


@pytest.mark.parametrize("name", list(_CALLS))
def test_a_refused_request_keeps_stripes_message_param_and_code(live, monkeypatch, name):
    call, owner, method, _, (expected_class, _) = _CALLS[name]
    monkeypatch.setattr(owner, method, _raising(stripe_error.InvalidRequestError(
        "amount too small", "amount", code="amount_too_small", headers=HEADERS)))

    with pytest.raises(expected_class) as caught:
        call(live)

    assert type(caught.value) is expected_class
    assert caught.value.message == "amount too small"
    assert caught.value.code == "amount_too_small"
    if expected_class is InvalidRequestError:
        assert caught.value.param == "amount"


@pytest.mark.parametrize("name", list(_CALLS))
def test_a_refused_request_without_a_code_gets_the_default_one(live, monkeypatch, name):
    call, owner, method, _, (expected_class, default_code) = _CALLS[name]
    monkeypatch.setattr(owner, method, _raising(stripe_error.InvalidRequestError(
        "bad request", None, code=None, headers=HEADERS)))

    with pytest.raises(expected_class) as caught:
        call(live)

    assert caught.value.code == default_code
    if expected_class is InvalidRequestError:
        assert caught.value.param is None


def _card_declined(code="card_declined", decline_code="insufficient_funds"):
    error = {"message": "Your card was declined.", "code": code}
    if decline_code:
        error["decline_code"] = decline_code
    return stripe_error.CardError(
        "Your card was declined.", None, code, json_body={"error": error}, headers=HEADERS)


@pytest.mark.parametrize("name", _CARD)
def test_a_decline_keeps_stripes_message_code_and_decline_code(live, monkeypatch, name):
    call, owner, method, _, _ = _CALLS[name]
    monkeypatch.setattr(owner, method, _raising(_card_declined(code="expired_card")))

    with pytest.raises(CardError) as caught:
        call(live)

    assert caught.value.message == "Your card was declined."
    assert caught.value.code == "expired_card"
    assert caught.value.decline_code == "insufficient_funds"


@pytest.mark.parametrize("name", _CARD)
def test_a_decline_without_a_code_or_decline_code_gets_the_defaults(live, monkeypatch, name):
    call, owner, method, _, _ = _CALLS[name]
    declined = stripe_error.CardError(
        "Declined.", None, None, json_body={"error": {"message": "Declined."}}, headers=HEADERS)
    monkeypatch.setattr(owner, method, _raising(declined))

    with pytest.raises(CardError) as caught:
        call(live)

    assert caught.value.message == "Declined."
    assert caught.value.code == "card_declined"
    assert caught.value.decline_code is None


def test_a_checkout_session_of_an_unknown_status_is_taken_as_open(live, monkeypatch):
    monkeypatch.setattr(stripe.checkout.Session, "retrieve", lambda *a, **k: _SdkObject(
        id="cs_1", status="something_new", payment_status="paid", amount_total=100,
        currency="usd"))

    session = live.retrieve_checkout_session("cs_1")

    assert session.status == CheckoutSessionStatus.OPEN
    assert session.payment_status == "paid"


def test_a_checkout_session_without_a_payment_status_is_unpaid(live, monkeypatch):
    monkeypatch.setattr(stripe.checkout.Session, "retrieve", lambda *a, **k: _SdkObject(
        id="cs_1", status="open", amount_total=100, currency="usd"))

    assert live.retrieve_checkout_session("cs_1").payment_status == "unpaid"


@pytest.mark.parametrize("header,signed", [
    ("t=1700000000,v1=abc", 1700000000),
    ("v1=abc,t=1700000000", 1700000000),
    ("t=5,v1=a=b", 5),
])
def test_signed_at_reads_the_t_item(header, signed):
    assert _signed_at(header) == signed


def test_signed_at_reads_an_item_the_way_the_sdk_does():
    # The first item named t, split at its first "=" only: "1=2" is its value,
    # not a number. A reader that split more, or from the right, would skip
    # this item or take "1" / "2" instead.
    with pytest.raises(ValueError):
        _signed_at("t=1=2,t=5")


WEBHOOK_SECRET = "webhook-secret-for-tests-only"


def _signed(payload: bytes, timestamp: int):
    signature = hmac.new(
        WEBHOOK_SECRET.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={signature}"


PAYLOAD = json.dumps({"id": "evt_1", "type": "x.y", "created": 1, "data": {}}).encode()


def test_a_missing_signature_header_is_refused_with_its_message(live):
    with pytest.raises(SignatureVerificationError) as caught:
        live.construct_webhook_event(PAYLOAD, None, WEBHOOK_SECRET)
    assert caught.value.message == "Missing Stripe-Signature header"
    assert caught.value.code == "signature_verification_failed"


def test_a_non_ascii_signature_header_is_refused_and_carried_in_the_error(live):
    header = "t=1,v1=café"
    with pytest.raises(SignatureVerificationError) as caught:
        live.construct_webhook_event(PAYLOAD, header, WEBHOOK_SECRET)
    assert caught.value.message == "Malformed Stripe-Signature header"
    assert caught.value.sig_header == header


def test_a_body_that_is_not_utf8_is_refused_and_the_header_carried(live):
    header = _signed(b"\xff\xfe", int(time.time()))
    with pytest.raises(SignatureVerificationError) as caught:
        live.construct_webhook_event(b"\xff\xfe", header, WEBHOOK_SECRET)
    assert caught.value.message == "Payload is not valid UTF-8"
    assert caught.value.sig_header == header


def test_a_bad_signature_keeps_the_sdks_message_and_the_header(live):
    header = f"t={int(time.time())},v1=" + "0" * 64
    with pytest.raises(SignatureVerificationError) as caught:
        live.construct_webhook_event(PAYLOAD, header, WEBHOOK_SECRET)
    assert caught.value.message.startswith("No signatures found matching")
    assert caught.value.sig_header == header


def test_a_signature_dated_in_the_future_is_refused_with_its_message(live):
    header = _signed(PAYLOAD, int(time.time()) + 3600)
    with pytest.raises(SignatureVerificationError) as caught:
        live.construct_webhook_event(PAYLOAD, header, WEBHOOK_SECRET)
    assert caught.value.message == "Timestamp outside the tolerance zone"
    assert caught.value.sig_header == header


def test_the_default_tolerance_is_300_seconds(live, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(live_module.time, "time", lambda: now)

    ok = live.construct_webhook_event(PAYLOAD, _signed(PAYLOAD, now - 300), WEBHOOK_SECRET)
    assert ok.id == "evt_1"
    with pytest.raises(SignatureVerificationError):
        live.construct_webhook_event(PAYLOAD, _signed(PAYLOAD, now - 301), WEBHOOK_SECRET)
    with pytest.raises(SignatureVerificationError):
        live.construct_webhook_event(PAYLOAD, _signed(PAYLOAD, now + 301), WEBHOOK_SECRET)


def test_a_verified_body_that_is_not_json_carries_the_header_in_the_error(live):
    body = b"not json"
    header = _signed(body, int(time.time()))
    with pytest.raises(SignatureVerificationError) as caught:
        live.construct_webhook_event(body, header, WEBHOOK_SECRET)
    assert caught.value.message == "Payload is not valid JSON"
    assert caught.value.sig_header == header


# -- app/schemas/order.py: the origin a checkout redirect must be on -----------------

@pytest.mark.parametrize("url,origin", [
    ("http://shop.example/cart", ("http", "shop.example", 80)),
    ("https://shop.example/cart", ("https", "shop.example", 443)),
    ("HTTPS://Shop.Example/cart", ("https", "shop.example", 443)),
    ("http://shop.example:3000/cart", ("http", "shop.example", 3000)),
    ("https://shop.example:8443/cart", ("https", "shop.example", 8443)),
])
def test_url_origin_is_scheme_lower_case_host_and_port_with_the_scheme_default(url, origin):
    assert _url_origin(url) == origin


@pytest.mark.parametrize("url", [
    "ftp://shop.example/cart",
    "javascript://shop.example/cart",
    "//shop.example/cart",
    "http:///cart",
    "http://user@shop.example/cart",
    "https://user:secret@shop.example/cart",
    "http://shop.example:notaport/cart",
])
def test_url_origin_is_none_for_anything_but_a_plain_http_url(url):
    assert _url_origin(url) is None


@pytest.fixture
def frontend(monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://shop.example")


@pytest.mark.parametrize("url", [
    "https://shop.example/checkout/success",
    "https://shop.example:443/checkout/success",
    "https://SHOP.example/checkout/success",
])
def test_a_redirect_on_the_frontend_origin_is_accepted(frontend, url):
    assert CheckoutSessionCreate(success_url=url, cancel_url=url).success_url == url


@pytest.mark.parametrize("url", [
    "http://shop.example/checkout/success",
    "https://shop.example:8443/checkout/success",
    "https://evil.example/checkout/success",
    "https://user@shop.example/checkout/success",
    "ftp://shop.example/checkout/success",
])
def test_a_redirect_off_the_frontend_origin_is_refused(frontend, url):
    with pytest.raises(ValidationError):
        CheckoutSessionCreate(success_url=url)
    with pytest.raises(ValidationError):
        CheckoutSessionCreate(cancel_url=url)
