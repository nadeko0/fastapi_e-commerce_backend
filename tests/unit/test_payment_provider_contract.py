"""
The payment provider contract: what app/api/v1/orders.py relies on from a
PaymentProvider, asserted once and run against both implementations - the
mock (StripePaymentProvider, which every other payment test uses) and the
live adapter (LiveStripePaymentProvider) on top of fake_stripe_api.py, an
in-process stand-in for api.stripe.com written from Stripe's documentation.
A behaviour the mock grows that Stripe does not have, or an adapter
translation that breaks, fails here for one of the two.

Relied on, and covered below:
- PaymentIntent: created awaiting confirmation, with id, client secret,
  amount, upper-case currency and metadata; idempotent per key and
  parameters (same request: same intent; other parameters: IdempotencyError;
  a request refused at validation does not use up its key); confirming the
  create call's key is an IdempotencyError; amounts must be positive ints.
- Confirmation by test card: succeeds, is declined (CardError with Stripe's
  code and decline code, the intent left requires_payment_method with
  last_payment_error), or requires 3D Secure (requires_action with a
  redirect next_action back to the return_url), after which authentication
  completes or declines it.
- Refunds: full and partial, never above what was captured, partials
  summing to it (property-tested in six store currencies), idempotent per
  key; asynchronous ones (pending then succeeded, succeeded then failed)
  with the event Stripe sends.
- Checkout Sessions: created open and unpaid, idempotent, retrieved,
  expired once.
- Unknown ids are CardError(code="resource_missing"); an unreachable
  Stripe is PaymentProviderTimeoutError, for every call.

Where the two differ by design, the last section says how and why, and
asserts each side.
"""
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from fake_stripe_api import FakeStripeAPI
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from app.services.payment.base import PaymentProvider
from app.services.payment.exceptions import (
    CardError,
    IdempotencyError,
    InvalidRequestError,
    PaymentProviderTimeoutError,
)
from app.services.payment.live_stripe_provider import LiveStripePaymentProvider
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.payment.types import (
    CheckoutSessionStatus,
    PaymentIntentStatus,
    RefundStatus,
)

API_KEY = "stripe-secret-key-for-tests-not-a-real-key"
RETURN_URL = "http://localhost:3000/checkout/return?order_id=7"
SUCCESS_URL = "http://localhost:3000/checkout/success?session_id={CHECKOUT_SESSION_ID}"
CANCEL_URL = "http://localhost:3000/checkout/cancel"
STORE_CURRENCIES = ["USD", "EUR", "GBP", "PLN", "HUF", "TWD"]


@dataclass
class Stripe:
    """One provider under test, and what the test needs to act out on the
    Stripe side of it."""

    name: str
    provider: PaymentProvider
    # The customer finishing 3D Secure on the page next_action sent them to.
    authenticate: Callable[[str], None]
    # Stripe finishing an asynchronous refund; returns the event it sends.
    settle_refund: Callable[[str], dict[str, Any]]
    # api.stripe.com becoming unreachable.
    go_offline: Callable[[], None]


@pytest.fixture(params=["mock", "live"])
def stripe(request, monkeypatch):
    if request.param == "mock":
        StripePaymentProvider.reset_mock_state()
        provider = StripePaymentProvider()

        def authenticate(intent_id):
            # The mock's stand-in for authentication is a second confirm,
            # which raises for a card declined after authenticating; Stripe
            # reports that by webhook instead, so it is swallowed here.
            try:
                provider.confirm_payment_intent(intent_id, return_url=RETURN_URL)
            except CardError:
                pass

        def go_offline():
            def unreachable(self):
                raise PaymentProviderTimeoutError()
            monkeypatch.setattr(StripePaymentProvider, "_simulate_network_call", unreachable)

        yield Stripe("mock", provider, authenticate, StripePaymentProvider.settle_refund,
                     go_offline)
        StripePaymentProvider.reset_mock_state()
    else:
        fake = FakeStripeAPI(API_KEY)
        fake.install(monkeypatch)
        yield Stripe("live", LiveStripePaymentProvider(api_key=API_KEY), fake.authenticate,
                     fake.settle_refund, lambda: setattr(fake, "offline", True))


def _key():
    return f"contract-{uuid.uuid4().hex}"


def _intent(stripe, token="pm_card_visa", amount=5000, currency="EUR", key=None, **kwargs):
    return stripe.provider.create_payment_intent(
        amount=amount, currency=currency, idempotency_key=key or _key(),
        payment_method=token, **kwargs,
    )


