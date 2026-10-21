"""
Business rules of paying and refunding, beyond the per-feature tests in
test_payment.py and test_checkout_session.py: refund arithmetic, payment
idempotency, webhook tampering and replay, and who may act on an order.
"""
import json
import time
import uuid

import pytest
from test_payment import (
    ORDER_AMOUNT_CENTS,
    ORDER_CURRENCY_REPORTED,
    ORDERS_PREFIX,
    WEBHOOK_URL,
    _create_admin,
    _create_verified_buyer_with_order,
    _get_order,
    _get_payments,
    _pay_requiring_action,
    _payment_payload,
    _post_webhook,
    _sign,
    _webhook_body,
)

from app.models.payment import WebhookEvent
from app.models.product import Product
from app.services.payment.stripe_provider import StripePaymentProvider


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    StripePaymentProvider.reset_mock_state()
    monkeypatch.setattr("app.api.v1.orders.send_order_confirmation_email", lambda *a, **k: True)
    monkeypatch.setattr("app.api.v1.orders.send_order_status_update_email", lambda *a, **k: True)
    yield
    StripePaymentProvider.reset_mock_state()


def _pay(client, setup, key=None):
    # /pay requires an Idempotency-Key: without one given, a fresh key, so
    # each call is a new payment attempt.
    headers = {**setup["headers"], "Idempotency-Key": key or uuid.uuid4().hex}
    return client.post(
        f"{ORDERS_PREFIX}/{setup['order']['id']}/pay",
        json=_payment_payload(setup["order"]),
        headers=headers,
    )


def _refund(client, order_id, headers, amount=None, key=None):
    body = {} if amount is None else {"amount": amount}
    headers = {**headers, "Idempotency-Key": key or uuid.uuid4().hex}
    return client.post(f"{ORDERS_PREFIX}/{order_id}/refund", json=body, headers=headers)


@pytest.fixture
def paid(client, db_session):
    """A paid 19.99 order and an admin."""
    setup = _create_verified_buyer_with_order(client, db_session)
    assert _pay(client, setup).status_code == 200
    setup["admin"] = _create_admin(db_session)["headers"]
    return setup


# -- Refunds -----------------------------------------------------------------

def test_refund_larger_than_the_charge_is_rejected_and_changes_nothing(client, db_session, paid):
    order_id = paid["order"]["id"]

    response = _refund(client, order_id, paid["admin"], amount="20.00")

    assert response.status_code == 400
    assert _get_order(db_session, order_id).payment_status == "paid"
    assert _get_payments(db_session, order_id)[0].amount_refunded == 0


def test_partial_refunds_add_up_to_a_full_refund(client, db_session, paid):
    order_id = paid["order"]["id"]

    first = _refund(client, order_id, paid["admin"], amount="5.00")
    second = _refund(client, order_id, paid["admin"], amount="14.99")

    assert first.json()["data"]["payment_status"] == "paid"
    assert second.json()["data"]["payment_status"] == "refunded"
    payment = _get_payments(db_session, order_id)[0]
    assert payment.amount_refunded == payment.amount == ORDER_AMOUNT_CENTS


def test_partial_refund_beyond_what_is_left_is_rejected(client, db_session, paid):
    order_id = paid["order"]["id"]
    assert _refund(client, order_id, paid["admin"], amount="15.00").status_code == 200

    response = _refund(client, order_id, paid["admin"], amount="5.00")

    assert response.status_code == 400
    assert _get_payments(db_session, order_id)[0].amount_refunded == 1500
    assert _get_order(db_session, order_id).payment_status == "paid"


def test_full_refund_after_a_partial_one_returns_the_rest(client, db_session, paid):
    order_id = paid["order"]["id"]
    _refund(client, order_id, paid["admin"], amount="4.99")

    response = _refund(client, order_id, paid["admin"])

    assert response.status_code == 200
    assert response.json()["data"]["amount"] == "15.00"
    assert response.json()["data"]["payment_status"] == "refunded"


def test_a_refunded_order_cannot_be_refunded_again(client, db_session, paid):
    order_id = paid["order"]["id"]
    assert _refund(client, order_id, paid["admin"]).status_code == 200

    again = _refund(client, order_id, paid["admin"])

    assert again.status_code == 400
    payment = _get_payments(db_session, order_id)[0]
    assert payment.amount_refunded == payment.amount


