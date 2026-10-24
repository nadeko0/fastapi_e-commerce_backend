"""
A reused Idempotency-Key with a different request payload is answered 422
(draft-ietf-httpapi-idempotency-key-header-08, sections 2.4 and 2.7), on
POST /orders, POST /orders/{id}/pay and POST /orders/{id}/checkout-session.
The same key with the same payload replays the first result, and an attempt
stored before the fingerprint column existed (NULL) replays whatever the
payload. A refund is not stored locally under its key: the provider answers
a reused key with other parameters (409), covered in test_payment.py.
"""
import threading

import pytest
from conftest import IS_POSTGRES, TestingSessionLocal
from fastapi import HTTPException
from payment_support import PAY_3DS, Outbox, Shop, new_key, reset_provider

import app.api.v1.orders as orders_module
from app.core.idempotency import REUSED_KEY_DETAIL, ensure_same_request, request_fingerprint
from app.models.address import Address
from app.models.order import Order
from app.models.payment import Payment
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.redis_async import AsyncRedisService

_needs_no_row_locks = pytest.mark.skipif(
    IS_POSTGRES, reason="the simulated twin would wait for the order row lock"
)


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def shop(client, db_session, monkeypatch):
    Outbox(monkeypatch)
    return Shop(client, db_session)


def _insert_from_another_session(row):
    db = TestingSessionLocal()
    try:
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _forget_fingerprints(shop):
    """Make every stored key look like one from before the column existed."""
    shop.db.query(Order).update({Order.idempotency_fingerprint: None})
    shop.db.query(Payment).update({Payment.idempotency_fingerprint: None})
    shop.db.commit()


def _second_address(shop, buyer):
    address = Address(user_id=buyer.user_id, street="2 Side Street", city="Gotham",
                      state="NY", postal_code="10002", country="US")
    shop.db.add(address)
    shop.db.commit()
    return address.id


def _count_calls(monkeypatch, name):
    calls = []
    real = getattr(StripePaymentProvider, name)

    def _counted(self, *args, **kwargs):
        calls.append(kwargs)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(StripePaymentProvider, name, _counted)
    return calls


# -- the helper ----------------------------------------------------------------

def test_the_fingerprint_is_canonical():
    a = request_fingerprint(operation="pay", body={"amount": "1.00", "token": "t"})
    b = request_fingerprint(body={"token": "t", "amount": "1.00"}, operation="pay")
    assert a == b
    assert len(a) == 64
    assert a != request_fingerprint(operation="pay", body={"amount": "1.01", "token": "t"})


def test_ensure_same_request():
    fingerprint = request_fingerprint(a=1)
    ensure_same_request(None, fingerprint)
    ensure_same_request(fingerprint, fingerprint)
    with pytest.raises(HTTPException) as raised:
        ensure_same_request(request_fingerprint(a=2), fingerprint)
    assert raised.value.status_code == 422
    assert raised.value.detail == REUSED_KEY_DETAIL


# -- POST /orders ----------------------------------------------------------------

def test_order_key_reused_with_another_address_answers_422(shop):
    product = shop.product(stock=5)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product, 2).status_code == 200
    key = new_key()
    assert shop.place_order(buyer, key=key).status_code == 200
    other = _second_address(shop, buyer)

    response = shop.client.post(
        "/api/v1/orders", json={"shipping_address_id": other},
        headers={**buyer.headers, "Idempotency-Key": key},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": REUSED_KEY_DETAIL}
    assert shop.db.query(Order).count() == 1


def test_order_key_reused_with_the_same_address_replays(shop):
    product = shop.product(stock=5)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product, 2).status_code == 200
    key = new_key()
    first = shop.place_order(buyer, key=key)
    second = shop.place_order(buyer, key=key)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert shop.db.query(Order).one().idempotency_fingerprint is not None