def _paid(stripe, token="pm_card_visa", amount=5000, currency="EUR"):
    intent = _intent(stripe, token, amount, currency)
    confirmed = stripe.provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)
    assert confirmed.status == PaymentIntentStatus.SUCCEEDED
    return confirmed


# -- PaymentIntent: create -------------------------------------------------------

def test_a_new_intent_awaits_confirmation(stripe):
    intent = _intent(stripe, amount=1999, currency="EUR", metadata={"order_id": "7"})

    assert intent.id and intent.client_secret
    assert (intent.amount, intent.currency) == (1999, "EUR")
    assert intent.status == PaymentIntentStatus.REQUIRES_CONFIRMATION
    assert (intent.payment_method, intent.metadata) == ("pm_card_visa", {"order_id": "7"})
    assert (intent.next_action, intent.last_payment_error) == (None, None)
    assert stripe.provider.retrieve_payment_intent(intent.id).id == intent.id


def test_the_same_request_with_the_same_key_is_the_same_intent(stripe):
    key = _key()
    first = _intent(stripe, key=key)

    assert _intent(stripe, key=key).id == first.id
    assert _intent(stripe).id != first.id


@pytest.mark.parametrize("change", [{"amount": 5001}, {"currency": "GBP"}])
def test_the_same_key_with_other_parameters_is_refused(stripe, change):
    key = _key()
    _intent(stripe, key=key)
    with pytest.raises(IdempotencyError):
        _intent(stripe, key=key, **{"amount": 5000, "currency": "EUR", **change})


@pytest.mark.parametrize("amount", [0, -1, 12.5, Decimal("12.50"), "1250", True])
def test_an_amount_that_is_not_a_positive_int_is_refused_and_keeps_the_key(stripe, amount):
    key = _key()
    with pytest.raises(InvalidRequestError):
        _intent(stripe, amount=amount, key=key)
    # Refused at validation, so the key was not used: the corrected
    # request goes through with it.
    assert _intent(stripe, amount=5000, key=key).amount == 5000


def test_a_currency_that_does_not_exist_is_refused(stripe):
    with pytest.raises(InvalidRequestError):
        _intent(stripe, currency="XYZ")


@pytest.mark.parametrize("currency", STORE_CURRENCIES)
def test_every_store_currency_is_charged_and_reported_in_upper_case(stripe, currency):
    assert _paid(stripe, amount=50000, currency=currency).currency == currency


# -- PaymentIntent: confirm --------------------------------------------------------

@pytest.mark.parametrize("token", [
    "pm_card_visa", "pm_card_mastercard", "pm_card_amex", "pm_card_jcb", "pm_card_unionpay",
    "pm_card_de", "pm_card_gb", "pm_card_jp", "pm_card_threeDSecureOptional",
    "pm_card_amex_threeDSecureNotSupported",
])
def test_a_card_that_pays_succeeds_on_the_first_confirmation(stripe, token):
    intent = _paid(stripe, token)
    assert intent.next_action is None
    assert stripe.provider.retrieve_payment_intent(intent.id).status == (
        PaymentIntentStatus.SUCCEEDED
    )


@pytest.mark.parametrize("token,code,decline_code", [
    ("pm_card_visa_chargeDeclined", "card_declined", "generic_decline"),
    ("pm_card_visa_chargeDeclinedInsufficientFunds", "card_declined", "insufficient_funds"),
    ("pm_card_visa_chargeDeclinedLostCard", "card_declined", "lost_card"),
    ("pm_card_visa_chargeDeclinedStolenCard", "card_declined", "stolen_card"),
    ("pm_card_chargeDeclinedExpiredCard", "expired_card", None),
    ("pm_card_chargeDeclinedIncorrectCvc", "incorrect_cvc", None),
    ("pm_card_chargeDeclinedProcessingError", "processing_error", None),
    ("pm_card_visa_chargeDeclinedVelocityLimitExceeded", "card_declined",
     "card_velocity_exceeded"),
    ("pm_card_radarBlock", "card_declined", "fraudulent"),
    ("pm_card_threeDSecureRequiredProcessingError", "card_declined", "processing_error"),
])
def test_a_declined_card_is_a_card_error_and_the_intent_waits(stripe, token, code,
                                                              decline_code):
    intent = _intent(stripe, token)

    with pytest.raises(CardError) as declined:
        stripe.provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)

    assert (declined.value.code, declined.value.decline_code) == (code, decline_code)
    after = stripe.provider.retrieve_payment_intent(intent.id)
    assert after.status == PaymentIntentStatus.REQUIRES_PAYMENT_METHOD
    assert after.last_payment_error["code"] == code


