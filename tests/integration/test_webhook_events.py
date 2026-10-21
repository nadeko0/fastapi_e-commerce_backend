"""
How the Stripe webhook (app/api/v1/orders.py, stripe_webhook) records the
events it receives: an event is recorded as processed together with what
it changed, so a delivery that fails half-way is processed again when
Stripe redelivers it, and only then.
"""
import pytest
from fastapi.testclient import TestClient
from payment_support import (
    PAY_3DS,
    Outbox,
    Shop,
    order_state,
    payment_rows,
    reset_provider,
    use_production_sessions,
)
from sqlalchemy.exc import OperationalError

import app.api.v1.orders as orders_module
from app.main import app


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
    use_production_sessions(monkeypatch)
    return Shop(client, db_session)


def _awaiting_3ds(shop):
    product = shop.product(price="19.99", stock=5)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    assert shop.pay(buyer, order["id"], token=PAY_3DS).status_code == 200
    return buyer, order, shop.payments(order["id"])[0]


def test_an_event_whose_processing_failed_is_applied_when_stripe_redelivers_it(
    shop, outbox, monkeypatch
):
    # The event id used to be committed as processed before the event was
    # applied. When applying it failed (a database error mid-way), Stripe
    # got a 500 and redelivered - and the redelivery was answered
    # "already_processed": the customer's payment was never applied and
    # the order would expire unpaid.
    buyer, order, payment = _awaiting_3ds(shop)
    outbox.take()
    real_apply = orders_module._apply_payment_success
    failures = []

    def _fails_once(*args, **kwargs):
        if not failures:
            failures.append(1)
            raise OperationalError("UPDATE orders ...", {}, Exception("connection lost"))
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(orders_module, "_apply_payment_success", _fails_once)
    event_id = "evt_redelivered_after_a_failure"

    # As a server answers an unhandled error: 500, not an exception.
    server = Shop(TestClient(app, raise_server_exceptions=False), shop.db)
    first = server.intent_event("payment_intent.succeeded", payment, event_id=event_id)
    assert first.status_code == 500
    assert order_state(shop, order["id"]) == ("new", "pending")
    assert event_id not in shop.webhook_event_ids()

    second = shop.intent_event("payment_intent.succeeded", payment, event_id=event_id)

    assert second.status_code == 200, second.text
    assert second.json()["data"] == {"status": "processed", "applied": True}
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 0, "USD", False)]
    assert event_id in shop.webhook_event_ids()
    assert [(e[0], e[4]) for e in outbox.take()] == [("status_update", "paid")]

    third = shop.intent_event("payment_intent.succeeded", payment, event_id=event_id)
    assert third.json()["data"] == {"status": "already_processed"}


# -- payment_intent.canceled ---------------------------------------------------

def test_a_canceled_payment_intent_fails_its_attempt_and_stops_blocking_the_order(
    shop, outbox
):
    # A PaymentIntent canceled at Stripe (from the Dashboard, or a 3D
    # Secure authentication the customer never finished, canceled by the
    # integration) can never be charged. Its event was not handled: the
    # attempt stayed requires_action, and while it did, every new attempt
    # was refused with 409 "Another payment attempt ... is in progress".
    buyer, order, payment = _awaiting_3ds(shop)
    outbox.take()
    assert shop.pay(buyer, order["id"]).status_code == 409

    response = shop.intent_event("payment_intent.canceled", payment,
                                 status="canceled", cancellation_reason="abandoned")

    assert response.status_code == 200, response.text
    assert payment_rows(shop, order["id"]) == [("failed", 1999, 0, "USD", False)]
    assert order_state(shop, order["id"]) == ("new", "failed")
    retry = shop.pay(buyer, order["id"])
    assert retry.status_code == 200, retry.text
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert [p[0] for p in payment_rows(shop, order["id"])] == ["failed", "succeeded"]


def test_a_cancel_reported_after_the_attempt_succeeded_changes_nothing(shop, outbox):
    buyer, order, payment = _awaiting_3ds(shop)
    assert shop.intent_event("payment_intent.succeeded", payment).status_code == 200
    outbox.take()

    response = shop.intent_event("payment_intent.canceled", payment, status="canceled")

    assert response.status_code == 200
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 0, "USD", False)]
    assert outbox.take() == []


# -- Event types with no handling of their own ------------------------------------

@pytest.mark.parametrize("event_type", [
    "payment_intent.created", "payment_intent.processing",
    "payment_intent.requires_action", "payment_intent.amount_capturable_updated",
])
def test_an_unhandled_event_for_a_known_attempt_is_recorded_not_reported_applied(
    shop, outbox, event_type
):
    # Stripe sends these for every PaymentIntent of the account; an
    # endpoint subscribed to them found the attempt, changed nothing, and
    # answered "applied": true - in the Dashboard's delivery log, the same
    # answer as a payment applied to the order.
    buyer, order, payment = _awaiting_3ds(shop)
    outbox.take()

    response = shop.intent_event(event_type, payment, event_id=f"evt_{event_type}")

    assert response.status_code == 200
    assert response.json()["data"] == {"status": "processed", "applied": False}
    assert f"evt_{event_type}" in shop.webhook_event_ids()
    assert order_state(shop, order["id"]) == ("new", "pending")
    assert payment_rows(shop, order["id"]) == [("requires_action", 1999, 0, "USD", False)]
    assert outbox.take() == []


# -- A report about an attempt that already paid the order --------------------------

@pytest.mark.parametrize("report", [
    {"amount": 1998}, {"amount": 2000}, {"currency": "eur"}, {"amount": 0},
])
def test_a_differing_report_about_the_attempt_that_paid_does_not_orphan_the_order(
    shop, outbox, report
):
    # Found by the stateful test (test_payment_state_machine.py): /pay
    # applied the success, then a payment_intent.succeeded for the same
    # attempt reported another amount or currency. The mismatch check ran
    # before anything else and flagged the attempt that had paid the
    # order: the order stayed "paid" with no unflagged charge behind it,
    # and could no longer be refunded ("No successful payment found").
    product = shop.product(price="19.99", stock=3)
    buyer = shop.buyer()
    admin = shop.admin()
    order = shop.order_of(buyer, product)
    assert shop.pay(buyer, order["id"]).status_code == 200
    payment = shop.payments(order["id"])[0]
    outbox.take()

    response = shop.intent_event("payment_intent.succeeded", payment, **report)

    assert response.status_code == 200
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 0, "USD", False)]
    assert outbox.take() == []
    assert shop.refund(admin, order["id"]).status_code == 200
    assert order_state(shop, order["id"]) == ("confirmed", "refunded")


def test_a_differing_session_report_about_the_attempt_that_paid_is_ignored(shop, outbox):
    product = shop.product(price="19.99", stock=3)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    assert shop.checkout_session(buyer, order["id"]).status_code == 200
    payment = shop.payments(order["id"])[0]
    assert shop.session_event("checkout.session.completed", payment,
                              payment_intent="pi_paid").status_code == 200

    response = shop.session_event("checkout.session.async_payment_succeeded",
                                  shop.payments(order["id"])[0], amount=1)

    assert response.status_code == 200
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 0, "USD", False)]
