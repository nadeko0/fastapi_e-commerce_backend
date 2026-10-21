"""
The mock provider simulating Stripe's test PaymentMethods
(https://docs.stripe.com/testing), keyed by the same pm_ ids so a test
reads like Stripe's guide: each success, decline and 3D Secure card, and
the asynchronous refund cards with the events Stripe sends for them.
"""
import pytest

from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.payment.types import PaymentIntentStatus

RETURN_URL = "http://localhost:3000/checkout/return?order_id=1"


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
