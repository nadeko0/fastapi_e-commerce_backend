"""
The customer journey against Stripe's REAL test mode: the app itself
(HTTP API, database, order rules) with LiveStripePaymentProvider instead
of the mock, driven with Stripe's documented test PaymentMethods
(https://docs.stripe.com/testing - pm_ ids, which Stripe recommends for
server-side tests, never raw card numbers).

Skipped, like test_payment_live_stripe.py, unless a real test-mode
STRIPE_SECRET_KEY is in the environment or in .env.test. Every case checks
what the app answers and stores (the attempt's status, the order's status
and payment_status, the message the customer is shown) as well as what
Stripe holds. Never a 500: a decline or a provider error must reach the
customer as a sensible message.

Webhooks: no endpoint is registered with Stripe for a test run, so where
an outcome arrives only by webhook (a refund that completes or fails
later), the test fetches the real object from Stripe and delivers it to
the app's webhook as Stripe would, signed with the app's webhook secret.
"""
import json
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from payment_support import (
    Outbox,
    Shop,
    order_state,
    payment_rows,
    use_production_sessions,
)
from stripe_event_support import deliver

from app.core.config import settings
from app.main import app
from app.services.payment import get_payment_provider
from app.services.payment.stripe_provider import StripePaymentProvider

_dotenv_test = Path(__file__).resolve().parents[2] / ".env.test"
if _dotenv_test.exists():
    from dotenv import load_dotenv

    load_dotenv(_dotenv_test, override=False)

_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
_HAS_REAL_KEY = _SECRET_KEY.startswith("sk_test_") and _SECRET_KEY != "sk_test_mock_placeholder"

pytestmark = pytest.mark.skipif(
    not _HAS_REAL_KEY,
    reason=(
        "No real Stripe test-mode STRIPE_SECRET_KEY in the environment "
        "(set STRIPE_SECRET_KEY or provide .env.test) - skipping the live "
        "Stripe journey."
    ),
)

SUCCEEDING = ["pm_card_visa", "pm_card_mastercard", "pm_card_amex", "pm_card_jcb",
              "pm_card_unionpay", "pm_card_de", "pm_card_gb", "pm_card_jp", "pm_card_br"]
DECLINED = ["pm_card_visa_chargeDeclined", "pm_card_visa_chargeDeclinedInsufficientFunds",
            "pm_card_visa_chargeDeclinedLostCard", "pm_card_visa_chargeDeclinedStolenCard",
            "pm_card_chargeDeclinedExpiredCard", "pm_card_chargeDeclinedIncorrectCvc",
            "pm_card_chargeDeclinedProcessingError",
            "pm_card_visa_chargeDeclinedVelocityLimitExceeded", "pm_card_radarBlock"]
THREE_DS_REQUIRED = ["pm_card_threeDSecure2Required",
                     "pm_card_threeDSecureRequiredChargeDeclined",
                     "pm_card_threeDSecureRequiredProcessingError",
                     # Indian cards are authenticated (RBI rules): Stripe's test
                     # mode answers requires_action for the India test card.
                     "pm_card_in"]
# Refunds made with pm_card_pendingRefund / pm_card_refundFail reach their final
# status minutes later in test mode, too late for an ordinary run. What Stripe
# reports at once is always checked; waiting for the final status is opt-in.
_SLOW = os.environ.get("STRIPE_LIVE_SLOW") == "1"
_SLOW_TIMEOUT = 15 * 60


def _final_status_or_skip():
    if not _SLOW:
        pytest.skip("Stripe settles this refund minutes later; set STRIPE_LIVE_SLOW=1 to wait for it")


@pytest.fixture
def stripe_sdk():
    import stripe

    return stripe


@pytest.fixture
def shop(client, db_session, monkeypatch):
    from app.services.payment.live_stripe_provider import LiveStripePaymentProvider

    use_production_sessions(monkeypatch)
    monkeypatch.setitem(app.dependency_overrides, get_payment_provider,
                        lambda: LiveStripePaymentProvider(api_key=_SECRET_KEY))
    return Shop(client, db_session)


@pytest.fixture
def outbox(monkeypatch):
    return Outbox(monkeypatch)


def _order(shop, price="25.00", quantity=1):
    product = shop.product(price=price, stock=10)
    buyer = shop.buyer()
    return product, buyer, shop.order_of(buyer, product, quantity)