def test_order_stored_without_a_fingerprint_replays_for_any_address(shop):
    product = shop.product(stock=5)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product, 2).status_code == 200
    key = new_key()
    first = shop.place_order(buyer, key=key)
    _forget_fingerprints(shop)
    other = _second_address(shop, buyer)

    response = shop.client.post(
        "/api/v1/orders", json={"shipping_address_id": other},
        headers={**buyer.headers, "Idempotency-Key": key},
    )

    assert response.status_code == 200
    assert response.json()["id"] == first.json()["id"]


@_needs_no_row_locks
def test_order_racing_a_twin_with_another_address_answers_422(shop, monkeypatch):
    product = shop.product(stock=5)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product, 2).status_code == 200
    key = new_key()
    real_get_cart = AsyncRedisService.get_cart
    twin = []

    async def _twin_commits_first(self, user_id):
        if not twin:
            twin.append(_insert_from_another_session(Order(
                user_id=buyer.user_id, status="new", payment_status="pending",
                shipping_address_id=buyer.address_id, idempotency_key=key,
                idempotency_fingerprint="0" * 64,
                total_amount=15, currency="USD",
            )))
        return await real_get_cart(self, user_id)

    monkeypatch.setattr(AsyncRedisService, "get_cart", _twin_commits_first)

    response = shop.place_order(buyer, key=key)

    assert response.status_code == 422
    assert response.json() == {"detail": REUSED_KEY_DETAIL}
    assert shop.db.query(Order).count() == 1


# -- POST /orders/{id}/pay -------------------------------------------------------

def test_pay_key_reused_with_another_body_answers_422_and_charges_once(shop, monkeypatch):
    product = shop.product(price="39.98")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    created = _count_calls(monkeypatch, "create_payment_intent")
    key = new_key()
    assert shop.pay(buyer, order["id"], key=key).status_code == 200

    other_token = shop.pay(buyer, order["id"], token=PAY_3DS, key=key)
    other_amount = shop.pay(buyer, order["id"], amount="1.00", key=key)
    other_currency = shop.pay(buyer, order["id"], currency="USD", key=key)

    for response in (other_token, other_amount, other_currency):
        assert response.status_code == 422, response.text
        assert response.json() == {"detail": REUSED_KEY_DETAIL}
    assert len(created) == 1
    assert len(shop.payments(order["id"])) == 1


def test_pay_key_reused_with_the_same_body_replays(shop, monkeypatch):
    product = shop.product(price="39.98")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    created = _count_calls(monkeypatch, "create_payment_intent")
    key = new_key()
    first = shop.pay(buyer, order["id"], key=key)
    # 39.980 is the same amount as 39.98 once validated.
    second = shop.pay(buyer, order["id"], amount="39.980", key=key)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert len(created) == 1
    assert shop.payments(order["id"])[0].idempotency_fingerprint is not None


def test_pay_attempt_stored_without_a_fingerprint_replays_for_any_body(shop):
    product = shop.product(price="39.98")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    first = shop.pay(buyer, order["id"], key=key)
    _forget_fingerprints(shop)

    second = shop.pay(buyer, order["id"], token=PAY_3DS, amount="1.00", key=key)

    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


@_needs_no_row_locks
def test_pay_racing_a_twin_with_another_body_answers_422(shop, monkeypatch):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    real_guard = orders_module._guard_currency_matches_order

    def _twin_records_its_attempt_first(requested_currency, order_row):
        _insert_from_another_session(Payment(
            order_id=order_row.id, provider="stripe", payment_intent_id="pi_twin_attempt",
            idempotency_key=key, idempotency_fingerprint="0" * 64,
            amount=1999, currency="USD", status="processing",
        ))
        return real_guard(requested_currency, order_row)

    monkeypatch.setattr(
        orders_module, "_guard_currency_matches_order", _twin_records_its_attempt_first
    )

    response = shop.pay(buyer, order["id"], key=key)

    assert response.status_code == 422
    assert len(shop.payments(order["id"])) == 1