def test_a_declined_confirmation_retried_with_its_key_is_declined_again(stripe):
    intent = _intent(stripe, "pm_card_visa_chargeDeclined")
    for _ in range(2):
        with pytest.raises(CardError):
            stripe.provider.confirm_payment_intent(intent.id, idempotency_key="confirm-1",
                                                   return_url=RETURN_URL)


@pytest.mark.parametrize("token,after", [
    ("pm_card_authenticationRequired", PaymentIntentStatus.SUCCEEDED),
    ("pm_card_threeDSecure2Required", PaymentIntentStatus.SUCCEEDED),
    ("pm_card_threeDSecureRequiredChargeDeclined", PaymentIntentStatus.REQUIRES_PAYMENT_METHOD),
])
def test_3d_secure_redirects_and_then_completes_or_declines(stripe, token, after):
    intent = _intent(stripe, token)

    pending = stripe.provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)

    assert pending.status == PaymentIntentStatus.REQUIRES_ACTION
    assert pending.next_action["type"] == "redirect_to_url"
    assert pending.next_action["redirect_to_url"]["return_url"] == RETURN_URL
    assert pending.next_action["redirect_to_url"]["url"].startswith("https://")

    stripe.authenticate(intent.id)

    done = stripe.provider.retrieve_payment_intent(intent.id)
    assert (done.status, done.next_action) == (after, None)


def test_confirming_with_the_create_calls_key_is_refused(stripe):
    # Stripe ties a key to its first request, endpoint included.
    key = _key()
    intent = _intent(stripe, key=key)
    with pytest.raises(IdempotencyError):
        stripe.provider.confirm_payment_intent(intent.id, idempotency_key=key,
                                               return_url=RETURN_URL)


@pytest.mark.parametrize("call", ["confirm", "retrieve"])
def test_an_unknown_intent_is_resource_missing(stripe, call):
    with pytest.raises(CardError) as missing:
        if call == "confirm":
            stripe.provider.confirm_payment_intent("pi_unknown", return_url=RETURN_URL)
        else:
            stripe.provider.retrieve_payment_intent("pi_unknown")
    assert missing.value.code == "resource_missing"


# -- refunds -------------------------------------------------------------------------

def test_a_full_refund_returns_everything_captured(stripe):
    intent = _paid(stripe, amount=4321, currency="GBP")

    refund = stripe.provider.create_refund(payment_intent_id=intent.id)

    assert refund.id and refund.payment_intent == intent.id
    assert (refund.amount, refund.currency, refund.status) == (4321, "GBP",
                                                               RefundStatus.SUCCEEDED)
    with pytest.raises(CardError):
        stripe.provider.create_refund(payment_intent_id=intent.id)