def _deliver(shop, event_type, obj):
    """Deliver a real Stripe object to the app's webhook, as Stripe would."""
    body = json.dumps({"id": f"evt_{uuid.uuid4().hex[:24]}", "object": "event",
                       "type": event_type, "created": int(time.time()),
                       "data": {"object": obj}}).encode()
    signature = StripePaymentProvider.sign_payload(body, settings.STRIPE_WEBHOOK_SECRET.get_secret_value())
    return deliver(shop.client.post("/api/v1/orders/webhooks/stripe", content=body,
                                    headers={"Stripe-Signature": signature,
                                             "Content-Type": "application/json"}), body)


def _wait_for_refund(stripe, refund_id, statuses, timeout=90):
    deadline = time.monotonic() + timeout
    while True:
        refund = stripe.Refund.retrieve(refund_id, api_key=_SECRET_KEY)
        if refund.status in statuses or time.monotonic() > deadline:
            return refund
        time.sleep(3)


def _stripe_refunded(stripe, payment_intent_id):
    refunds = stripe.Refund.list(payment_intent=payment_intent_id, api_key=_SECRET_KEY)
    return sorted((r.amount, r.status) for r in refunds.data)


# -- The journey ---------------------------------------------------------------------

def test_cart_to_partial_and_remaining_refund(shop, outbox, stripe_sdk):
    product, buyer, order = _order(shop, price="12.50", quantity=2)
    admin = shop.admin()
    assert (Decimal(order["total_amount"]), order["currency"]) == (Decimal("25.00"), "USD")

    paid = shop.pay(buyer, order["id"], token="pm_card_visa")

    assert paid.status_code == 200, paid.text
    assert paid.json()["data"]["status"] == "paid"
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    attempt = shop.payments(order["id"])[0]
    intent = stripe_sdk.PaymentIntent.retrieve(attempt.payment_intent_id, api_key=_SECRET_KEY)
    assert (intent.status, intent.amount, intent.amount_received, intent.currency) == (
        "succeeded", 2500, 2500, "usd")
    assert intent.metadata["order_id"] == str(order["id"])

    partial = shop.refund(admin, order["id"], amount="10.00")
    assert partial.status_code == 200, partial.text
    assert (partial.json()["data"]["status"], partial.json()["data"]["payment_status"]) == (
        "succeeded", "paid")
    rest = shop.refund(admin, order["id"])
    assert rest.status_code == 200, rest.text
    assert (rest.json()["data"]["amount"], rest.json()["data"]["payment_status"]) == (
        "15.00", "refunded")

    assert payment_rows(shop, order["id"]) == [("succeeded", 2500, 2500, "USD", False)]
    assert _stripe_refunded(stripe_sdk, attempt.payment_intent_id) == [
        (1000, "succeeded"), (1500, "succeeded")]
    assert shop.refund(admin, order["id"], amount="0.01").status_code == 400


# -- Stripe's test PaymentMethods ------------------------------------------------------

@pytest.mark.parametrize("payment_method", SUCCEEDING)
def test_a_succeeding_payment_method_pays_the_order(shop, stripe_sdk, payment_method):
    _, buyer, order = _order(shop)

    response = shop.pay(buyer, order["id"], token=payment_method)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "paid"
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, order["id"]) == [("succeeded", 2500, 0, "USD", False)]
    intent = stripe_sdk.PaymentIntent.retrieve(
        shop.payments(order["id"])[0].payment_intent_id, api_key=_SECRET_KEY)
    assert (intent.status, intent.amount_received) == ("succeeded", 2500)


@pytest.mark.parametrize("payment_method", DECLINED)
def test_a_declined_payment_method_fails_the_attempt_with_a_message(shop, payment_method):
    _, buyer, order = _order(shop)

    response = shop.pay(buyer, order["id"], token=payment_method)

    assert response.status_code == 402, response.text
    message = response.json()["detail"]
    assert isinstance(message, str) and 10 < len(message) < 300
    assert "Traceback" not in message and "sk_" not in message
    assert order_state(shop, order["id"]) == ("new", "failed")
    assert payment_rows(shop, order["id"]) == [("failed", 2500, 0, "USD", False)]
    # The order stays payable with another card.
    retry = shop.pay(buyer, order["id"], token="pm_card_visa")
    assert retry.status_code == 200, retry.text
    assert order_state(shop, order["id"]) == ("confirmed", "paid")