@pytest.mark.parametrize("amount", ["0", "-1.00"])
def test_refund_amount_must_be_positive(client, paid, amount):
    response = _refund(client, paid["order"]["id"], paid["admin"], amount=amount)
    assert response.status_code == 422


def test_refund_does_not_touch_stock_even_after_an_admin_cancelled_the_paid_order(
    client, db_session, paid
):
    # Cancelling returns the stock; the refund that follows returns money
    # only. Restocking again there would count the units twice.
    order_id = paid["order"]["id"]
    product_id = paid["product"].id
    cancelled = client.put(f"{ORDERS_PREFIX}/{order_id}/status",
                           params={"status": "cancelled"}, headers=paid["admin"])
    assert cancelled.status_code == 200
    db_session.expire_all()
    assert db_session.get(Product, product_id).stock_quantity == 10

    refund = _refund(client, order_id, paid["admin"])

    assert refund.json()["data"]["payment_status"] == "refunded"
    db_session.expire_all()
    assert db_session.get(Product, product_id).stock_quantity == 10


def test_refund_is_admin_only(client, db_session, paid):
    response = _refund(client, paid["order"]["id"], paid["headers"])
    assert response.status_code == 403
    assert _get_order(db_session, paid["order"]["id"]).payment_status == "paid"


def test_refund_of_an_unknown_order_is_404(client, paid):
    assert _refund(client, 999_999, paid["admin"]).status_code == 404


# -- Payment idempotency -----------------------------------------------------

def test_replaying_a_successful_pay_returns_the_same_attempt_and_charges_once(
    client, db_session
):
    setup = _create_verified_buyer_with_order(client, db_session)

    first = _pay(client, setup, key="same-key")
    replay = _pay(client, setup, key="same-key")

    assert first.status_code == replay.status_code == 200
    assert replay.json()["data"]["id"] == first.json()["data"]["id"]
    assert replay.json()["data"]["transaction_id"] == first.json()["data"]["transaction_id"]
    assert len(_get_payments(db_session, setup["order"]["id"])) == 1
    assert len(StripePaymentProvider._intents_by_id) == 1


def test_a_new_key_for_a_paid_order_is_refused_not_charged(client, db_session):
    setup = _create_verified_buyer_with_order(client, db_session)
    assert _pay(client, setup, key="attempt-1").status_code == 200

    second = _pay(client, setup, key="attempt-2")

    assert second.status_code == 400
    assert second.json()["detail"] == "Order already paid"
    assert len(_get_payments(db_session, setup["order"]["id"])) == 1
    assert len(StripePaymentProvider._intents_by_id) == 1


def test_replaying_a_checkout_session_after_the_order_was_paid_returns_it(client, db_session):
    # The idempotency check comes before the "already paid" guard, so a
    # client retrying its own request gets its original answer.
    setup = _create_verified_buyer_with_order(client, db_session)
    url = f"{ORDERS_PREFIX}/{setup['order']['id']}/checkout-session"
    headers = {**setup["headers"], "Idempotency-Key": "session-key"}
    first = client.post(url, headers=headers)
    assert _pay(client, setup).status_code == 200

    replay = client.post(url, headers=headers)
    fresh = client.post(url, headers={**setup["headers"], "Idempotency-Key": "another-key"})

    assert replay.status_code == 200
    assert replay.json()["data"]["checkout_session_id"] == first.json()["data"]["checkout_session_id"]
    assert fresh.status_code == 400


# -- Webhook signature and replay ----------------------------------------------

def test_webhook_with_a_body_changed_after_signing_is_rejected(client, db_session):
    setup = _create_verified_buyer_with_order(client, db_session)
    intent_id = _pay_requiring_action(client, setup)
    signed = _webhook_body("payment_intent.payment_failed", intent_id)
    tampered = _webhook_body("payment_intent.succeeded", intent_id,
                             amount=ORDER_AMOUNT_CENTS, currency=ORDER_CURRENCY_REPORTED)

    response = _post_webhook(client, tampered, _sign(signed))

    assert response.status_code == 400
    assert db_session.query(WebhookEvent).count() == 0
    assert _get_order(db_session, setup["order"]["id"]).payment_status == "pending"


