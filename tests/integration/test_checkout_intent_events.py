"""
payment_intent.* events for a Checkout Session attempt.

The Payment row of a Checkout attempt holds a "pending-cs-..." placeholder in
payment_intent_id until checkout.session.completed stores the real id, so a
payment_intent.succeeded / payment_failed event used to find no row and was
acknowledged as "not applied": money taken with the session event lost or
failing left the order unpaid. The session's PaymentIntent now carries
{order_id, payment_id} metadata (payment_intent_data.metadata), and the event
finds its attempt through it.

- payment_intent.succeeded pays the order, the same as checkout.session.completed
  does, and the two together (either order, or either one twice) have one effect.
- payment_intent.payment_failed on a Checkout attempt is only logged: the
  customer can retry on the same open Session.
"""
import logging
import uuid

import pytest
from payment_support import Outbox, Shop, order_state, payment_rows, reset_provider


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


@pytest.fixture
def attempt(shop):
    """An order with an open Checkout Session attempt, not completed."""
    product = shop.product(price="19.99", stock=4)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    assert shop.checkout_session(buyer, order["id"]).status_code == 200
    (payment,) = shop.payments(order["id"])
    assert payment.payment_intent_id.startswith("pending-cs-")
    return {"order": order, "product": product, "payment": payment,
            "intent_id": f"pi_{uuid.uuid4().hex[:24]}"}


def _intent(attempt, **extra):
    payment = attempt["payment"]
    return {
        "id": attempt["intent_id"], "object": "payment_intent",
        "amount": payment.amount, "amount_received": payment.amount,
        "currency": payment.currency.lower(),
        # What create_checkout_session sends as payment_intent_data.metadata.
        "metadata": {"order_id": str(payment.order_id), "payment_id": str(payment.id)},
        **extra,
    }


def _succeeded(shop, attempt, **kwargs):
    return shop.webhook("payment_intent.succeeded", _intent(attempt), **kwargs)


def _completed(shop, attempt):
    return shop.session_event(
        "checkout.session.completed", attempt["payment"], payment_intent=attempt["intent_id"])


def _paid_once(shop, attempt, outbox):
    order_id = attempt["order"]["id"]
    assert order_state(shop, order_id) == ("confirmed", "paid")
    assert payment_rows(shop, order_id) == [("succeeded", 1999, 0, "USD", False)]
    assert shop.payments(order_id)[0].payment_intent_id == attempt["intent_id"]
    assert [kind for kind, *_ in outbox.take()] == ["status_update"]
    # Stock was taken when the order was placed and is not touched again.
    assert shop.stock(attempt["product"]) == 3


def test_payment_intent_succeeded_alone_pays_the_order(shop, attempt, outbox):
    # checkout.session.completed was lost or failed: the money is taken.
    outbox.take()

    response = _succeeded(shop, attempt)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "processed", "applied": True}
    _paid_once(shop, attempt, outbox)


def test_checkout_session_completed_alone_still_pays_the_order(shop, attempt, outbox):
    outbox.take()

    response = _completed(shop, attempt)

    assert response.json() == {"status": "processed", "applied": True}
    _paid_once(shop, attempt, outbox)


@pytest.mark.parametrize("first", ["intent", "session"])
def test_both_events_in_either_order_have_one_effect(shop, attempt, outbox, first):
    outbox.take()
    events = {"intent": lambda: _succeeded(shop, attempt),
              "session": lambda: _completed(shop, attempt)}
    second = "session" if first == "intent" else "intent"

    assert events[first]().json()["applied"] is True
    assert events[second]().json()["applied"] is True

    _paid_once(shop, attempt, outbox)


def test_the_same_intent_event_delivered_twice_has_one_effect(shop, attempt, outbox):
    outbox.take()

    first = _succeeded(shop, attempt, event_id="evt_same_intent")
    again = _succeeded(shop, attempt, event_id="evt_same_intent")

    assert first.json() == {"status": "processed", "applied": True}
    assert again.json() == {"status": "already_processed"}
    _paid_once(shop, attempt, outbox)