@pytest.mark.parametrize("currency", STORE_CURRENCIES)
@given(data=st.data())
@hyp_settings(max_examples=15, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_refunds_never_exceed_the_capture_and_partials_add_up(stripe, currency, data):
    captured = data.draw(st.integers(min_value=20_000, max_value=10_000_000), "captured")
    intent = _paid(stripe, amount=captured, currency=currency)
    remaining, refunded = captured, []

    for requested in data.draw(st.lists(st.integers(min_value=1, max_value=captured + 1),
                                        max_size=6), "partials"):
        if requested > remaining:
            with pytest.raises(CardError):
                stripe.provider.create_refund(payment_intent_id=intent.id, amount=requested)
            continue
        refund = stripe.provider.create_refund(payment_intent_id=intent.id, amount=requested)
        assert (refund.amount, refund.currency) == (requested, currency)
        remaining -= requested
        refunded.append(requested)

    if remaining:
        assert stripe.provider.create_refund(payment_intent_id=intent.id).amount == remaining
        refunded.append(remaining)
    assert sum(refunded) == captured
    with pytest.raises(CardError):
        stripe.provider.create_refund(payment_intent_id=intent.id, amount=1)


def test_a_refund_retried_with_its_key_is_the_same_refund(stripe):
    intent = _paid(stripe, amount=5000)

    first = stripe.provider.create_refund(payment_intent_id=intent.id, amount=1000,
                                          idempotency_key="refund-1")
    again = stripe.provider.create_refund(payment_intent_id=intent.id, amount=1000,
                                          idempotency_key="refund-1")

    assert again.id == first.id
    # Only one refund was made: 4000 is still refundable.
    assert stripe.provider.create_refund(payment_intent_id=intent.id).amount == 4000


def test_a_refund_key_reused_for_another_amount_is_refused(stripe):
    intent = _paid(stripe, amount=5000)
    stripe.provider.create_refund(payment_intent_id=intent.id, amount=1000,
                                  idempotency_key="refund-1")
    with pytest.raises(IdempotencyError):
        stripe.provider.create_refund(payment_intent_id=intent.id, amount=2000,
                                      idempotency_key="refund-1")


def test_only_a_succeeded_intent_can_be_refunded(stripe):
    intent = _intent(stripe)
    with pytest.raises(CardError):
        stripe.provider.create_refund(payment_intent_id=intent.id)


def test_refunding_an_unknown_intent_is_resource_missing(stripe):
    with pytest.raises(CardError) as missing:
        stripe.provider.create_refund(payment_intent_id="pi_unknown")
    assert missing.value.code == "resource_missing"


def test_a_refund_amount_that_is_not_an_int_is_refused(stripe):
    # The mock raises InvalidRequestError; the live adapter turns every
    # InvalidRequestError of Refund.create but resource_missing into a
    # CardError. /refund answers 400 for both, so either is the contract.
    intent = _paid(stripe)
    with pytest.raises((InvalidRequestError, CardError)):
        stripe.provider.create_refund(payment_intent_id=intent.id, amount=10.5)


def test_a_pending_refund_succeeds_later_with_refund_updated(stripe):
    intent = _paid(stripe, "pm_card_pendingRefund", amount=5000, currency="PLN")

    refund = stripe.provider.create_refund(payment_intent_id=intent.id, amount=3000)
    assert refund.status == RefundStatus.PENDING

    event = stripe.settle_refund(refund.id)

    assert (event["object"], event["type"]) == ("event", "refund.updated")
    sent = event["data"]["object"]
    assert (sent["id"], sent["object"], sent["status"]) == (refund.id, "refund", "succeeded")
    assert (sent["amount"], sent["currency"], sent["payment_intent"]) == (3000, "pln",
                                                                          intent.id)


def test_a_refund_that_fails_later_sends_refund_failed_and_can_be_redone(stripe):
    intent = _paid(stripe, "pm_card_refundFail", amount=5000, currency="PLN")

    refund = stripe.provider.create_refund(payment_intent_id=intent.id)
    assert (refund.status, refund.amount) == (RefundStatus.SUCCEEDED, 5000)

    event = stripe.settle_refund(refund.id)

    assert event["type"] == "refund.failed"
    assert event["data"]["object"]["status"] == "failed"
    assert event["data"]["object"]["failure_reason"]
    assert stripe.provider.create_refund(payment_intent_id=intent.id).amount == 5000


# -- Checkout Sessions ------------------------------------------------------------------

def _session(stripe, amount=5000, currency="EUR", key=None, **urls):
    return stripe.provider.create_checkout_session(
        amount=amount, currency=currency, idempotency_key=key or _key(),
        success_url=urls.get("success_url", SUCCESS_URL),
        cancel_url=urls.get("cancel_url", CANCEL_URL),
        description="Order #7", metadata={"order_id": "7"},
    )


def test_a_new_session_is_open_and_unpaid(stripe):
    session = _session(stripe, amount=2500, currency="EUR")

    assert session.id and session.url.startswith("https://")
    assert (session.status, session.payment_status) == (CheckoutSessionStatus.OPEN, "unpaid")
    assert (session.amount_total, session.currency) == (2500, "EUR")
    assert (session.payment_intent, session.metadata) == (None, {"order_id": "7"})
    assert stripe.provider.retrieve_checkout_session(session.id).id == session.id


def test_a_session_is_idempotent_per_key_and_parameters(stripe):
    key = _key()
    first = _session(stripe, key=key)
    assert _session(stripe, key=key).id == first.id
    with pytest.raises(IdempotencyError):
        _session(stripe, amount=5001, key=key)


@pytest.mark.parametrize("missing", ["success_url", "cancel_url"])
def test_a_session_needs_both_redirect_urls(stripe, missing):
    with pytest.raises(InvalidRequestError):
        _session(stripe, **{missing: ""})


def test_an_open_session_expires_once(stripe):
    session = _session(stripe)

    assert stripe.provider.expire_checkout_session(session.id).status == (
        CheckoutSessionStatus.EXPIRED
    )
    with pytest.raises(CardError):
        stripe.provider.expire_checkout_session(session.id)


@pytest.mark.parametrize("call", ["retrieve", "expire"])
def test_an_unknown_session_is_resource_missing(stripe, call):
    with pytest.raises(CardError) as missing:
        getattr(stripe.provider, f"{call}_checkout_session")("cs_test_unknown")
    assert missing.value.code == "resource_missing"


# -- Stripe unreachable ---------------------------------------------------------------

def test_every_call_reports_an_unreachable_stripe_as_a_timeout(stripe):
    intent = _paid(stripe)
    session = _session(stripe)
    stripe.go_offline()
    p = stripe.provider
    calls = {
        "create": lambda: _intent(stripe),
        "confirm": lambda: p.confirm_payment_intent(intent.id, return_url=RETURN_URL),
        "retrieve": lambda: p.retrieve_payment_intent(intent.id),
        "refund": lambda: p.create_refund(payment_intent_id=intent.id),
        "create session": lambda: _session(stripe),
        "retrieve session": lambda: p.retrieve_checkout_session(session.id),
        "expire session": lambda: p.expire_checkout_session(session.id),
    }
    for name, call in calls.items():
        with pytest.raises(PaymentProviderTimeoutError):
            call()


# -- where the mock differs from Stripe, by design ----------------------------------------
#
# None of these is a path app/api/v1/orders.py takes: /pay confirms each
# intent once, with its own key, and leaves 3D Secure to the webhook.

def test_confirming_a_succeeded_intent_again(stripe):
    # Stripe refuses (the intent's state does not allow it); the mock
    # returns the intent unchanged, its stand-in for a retried confirm.
    intent = _paid(stripe)
    if stripe.name == "mock":
        again = stripe.provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)
        assert again.status == PaymentIntentStatus.SUCCEEDED
    else:
        with pytest.raises(InvalidRequestError) as refused:
            stripe.provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)
        assert refused.value.code == "payment_intent_unexpected_state"


