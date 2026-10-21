"""
The mock provider simulating Stripe's test PaymentMethods
(https://docs.stripe.com/testing), keyed by the same pm_ ids so a test
reads like Stripe's guide: each success, decline and 3D Secure card, and
the asynchronous refund cards with the events Stripe sends for them.
"""
import json

import pytest

from app.services.payment.exceptions import CardError
from app.services.payment.stripe_provider import (
    AUTHENTICATING_TOKENS,
    DECLINING_TOKENS,
    SUCCEEDING_TOKENS,
    TOKEN_PENDING_REFUND,
    TOKEN_REFUND_FAIL,
    TOKEN_SUCCESS_DEFAULT,
    StripePaymentProvider,
)
from app.services.payment.types import PaymentIntentStatus, RefundStatus

RETURN_URL = "http://localhost:3000/checkout/return?order_id=1"
WEBHOOK_SECRET = "webhook-signing-secret-for-tests-not-a-real-one"


@pytest.fixture(autouse=True)
def _fresh_mock():
    StripePaymentProvider.reset_mock_state()
    yield
    StripePaymentProvider.reset_mock_state()


def _confirm(token, amount=5000, currency="USD"):
    provider = StripePaymentProvider()
    intent = provider.create_payment_intent(amount=amount, currency=currency,
                                            idempotency_key=f"key-{token}",
                                            payment_method=token)
    return provider, provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)


# With a return_url, Stripe drives 3D Secure as a redirect: next_action is
# {"type": "redirect_to_url", "redirect_to_url": {"url": ..., "return_url":
# ...}} - what the opt-in live suite saw against Stripe's test mode
# (tests/integration/test_payment_live_stripe.py) and what /pay hands the
# client. The mock answered {"type": "use_stripe_sdk", ...} and dropped the
# return_url, so a client built against the mock followed the wrong branch.
def test_3d_secure_asks_for_a_redirect_back_to_the_return_url():
    _, intent = _confirm("pm_card_authenticationRequired")

    assert intent.status == PaymentIntentStatus.REQUIRES_ACTION
    assert intent.next_action["type"] == "redirect_to_url"
    assert intent.next_action["redirect_to_url"]["return_url"] == RETURN_URL
    assert intent.next_action["redirect_to_url"]["url"].startswith("https://")


# Stripe's generic decline card is pm_card_visa_chargeDeclined (the opt-in
# live suite declines with it against Stripe's test mode). The mock only
# knew "pm_card_chargeDeclined", which is not a Stripe test card, and
# charged pm_card_visa_chargeDeclined successfully: a client tested with
# Stripe's own decline card saw its order paid.
def test_stripes_generic_decline_card_is_declined():
    provider = StripePaymentProvider()
    intent = provider.create_payment_intent(amount=5000, currency="USD", idempotency_key="k",
                                            payment_method="pm_card_visa_chargeDeclined")

    with pytest.raises(CardError) as declined:
        provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)

    assert (declined.value.code, declined.value.decline_code) == ("card_declined",
                                                                  "generic_decline")
    assert provider.retrieve_payment_intent(intent.id).status == (
        PaymentIntentStatus.REQUIRES_PAYMENT_METHOD
    )


# -- the guide's tables, transcribed here independently of the mock's ---------------
#
# docs.stripe.com/testing, read in October 2026. If the mock's tables drift
# from these, the tests below say which card.

GUIDE_DECLINES = {
    "pm_card_visa_chargeDeclined": ("card_declined", "generic_decline"),
    "pm_card_visa_chargeDeclinedInsufficientFunds": ("card_declined", "insufficient_funds"),
    "pm_card_visa_chargeDeclinedLostCard": ("card_declined", "lost_card"),
    "pm_card_visa_chargeDeclinedStolenCard": ("card_declined", "stolen_card"),
    "pm_card_chargeDeclinedExpiredCard": ("expired_card", None),
    "pm_card_chargeDeclinedIncorrectCvc": ("incorrect_cvc", None),
    "pm_card_chargeDeclinedProcessingError": ("processing_error", None),
    "pm_card_visa_chargeDeclinedVelocityLimitExceeded": ("card_declined",
                                                         "card_velocity_exceeded"),
}
GUIDE_SUCCEEDING = [
    "pm_card_visa", "pm_card_mastercard", "pm_card_amex", "pm_card_discover", "pm_card_diners",
    "pm_card_jcb", "pm_card_unionpay", "pm_card_visa_debit", "pm_card_mastercard_prepaid",
    "pm_card_us", "pm_card_de", "pm_card_gb", "pm_card_fr", "pm_card_pl", "pm_card_hu",
    "pm_card_jp", "pm_card_tw", "pm_card_threeDSecureOptional",
    "pm_card_amex_threeDSecureNotSupported", "pm_card_pendingRefund", "pm_card_refundFail",
]


