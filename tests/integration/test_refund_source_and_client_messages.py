"""
Two things about what /pay, /checkout-session and /refund tell their callers
and record:

- payment_refunds.source is "admin" for a refund made through /refund
  whichever of the endpoint and the refund.created webhook records it first;
  the webhook never changes the source of a row that exists.
- A provider's developer-level error text (object ids, amounts in its own
  format) is logged, never sent to the client. A card decline's message is
  for the customer and is kept.
"""
import uuid

import pytest
from conftest import TestingSessionLocal
from payment_support import Shop, reset_provider

from app.api.v1.orders import _record_refund
from app.models.payment_ledger import PaymentRefund
from app.services.payment.exceptions import InvalidRequestError
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.stripe_events import apply_event


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def shop(client, db_session, monkeypatch):
    return Shop(client, db_session)


@pytest.fixture
def paid(shop):
    product = shop.product(price="19.99", stock=4)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    assert shop.pay(buyer, order["id"]).status_code == 200
    return {"order": order, "buyer": buyer, "admin": shop.admin(),
            "payment": shop.payments(order["id"])[0]}


class _NoFollowUps:
    def add_task(self, *args, **kwargs):
        raise AssertionError("a refund event queues nothing")


def _refund_created(payment, refund):
    return {"object": {
        "id": refund.id, "object": "refund", "amount": refund.amount,
        "currency": payment.currency.lower(), "payment_intent": payment.payment_intent_id,
        "status": refund.status.value, "failure_reason": None, "metadata": {},
    }}


def _sources(shop):
    shop.db.expire_all()
    return [(r.refund_id[:3], r.source) for r in shop.db.query(PaymentRefund)
            .order_by(PaymentRefund.id)]


# -- Refund source ---------------------------------------------------------------

def test_the_endpoint_recording_first_keeps_its_admin_tag_when_the_webhook_follows(shop, paid):
    response = shop.refund(paid["admin"], paid["order"]["id"], amount="5.00")
    assert response.status_code == 200, response.text
    refund_id = response.json()["id"]

    shop.webhook("refund.created", {
        "id": refund_id, "object": "refund", "amount": 500,
        "currency": "usd", "payment_intent": paid["payment"].payment_intent_id,
        "status": "succeeded", "failure_reason": None, "metadata": {}})

    assert _sources(shop) == [("re_", "admin")]


def test_the_webhook_committed_before_the_endpoint_records_still_ends_as_admin(
        shop, paid, monkeypatch):
    # The worker applies refund.created while /refund is between the
    # provider's answer and its own commit: the row is first recorded as
    # "provider", and the admin's call corrects it.
    original = StripePaymentProvider.create_refund
    payment = paid["payment"]

    def create_then_deliver_the_webhook(self, **kwargs):
        refund = original(self, **kwargs)
        with TestingSessionLocal() as db:
            apply_event(db, "refund.created", _refund_created(payment, refund), _NoFollowUps())
        return refund

    monkeypatch.setattr(StripePaymentProvider, "create_refund", create_then_deliver_the_webhook)

    response = shop.refund(paid["admin"], paid["order"]["id"], amount="5.00")

    assert response.status_code == 200, response.text
    assert _sources(shop) == [("re_", "admin")]
    shop.db.expire_all()
    assert shop.payments(paid["order"]["id"])[0].amount_refunded == 500  # counted once


def test_a_refund_first_seen_in_a_webhook_is_a_provider_refund(shop, paid):
    refund_id = f"re_{uuid.uuid4().hex[:24]}"

    shop.webhook("refund.created", {
        "id": refund_id, "object": "refund", "amount": 500, "currency": "usd",
        "payment_intent": paid["payment"].payment_intent_id,
        "status": "succeeded", "failure_reason": None, "metadata": {}})
    shop.webhook("refund.updated", {
        "id": refund_id, "object": "refund", "amount": 500, "currency": "usd",
        "payment_intent": paid["payment"].payment_intent_id,
        "status": "succeeded", "failure_reason": None, "metadata": {}})

    assert _sources(shop) == [("re_", "provider")]


def test_recording_a_known_refund_again_as_provider_keeps_its_source(shop, paid):
    refund_id = f"re_{uuid.uuid4().hex[:24]}"
    payment = shop.payments(paid["order"]["id"])[0]
    first = _record_refund(shop.db, payment, refund_id, 500, "succeeded", source="provider")
    shop.db.flush()

    again = _record_refund(shop.db, payment, refund_id, 500, "succeeded", source="provider")

    assert again is first
    assert again.source == "provider"
    assert payment.amount_refunded == 500  # counted once


# -- Provider text stays out of the answers ----------------------------------------

def test_pay_with_an_invalid_payment_method_does_not_echo_the_providers_message(
        shop, paid, monkeypatch):
    other = shop.order_of(paid["buyer"], shop.product(price="5.00"))

    def refuse(self, **kwargs):
        raise InvalidRequestError("No such PaymentMethod: 'pm_garbage'", param="payment_method",
                                  code="resource_missing")

    monkeypatch.setattr(StripePaymentProvider, "create_payment_intent", refuse)

    response = shop.pay(paid["buyer"], other["id"], token="pm_garbage")

    assert response.status_code == 400
    assert "pm_garbage" not in response.text
    assert "PaymentMethod" not in response.text
    assert response.json()["detail"] == (
        "The payment details were not accepted: check the payment method and try again")


def test_a_card_decline_keeps_the_customer_facing_message(shop, paid):
    from payment_support import PAY_DECLINED

    other = shop.order_of(paid["buyer"], shop.product(price="5.00"))

    response = shop.pay(paid["buyer"], other["id"], token=PAY_DECLINED)

    assert response.status_code == 422
    assert response.json()["detail"]


def test_checkout_session_refused_as_invalid_does_not_echo_the_providers_message(
        shop, paid, monkeypatch):
    other = shop.order_of(paid["buyer"], shop.product(price="5.00"))

    def refuse(self, **kwargs):
        raise InvalidRequestError("Invalid integer: line_items[0][price_data][unit_amount]",
                                  code="parameter_invalid_integer")

    monkeypatch.setattr(StripePaymentProvider, "create_checkout_session", refuse)

    response = shop.checkout_session(paid["buyer"], other["id"])

    assert response.status_code == 400
    assert "line_items" not in response.text


def test_an_over_refund_is_answered_in_our_wording_and_amounts(shop, paid):
    order_id = paid["order"]["id"]
    assert shop.refund(paid["admin"], order_id, amount="2.21").status_code == 200

    response = shop.refund(paid["admin"], order_id, amount="20.00")

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Refund amount exceeds the refundable amount of this payment (17.78 USD)")


def test_a_refund_refused_for_another_reason_gets_a_generic_message(shop, paid, monkeypatch):
    def refuse(self, **kwargs):
        raise InvalidRequestError("This PaymentIntent does not have a successful charge to refund.")

    monkeypatch.setattr(StripePaymentProvider, "create_refund", refuse)

    response = shop.refund(paid["admin"], paid["order"]["id"], amount="1.00")

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "The refund could not be processed: the payment provider refused it")