def test_confirming_again_while_3d_secure_is_pending(stripe):
    # The mock has no customer: a second confirm is its stand-in for the
    # customer authenticating. On Stripe the intent keeps waiting.
    intent = _intent(stripe, "pm_card_threeDSecure2Required")
    stripe.provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)

    again = stripe.provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)

    expected = {"mock": PaymentIntentStatus.SUCCEEDED,
                "live": PaymentIntentStatus.REQUIRES_ACTION}
    assert again.status == expected[stripe.name]


def test_an_intent_created_without_a_payment_method(stripe):
    # PaymentCreate.payment_method_token is optional: the mock then charges
    # its default card, pm_card_visa. Stripe cannot confirm without one.
    intent = stripe.provider.create_payment_intent(amount=5000, currency="EUR",
                                                   idempotency_key=_key())
    if stripe.name == "mock":
        assert stripe.provider.confirm_payment_intent(
            intent.id, return_url=RETURN_URL).status == PaymentIntentStatus.SUCCEEDED
    else:
        assert intent.status == PaymentIntentStatus.REQUIRES_PAYMENT_METHOD
        with pytest.raises(InvalidRequestError):
            stripe.provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)


def test_an_amount_below_stripes_minimum_charge(stripe):
    # Stripe refuses charges below a per-currency minimum (0.50 USD,
    # docs.stripe.com/currencies), which depends on the account's
    # settlement currency; the mock accepts any positive amount. /pay
    # answers 400 for Stripe's refusal (InvalidRequestError).
    if stripe.name == "mock":
        assert _intent(stripe, amount=1, currency="USD").amount == 1
    else:
        with pytest.raises(InvalidRequestError) as refused:
            _intent(stripe, amount=1, currency="USD")
        assert refused.value.code == "amount_too_small"


def test_an_idempotent_replay_after_the_intent_moved_on(stripe):
    # Stripe replays the response it saved for the key (the intent as it
    # was created); the mock returns its one intent object as it is now.
    # Either way it is the same intent - what /pay relies on.
    key = _key()
    intent = _intent(stripe, key=key)
    stripe.provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)

    replay = _intent(stripe, key=key)

    assert replay.id == intent.id
    expected = {"mock": PaymentIntentStatus.SUCCEEDED,
                "live": PaymentIntentStatus.REQUIRES_CONFIRMATION}
    assert replay.status == expected[stripe.name]