@pytest.mark.parametrize("token", GUIDE_SUCCEEDING)
def test_a_card_the_guide_charges_succeeds_at_once(token):
    _, intent = _confirm(token)
    assert (intent.status, intent.next_action) == (PaymentIntentStatus.SUCCEEDED, None)


def test_the_mocks_success_list_holds_every_card_the_guide_charges():
    assert set(GUIDE_SUCCEEDING) <= SUCCEEDING_TOKENS
    assert not SUCCEEDING_TOKENS & (set(DECLINING_TOKENS) | set(AUTHENTICATING_TOKENS))


def test_a_payment_method_the_mock_does_not_know_succeeds_as_before():
    # E.g. a PaymentMethod Stripe.js created in test mode: the mock's default.
    _, intent = _confirm("pm_1QAbCdEfGhIjKlMn")
    assert intent.status == PaymentIntentStatus.SUCCEEDED


@pytest.mark.parametrize("token", list(GUIDE_DECLINES))
def test_a_card_the_guide_declines_is_declined_with_its_codes(token):
    provider = StripePaymentProvider()
    intent = provider.create_payment_intent(amount=5000, currency="EUR",
                                            idempotency_key="k", payment_method=token)

    with pytest.raises(CardError) as declined:
        provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)

    code, decline_code = GUIDE_DECLINES[token]
    assert (declined.value.code, declined.value.decline_code) == (code, decline_code)
    after = provider.retrieve_payment_intent(intent.id)
    assert after.status == PaymentIntentStatus.REQUIRES_PAYMENT_METHOD
    assert after.last_payment_error["code"] == code
    assert after.last_payment_error["decline_code"] == decline_code
    # A retry with the same card is declined again; nothing was charged.
    with pytest.raises(CardError):
        provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)


def test_radar_blocks_its_always_blocked_card():
    # "Blocked payments return card errors" (guide, Fraud prevention).
    with pytest.raises(CardError) as blocked:
        _confirm("pm_card_radarBlock")
    assert (blocked.value.code, blocked.value.decline_code) == ("card_declined", "fraudulent")


def test_the_mocks_old_decline_token_still_declines():
    with pytest.raises(CardError) as declined:
        _confirm("pm_card_chargeDeclined")
    assert declined.value.decline_code == "generic_decline"


# -- 3D Secure --------------------------------------------------------------------

@pytest.mark.parametrize("token", ["pm_card_authenticationRequired",
                                   "pm_card_threeDSecure2Required"])
def test_a_card_that_requires_authentication_succeeds_once_authenticated(token):
    provider, intent = _confirm(token)
    assert intent.status == PaymentIntentStatus.REQUIRES_ACTION
    assert intent.next_action["redirect_to_url"]["return_url"] == RETURN_URL

    # The mock's stand-in for the customer finishing 3D Secure.
    done = provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)

    assert (done.status, done.next_action) == (PaymentIntentStatus.SUCCEEDED, None)


def test_a_card_declined_after_authentication():
    provider, intent = _confirm("pm_card_threeDSecureRequiredChargeDeclined")
    assert intent.status == PaymentIntentStatus.REQUIRES_ACTION

    with pytest.raises(CardError) as declined:
        provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)

    assert declined.value.code == "card_declined"
    after = provider.retrieve_payment_intent(intent.id)
    assert (after.status, after.next_action) == (PaymentIntentStatus.REQUIRES_PAYMENT_METHOD,
                                                 None)