def test_webhook_signed_too_long_ago_is_rejected(client, db_session):
    setup = _create_verified_buyer_with_order(client, db_session)
    intent_id = _pay_requiring_action(client, setup)
    body = _webhook_body("payment_intent.succeeded", intent_id,
                         amount=ORDER_AMOUNT_CENTS, currency=ORDER_CURRENCY_REPORTED)

    response = _post_webhook(client, body, _sign(body, timestamp=int(time.time()) - 3600))

    assert response.status_code == 400
    assert _get_order(db_session, setup["order"]["id"]).payment_status == "pending"


def test_a_processed_event_resent_with_a_fresh_signature_is_not_applied_again(
    client, db_session
):
    # A replay with a new timestamp passes the signature and tolerance
    # checks; the event id is what stops it.
    setup = _create_verified_buyer_with_order(client, db_session)
    intent_id = _pay_requiring_action(client, setup)
    event_id = f"evt_{uuid.uuid4().hex[:24]}"
    body = _webhook_body("payment_intent.succeeded", intent_id, event_id=event_id,
                         amount=ORDER_AMOUNT_CENTS, currency=ORDER_CURRENCY_REPORTED)
    assert _post_webhook(client, body, _sign(body)).json()["data"]["applied"] is True
    # The order moves on (refunded by support); replaying the old success
    # must not touch it.
    order = _get_order(db_session, setup["order"]["id"])
    order.payment_status = "refunded"
    db_session.commit()

    replay = _post_webhook(client, body, _sign(body, timestamp=int(time.time()) + 1))

    assert replay.json()["data"]["status"] == "already_processed"
    assert _get_order(db_session, setup["order"]["id"]).payment_status == "refunded"


def test_an_event_type_the_app_does_not_handle_changes_nothing(client, db_session):
    setup = _create_verified_buyer_with_order(client, db_session)
    intent_id = _pay_requiring_action(client, setup)
    body = json.dumps({"id": f"evt_{uuid.uuid4().hex[:24]}", "type": "charge.dispute.created",
                       "created": int(time.time()), "data": {"object": {"id": intent_id}}}).encode()

    response = client.post(WEBHOOK_URL, content=body, headers={
        "Stripe-Signature": _sign(body), "Content-Type": "application/json"})

    assert response.status_code == 200
    assert _get_order(db_session, setup["order"]["id"]).payment_status == "pending"
    assert _get_payments(db_session, setup["order"]["id"])[0].status == "requires_action"


# -- Who may act on an order ---------------------------------------------------

@pytest.mark.parametrize("action", ["pay", "checkout-session", "cancel"])
def test_a_customer_cannot_act_on_another_customers_order(client, db_session, action):
    victim = _create_verified_buyer_with_order(client, db_session, email="victim@example.com")
    attacker = _create_verified_buyer_with_order(client, db_session, email="attacker@example.com")
    url = f"{ORDERS_PREFIX}/{victim['order']['id']}/{action}"

    # With a valid Idempotency-Key, so the request reaches the ownership check.
    headers = {**attacker["headers"], "Idempotency-Key": uuid.uuid4().hex}
    if action == "pay":
        response = client.post(url, json=_payment_payload(victim["order"]), headers=headers)
    else:
        response = client.post(url, headers=headers)

    # 404, not 403: the order's existence is not revealed either.
    assert response.status_code == 404
    order = _get_order(db_session, victim["order"]["id"])
    assert (order.status, order.payment_status) == ("new", "pending")
    assert _get_payments(db_session, victim["order"]["id"]) == []


def test_a_customer_cannot_read_another_customers_order(client, db_session):
    victim = _create_verified_buyer_with_order(client, db_session, email="victim@example.com")
    attacker = _create_verified_buyer_with_order(client, db_session, email="attacker@example.com")

    response = client.get(f"{ORDERS_PREFIX}/{victim['order']['id']}", headers=attacker["headers"])

    assert response.status_code == 404


@pytest.mark.parametrize("action", ["pay", "checkout-session", "cancel", "refund"])
def test_order_actions_require_authentication(client, db_session, action):
    setup = _create_verified_buyer_with_order(client, db_session)
    url = f"{ORDERS_PREFIX}/{setup['order']['id']}/{action}"
    body = _payment_payload(setup["order"]) if action == "pay" else {}

    assert client.post(url, json=body).status_code == 401