@pytest.mark.parametrize("payment_method", THREE_DS_REQUIRED)
def test_a_3ds_payment_method_waits_for_authentication(shop, payment_method):
    _, buyer, order = _order(shop)

    response = shop.pay(buyer, order["id"], token=payment_method)

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert (body["status"], body["requires_action"]) == ("requires_action", True)
    assert body["client_secret"] and body["next_action"]["type"] == "redirect_to_url"
    assert order_state(shop, order["id"]) == ("new", "pending")
    assert payment_rows(shop, order["id"]) == [("requires_action", 2500, 0, "USD", False)]
    assert shop.pay(buyer, order["id"], token="pm_card_visa").status_code == 409


def test_an_optional_3ds_payment_method_is_paid_or_waits(shop):
    _, buyer, order = _order(shop)

    response = shop.pay(buyer, order["id"], token="pm_card_threeDSecureOptional")

    assert response.status_code == 200, response.text
    status = response.json()["data"]["status"]
    expected = {"paid": (("confirmed", "paid"), "succeeded"),
                "requires_action": (("new", "pending"), "requires_action")}[status]
    assert order_state(shop, order["id"]) == expected[0]
    assert shop.payments(order["id"])[0].status == expected[1]


@pytest.mark.parametrize("currency,payment_method", [("EUR", "pm_card_de"),
                                                     ("GBP", "pm_card_gb")])
def test_paying_in_the_store_currency(shop, stripe_sdk, monkeypatch, currency, payment_method):
    monkeypatch.setattr(settings, "STORE_CURRENCY", currency)
    _, buyer, order = _order(shop, price="19.99")
    assert order["currency"] == currency

    assert shop.pay(buyer, order["id"], token=payment_method,
                    currency="USD").status_code == 400
    response = shop.pay(buyer, order["id"], token=payment_method, currency=currency)

    assert response.status_code == 200, response.text
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 0, currency, False)]
    intent = stripe_sdk.PaymentIntent.retrieve(
        shop.payments(order["id"])[0].payment_intent_id, api_key=_SECRET_KEY)
    assert (intent.amount_received, intent.currency) == (1999, currency.lower())


# -- Refunds that settle later -----------------------------------------------------------

def test_a_pending_refund_is_completed_when_stripe_says_so(shop, stripe_sdk):
    _, buyer, order = _order(shop)
    admin = shop.admin()
    assert shop.pay(buyer, order["id"], token="pm_card_pendingRefund").status_code == 200

    response = shop.refund(admin, order["id"])

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert (body["status"], body["payment_status"]) == ("pending", "paid")
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    _final_status_or_skip()
    refund = _wait_for_refund(stripe_sdk, body["id"], {"succeeded", "failed"}, timeout=_SLOW_TIMEOUT)
    assert refund.status == "succeeded"
    assert _deliver(shop, "refund.updated", refund.to_dict()).status_code == 200
    assert order_state(shop, order["id"]) == ("confirmed", "refunded")
    assert payment_rows(shop, order["id"]) == [("succeeded", 2500, 2500, "USD", False)]


def test_a_refund_that_fails_later_is_undone_and_flagged(shop, stripe_sdk):
    _, buyer, order = _order(shop)
    admin = shop.admin()
    assert shop.pay(buyer, order["id"], token="pm_card_refundFail").status_code == 200

    response = shop.refund(admin, order["id"])

    assert response.status_code == 200, response.text
    # Stripe reports this refund succeeded at first, and the app counts it.
    assert response.json()["data"]["status"] == "succeeded"
    assert order_state(shop, order["id"]) == ("confirmed", "refunded")
    _final_status_or_skip()
    refund = _wait_for_refund(stripe_sdk, response.json()["data"]["id"], {"failed"}, timeout=_SLOW_TIMEOUT)
    assert refund.status == "failed"
    delivered = _deliver(shop, "refund.failed", refund.to_dict())
    assert delivered.json()["data"]["requires_manual_review"] is True
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, order["id"]) == [("succeeded", 2500, 0, "USD", False)]
    view = shop.client.get(f"/api/v1/orders/{order['id']}/payments", headers=admin)
    [flagged] = view.json()["data"][0]["refunds"]
    assert (flagged["status"], flagged["requires_manual_review"]) == ("failed", True)