# -- POST /orders/{id}/checkout-session ------------------------------------------

def test_checkout_key_reused_with_another_body_answers_422(shop, monkeypatch):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    created = _count_calls(monkeypatch, "create_checkout_session")
    key = new_key()
    assert shop.checkout_session(buyer, order["id"], key=key).status_code == 200

    response = shop.checkout_session(buyer, order["id"], key=key, payload={"currency": "USD"})

    assert response.status_code == 422, response.text
    assert response.json() == {"detail": REUSED_KEY_DETAIL}
    assert len(created) == 1
    assert len(shop.payments(order["id"])) == 1


def test_checkout_key_reused_with_the_same_body_replays(shop):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    first = shop.checkout_session(buyer, order["id"], key=key)
    # No body and an empty object are the same request.
    second = shop.checkout_session(buyer, order["id"], key=key, payload={})
    assert second.status_code == 200
    assert (
        second.json()["checkout_session_id"]
        == first.json()["checkout_session_id"]
    )


def test_checkout_attempt_stored_without_a_fingerprint_replays_for_any_body(shop):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    first = shop.checkout_session(buyer, order["id"], key=key)
    _forget_fingerprints(shop)

    second = shop.checkout_session(buyer, order["id"], key=key, payload={"currency": "USD"})

    assert second.status_code == 200
    assert second.json()["payment_id"] == first.json()["payment_id"]


def test_the_key_of_a_pay_attempt_on_checkout_session_keeps_its_409(shop):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    assert shop.pay(buyer, order["id"], key=key).status_code == 200
    response = shop.checkout_session(buyer, order["id"], key=key, payload={"currency": "USD"})
    assert response.status_code == 409


def test_the_key_of_a_checkout_attempt_on_pay_is_not_compared(shop):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    assert shop.checkout_session(buyer, order["id"], key=key).status_code == 200
    # Another flow's row: not compared, answered as before (not a 422).
    assert shop.pay(buyer, order["id"], key=key).status_code != 422


@_needs_no_row_locks
def test_checkout_racing_a_twin_with_another_body_answers_422(shop, monkeypatch):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    real_guard = orders_module._guard_currency_matches_order

    def _twin_records_its_attempt_first(requested_currency, order_row):
        _insert_from_another_session(Payment(
            order_id=order_row.id, provider="stripe", payment_intent_id=f"{orders_module._PENDING_CHECKOUT_ID_PREFIX}twin",
            idempotency_key=key, idempotency_fingerprint="0" * 64,
            amount=1999, currency="USD", status="processing",
        ))
        return real_guard(requested_currency, order_row)

    monkeypatch.setattr(
        orders_module, "_guard_currency_matches_order", _twin_records_its_attempt_first
    )

    response = shop.checkout_session(buyer, order["id"], key=key)

    assert response.status_code == 422
    assert len(shop.payments(order["id"])) == 1


@pytest.mark.skipif(not IS_POSTGRES, reason="two real concurrent requests need row locks")
def test_a_duplicate_arriving_while_the_first_checkout_request_is_in_flight_answers_409(
    shop, monkeypatch
):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    in_provider = threading.Event()
    release = threading.Event()
    real = StripePaymentProvider.create_checkout_session

    def _slow(self, *args, **kwargs):
        in_provider.set()
        assert release.wait(timeout=20)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(StripePaymentProvider, "create_checkout_session", _slow)
    first = {}
    worker = threading.Thread(
        target=lambda: first.update(response=shop.checkout_session(buyer, order["id"], key=key))
    )
    worker.start()
    try:
        assert in_provider.wait(timeout=20)
        duplicate = shop.checkout_session(buyer, order["id"], key=key)
    finally:
        release.set()
        worker.join(timeout=30)

    assert duplicate.status_code == 409
    assert first["response"].status_code == 200
    assert len(shop.payments(order["id"])) == 1
