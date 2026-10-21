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
