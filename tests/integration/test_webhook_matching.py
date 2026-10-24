"""
Which payment a Stripe event is applied to (app/api/v1/orders.py,
stripe_webhook), and what happens to an event that matches none.

- An object this app did not create (no metadata.order_id of ours) is
  acknowledged and recorded, and changes nothing - Stripe stops retrying.
- An event for one of our orders whose attempt has not stored its
  PaymentIntent/Checkout Session id yet (the event overtook the request
  that is still waiting on the provider's answer) is not lost: it is
  stored and acknowledged, and not applied: the worker retries it with a
  backoff and applies it once the id is stored.
"""
import uuid

import pytest
from payment_support import (
    Outbox,
    Shop,
    order_state,
    payment_rows,
    reset_provider,
)
from stripe_event_support import run_event

from app.models.payment import WebhookEvent
from app.services.payment.stripe_provider import StripePaymentProvider


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def outbox(monkeypatch):
    return Outbox(monkeypatch)


@pytest.fixture
def shop(client, db_session, monkeypatch):
    return Shop(client, db_session)


def _event_state(shop, event_id):
    shop.db.expire_all()
    row = shop.db.query(WebhookEvent).filter_by(event_id=event_id).one()
    return row.status, row.attempts


def _succeeded(intent_id, order_id=None, amount=1999, event_id=None):
    obj = {"id": intent_id, "object": "payment_intent", "amount": amount,
           "amount_received": amount, "currency": "usd", "status": "succeeded",
           "metadata": {} if order_id is None else {"order_id": str(order_id)}}
    return obj, event_id or f"evt_{uuid.uuid4().hex[:24]}"


def test_an_event_for_an_object_this_app_did_not_create_is_acknowledged_and_recorded(
    shop, outbox
):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    outbox.take()
    obj, event_id = _succeeded("pi_created_elsewhere")

    response = shop.webhook("payment_intent.succeeded", obj, event_id=event_id)

    assert response.status_code == 200
    assert response.json() == {"status": "processed", "applied": False}
    assert event_id in shop.webhook_event_ids()
    assert order_state(shop, order["id"]) == ("new", "pending")
    assert shop.payments(order["id"]) == []
    assert outbox.take() == []


def test_an_event_naming_an_order_with_no_attempt_waiting_for_its_id_is_acknowledged(
    shop, outbox
):
    # metadata.order_id alone proves nothing: another deployment sharing
    # the Stripe account numbers its orders the same way. Only an attempt
    # of that order still waiting to store its id makes the event ours.
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    obj, event_id = _succeeded("pi_another_deployment", order_id=order["id"])

    response = shop.webhook("payment_intent.succeeded", obj, event_id=event_id)

    assert response.status_code == 200
    assert response.json() == {"status": "processed", "applied": False}
    assert event_id in shop.webhook_event_ids()
    assert order_state(shop, order["id"]) == ("new", "pending")


def test_an_event_that_overtakes_its_pay_request_is_retried_not_lost(
    shop, outbox, monkeypatch
):
    # The success arrives while /pay still waits on the provider's answer,
    # before it stored the PaymentIntent id: no attempt has that id yet.
    # It used to be acknowledged as "not ours" and dropped; the order then
    # depended on /pay's own answer, and was lost with it if /pay died.
    product = shop.product(price="19.99", stock=3)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    outbox.take()
    early = {}
    real_create = StripePaymentProvider.create_payment_intent

    def _event_arrives_during_the_call(self, **kwargs):
        intent = real_create(self, **kwargs)
        obj, event_id = _succeeded(intent.id, order_id=order["id"])
        early["event"] = (obj, event_id)
        early["response"] = shop.webhook("payment_intent.succeeded", obj, event_id=event_id)
        return intent

    monkeypatch.setattr(StripePaymentProvider, "create_payment_intent",
                        _event_arrives_during_the_call)

    paid = shop.pay(buyer, order["id"])

    # Stripe got its 200; the event is stored, and the worker could not
    # apply it yet.
    assert early["response"].status_code == 200, early["response"].text
    assert early["response"].json() == {"status": "retrying"}
    obj, event_id = early["event"]
    # /pay itself completed the payment meanwhile; the worker's retry then
    # finds the attempt and applies the event (a no-op on an applied success).
    assert paid.status_code == 200, paid.text
    assert event_id in shop.webhook_event_ids()
    assert _event_state(shop, event_id) == ("retrying", 1)
    retry = run_event(event_id)
    assert retry == {"status": "processed", "applied": True}
    assert _event_state(shop, event_id) == ("processed", 1)
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 0, "USD", False)]
    assert [(e[0], e[4]) for e in outbox.take()] == [("status_update", "paid")]


def test_a_checkout_event_that_overtakes_its_session_request_is_retried(
    shop, monkeypatch
):
    product = shop.product(price="19.99", stock=3)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    early = {}
    real_create = StripePaymentProvider.create_checkout_session

    def _event_arrives_during_the_call(self, **kwargs):
        session = real_create(self, **kwargs)
        obj = {"id": session.id, "object": "checkout.session", "payment_status": "paid",
               "amount_total": 1999, "currency": "usd", "payment_intent": "pi_from_session",
               "metadata": {"order_id": str(order["id"])}}
        early["event"] = (obj, f"evt_{uuid.uuid4().hex[:24]}")
        early["response"] = shop.webhook("checkout.session.completed", obj,
                                         event_id=early["event"][1])
        return session

    monkeypatch.setattr(StripePaymentProvider, "create_checkout_session",
                        _event_arrives_during_the_call)

    assert shop.checkout_session(buyer, order["id"]).status_code == 200

    assert early["response"].status_code == 200
    assert early["response"].json() == {"status": "retrying"}
    assert order_state(shop, order["id"]) == ("new", "pending")
    obj, event_id = early["event"]
    assert _event_state(shop, event_id) == ("retrying", 1)
    retry = run_event(event_id)
    assert retry == {"status": "processed", "applied": True}
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert shop.payments(order["id"])[0].payment_intent_id == "pi_from_session"


@pytest.mark.parametrize("metadata", [
    {"order_id": "not-a-number"}, {"order_id": None}, "not-a-dict", {"order_id": 10 ** 30},
])
def test_unusable_order_metadata_is_treated_as_not_ours(shop, metadata):
    obj = {"id": "pi_odd_metadata", "object": "payment_intent", "amount": 100,
           "amount_received": 100, "currency": "usd", "metadata": metadata}

    response = shop.webhook("payment_intent.succeeded", obj)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "processed", "applied": False}