def test_a_card_whose_3d_secure_lookup_fails_is_declined_at_once():
    with pytest.raises(CardError) as declined:
        _confirm("pm_card_threeDSecureRequiredProcessingError")
    assert declined.value.code == "card_declined"


# -- asynchronous refunds ------------------------------------------------------------

def _paid(token, amount=5000, currency="PLN"):
    provider, intent = _confirm(token, amount=amount, currency=currency)
    assert intent.status == PaymentIntentStatus.SUCCEEDED
    return provider, intent


def _post(event):
    """The event as Stripe would deliver it, through the mock's own check."""
    body = json.dumps(event).encode()
    header = StripePaymentProvider.sign_payload(body, WEBHOOK_SECRET)
    return StripePaymentProvider().construct_webhook_event(body, header, WEBHOOK_SECRET)


def test_a_pending_refund_succeeds_later_and_stripe_says_so():
    provider, intent = _paid(TOKEN_PENDING_REFUND)

    refund = provider.create_refund(payment_intent_id=intent.id, amount=2000,
                                    idempotency_key="refund-1")
    assert refund.status == RefundStatus.PENDING

    event = StripePaymentProvider.settle_refund(refund.id)

    assert refund.status == RefundStatus.SUCCEEDED
    assert event["type"] == "refund.updated"
    assert event["data"]["object"] == {
        "id": refund.id, "object": "refund", "amount": 2000, "currency": "pln",
        "payment_intent": intent.id, "status": "succeeded", "failure_reason": None,
        "created": event["data"]["object"]["created"],
    }
    delivered = _post(event)
    assert (delivered.id, delivered.type, delivered.data) == (event["id"], "refund.updated",
                                                              event["data"])
    # The idempotent replay of the refund request sees the settled refund.
    assert provider.create_refund(payment_intent_id=intent.id, amount=2000,
                                  idempotency_key="refund-1").status == RefundStatus.SUCCEEDED


def test_a_pending_refund_already_counts_against_what_is_refundable():
    provider, intent = _paid(TOKEN_PENDING_REFUND, amount=5000)
    provider.create_refund(payment_intent_id=intent.id, amount=3000)

    with pytest.raises(CardError):
        provider.create_refund(payment_intent_id=intent.id, amount=2001)
    assert provider.create_refund(payment_intent_id=intent.id).amount == 2000


def test_a_refund_that_fails_later_gives_the_amount_back_and_stripe_says_so():
    provider, intent = _paid(TOKEN_REFUND_FAIL, amount=5000)

    refund = provider.create_refund(payment_intent_id=intent.id)
    assert (refund.status, refund.amount) == (RefundStatus.SUCCEEDED, 5000)
    with pytest.raises(CardError):  # fully refunded, for now
        provider.create_refund(payment_intent_id=intent.id, amount=1)

    event = StripePaymentProvider.settle_refund(refund.id)

    assert refund.status == RefundStatus.FAILED
    assert event["type"] == "refund.failed"
    assert event["data"]["object"]["status"] == "failed"
    assert event["data"]["object"]["failure_reason"] == "unknown"
    assert _post(event).type == "refund.failed"
    # The money never went back, so it can be refunded again.
    assert provider.create_refund(payment_intent_id=intent.id).amount == 5000


@pytest.mark.parametrize("token", [TOKEN_SUCCESS_DEFAULT, TOKEN_PENDING_REFUND])
def test_a_refund_with_nothing_left_to_happen_cannot_be_settled(token):
    provider, intent = _paid(token)
    refund = provider.create_refund(payment_intent_id=intent.id)
    if token == TOKEN_PENDING_REFUND:
        StripePaymentProvider.settle_refund(refund.id)

    with pytest.raises(ValueError):
        StripePaymentProvider.settle_refund(refund.id)


def test_settling_an_unknown_refund_is_resource_missing():
    with pytest.raises(CardError) as missing:
        StripePaymentProvider.settle_refund("re_unknown")
    assert missing.value.code == "resource_missing"


def test_reset_forgets_refunds():
    provider, intent = _paid(TOKEN_PENDING_REFUND)
    refund = provider.create_refund(payment_intent_id=intent.id)
    StripePaymentProvider.reset_mock_state()
    with pytest.raises(CardError):
        StripePaymentProvider.settle_refund(refund.id)