def test_two_intent_events_with_different_ids_have_one_effect(shop, attempt, outbox):
    # Stripe redelivers under a new event id when the integration was
    # reconfigured, and a replay from the admin reuses the stored one.
    outbox.take()

    assert _succeeded(shop, attempt).json()["applied"] is True
    assert _succeeded(shop, attempt).json()["applied"] is True

    _paid_once(shop, attempt, outbox)


def test_a_decline_then_a_success_on_the_same_session(shop, attempt, outbox, caplog):
    # The customer's first card is declined on the hosted page, then a second
    # card pays. The decline must not close the attempt or fail the order.
    order_id = attempt["order"]["id"]
    outbox.take()

    with caplog.at_level(logging.WARNING, logger="app.services.stripe_events"):
        declined = shop.webhook(
            "payment_intent.payment_failed",
            _intent(attempt, amount_received=0, last_payment_error={
                "type": "card_error", "code": "card_declined", "decline_code": "insufficient_funds"}),
            event_id="evt_decline_1",
        )

    assert declined.json() == {"status": "processed", "applied": False}
    assert order_state(shop, order_id) == ("new", "pending")
    assert payment_rows(shop, order_id) == [("processing", 1999, 0, "USD", False)]
    log = [r.getMessage() for r in caplog.records if "declined on the hosted page" in r.getMessage()]
    assert len(log) == 1
    assert f"order {order_id}" in log[0]
    assert "evt_decline_1" in log[0]
    assert "insufficient_funds" in log[0]

    assert _succeeded(shop, attempt).json() == {"status": "processed", "applied": True}
    _paid_once(shop, attempt, outbox)


def test_a_decline_after_the_session_completed_changes_nothing(shop, attempt, outbox):
    # Once the intent id is stored a late payment_failed matches the row by it.
    _completed(shop, attempt)
    outbox.take()

    late = shop.webhook("payment_intent.payment_failed",
                        _intent(attempt, amount_received=0))

    assert late.json() == {"status": "processed", "applied": False}
    assert order_state(shop, attempt["order"]["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, attempt["order"]["id"]) == [("succeeded", 1999, 0, "USD", False)]


@pytest.mark.parametrize("metadata", [
    {},
    {"order_id": "{order}"},
    {"order_id": "{order}", "payment_id": "999999"},
    {"order_id": "999999", "payment_id": "{payment}"},
    {"order_id": "{order}", "payment_id": "not-a-number"},
])
def test_an_intent_whose_metadata_does_not_name_the_attempt_is_not_applied(
        shop, attempt, outbox, metadata):
    # Metadata alone is not trusted: both ids have to agree with one row.
    payment = attempt["payment"]
    metadata = {k: v.format(order=payment.order_id, payment=payment.id) for k, v in metadata.items()}
    outbox.take()

    response = shop.webhook("payment_intent.succeeded", _intent(attempt, metadata=metadata))

    assert response.json() == {"status": "processed", "applied": False}
    assert order_state(shop, payment.order_id) == ("new", "pending")
    assert outbox.take() == []


def test_a_charge_of_another_amount_through_the_intent_is_flagged_not_applied(shop, attempt):
    order_id = attempt["order"]["id"]

    response = shop.webhook("payment_intent.succeeded", _intent(attempt, amount_received=1))

    assert response.json() == {"status": "processed", "applied": False,
                               "requires_manual_review": True}
    assert order_state(shop, order_id) == ("new", "pending")
    assert payment_rows(shop, order_id) == [("succeeded", 1999, 0, "USD", True)]


def test_the_webhook_route_logs_the_stored_event(client, shop, attempt, caplog):
    with caplog.at_level(logging.INFO, logger="app.api.v1.orders"):
        shop.webhook("payment_intent.succeeded", _intent(attempt), event_id="evt_logged_1")

    lines = [r.getMessage() for r in caplog.records if "evt_logged_1" in r.getMessage()]
    assert lines == ["Stripe event evt_logged_1 (payment_intent.succeeded) stored"]
