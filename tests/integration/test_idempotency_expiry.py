"""
An Idempotency-Key expires after IDEMPOTENCY_KEY_TTL_HOURS
(draft-ietf-httpapi-idempotency-key-header-08, section 2.3: "The resource
SHOULD define such expiration policy and publish it in the documentation").

Inside the TTL a key replays (same request) or is answered 422 (another
request) as before. After it, the daily sweep clears the key and fingerprint
of orders and of settled payment attempts (succeeded, failed) so the same
value is a new key again; the business rows stay. An attempt still in flight
or of unknown outcome keeps its key at any age: the safe retry of an
"unknown" attempt (3.2.1) needs it.
"""
import logging
from datetime import timedelta

import pytest
from payment_support import PAY_3DS, PAY_DECLINED, Outbox, Shop, new_key, reset_provider

from app import tasks
from app.core.clock import utc_now
from app.core.config import settings
from app.core.idempotency import REUSED_KEY_DETAIL
from app.models.order import Order
from app.models.payment import Payment

TTL = timedelta(hours=settings.IDEMPOTENCY_KEY_TTL_HOURS)


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def shop(client, db_session, monkeypatch, tasks_db):
    Outbox(monkeypatch)
    return Shop(client, db_session)


def _age(shop, *rows, by):
    for row in rows:
        row.created_at = utc_now() - by
    shop.db.commit()


def _fresh(shop, row):
    shop.db.expire_all()
    return shop.db.get(type(row), row.id)


def test_the_ttl_defaults_to_24_hours():
    assert settings.IDEMPOTENCY_KEY_TTL_HOURS == 24


def test_beat_entry_and_queue():
    entry = tasks.celery.conf.beat_schedule["cleanup-expired-idempotency-keys"]
    assert entry["task"] == "cleanup_expired_idempotency_keys"
    assert entry["schedule"] == tasks.crontab(hour=3, minute=0)
    assert tasks.celery.tasks["cleanup_expired_idempotency_keys"].queue == "cleanup"


def test_inside_the_ttl_nothing_is_cleared_and_keys_still_replay(shop):
    product = shop.product(price="39.98")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    first = shop.pay(buyer, order["id"], key=key)
    _age(shop, *shop.db.query(Order).all(), *shop.db.query(Payment).all(),
         by=TTL - timedelta(minutes=5))

    assert tasks.cleanup_expired_idempotency_keys() == 0

    again = shop.pay(buyer, order["id"], key=key)
    assert again.json()["data"]["id"] == first.json()["data"]["id"]
    other = shop.pay(buyer, order["id"], token=PAY_3DS, key=key)
    assert other.status_code == 422
    assert other.json() == {"detail": REUSED_KEY_DETAIL}


def test_an_expired_order_key_creates_a_new_order(shop):
    product = shop.product(stock=5)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product, 1).status_code == 200
    key = new_key()
    first = shop.place_order(buyer, key=key).json()["data"]["id"]
    old = shop.db.get(Order, first)
    _age(shop, old, by=TTL + timedelta(minutes=1))
    updated = old.updated_at

    assert tasks.cleanup_expired_idempotency_keys() == 1

    old = _fresh(shop, old)
    assert old.idempotency_key is None and old.idempotency_fingerprint is None
    assert old.updated_at == updated
    assert shop.add_to_cart(buyer, product, 1).status_code == 200
    second = shop.place_order(buyer, key=key)
    assert second.status_code == 200
    assert second.json()["data"]["id"] != first
    assert shop.db.query(Order).count() == 2


def test_an_expired_key_is_no_longer_a_422_for_another_body(shop):
    product = shop.product(price="39.98")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    assert shop.pay(buyer, order["id"], key=key).status_code == 200
    _age(shop, *shop.db.query(Payment).all(), by=TTL + timedelta(hours=1))

    assert tasks.cleanup_expired_idempotency_keys() == 1

    payment = shop.db.query(Payment).one()
    assert payment.idempotency_key is None and payment.idempotency_fingerprint is None
    assert payment.status == "succeeded"
    # The order is paid: the new attempt is refused for that reason (neither a
    # 422 nor a replay), which shows the key was treated as new.
    again = shop.pay(buyer, order["id"], token=PAY_3DS, key=key)
    assert again.status_code not in (200, 422), again.text
    assert shop.db.query(Payment).count() == 1


def test_a_failed_attempt_key_is_freed_and_a_new_attempt_is_made(shop):
    product = shop.product(price="39.98")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    assert shop.pay(buyer, order["id"], token=PAY_DECLINED, key=key).status_code != 500
    failed = shop.db.query(Payment).one()
    assert failed.status == "failed"
    _age(shop, failed, by=TTL + timedelta(minutes=1))

    assert tasks.cleanup_expired_idempotency_keys() == 1
    # The provider prunes its own copy of the key too (Stripe: at least 24
    # hours old); the mock keeps it until it is reset.
    reset_provider()

    retry = shop.pay(buyer, order["id"], key=key)
    assert retry.status_code == 200, retry.text
    assert shop.db.query(Payment).count() == 2


@pytest.mark.parametrize("state", ["processing", "requires_action", "unknown"])
def test_an_attempt_not_settled_keeps_its_key_whatever_its_age(shop, state):
    product = shop.product(price="39.98")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    assert shop.pay(buyer, order["id"], key=key).status_code == 200
    payment = shop.db.query(Payment).one()
    payment.status = state
    _age(shop, payment, by=TTL * 30)

    assert tasks.cleanup_expired_idempotency_keys() == 0

    payment = _fresh(shop, payment)
    assert payment.idempotency_key == key
    assert payment.idempotency_fingerprint is not None


def test_the_task_is_idempotent_and_batched(shop, caplog):
    product = shop.product(stock=50)
    buyer = shop.buyer()
    orders = []
    for _ in range(5):
        assert shop.add_to_cart(buyer, product, 1).status_code == 200
        orders.append(shop.db.get(Order, shop.place_order(buyer).json()["data"]["id"]))
    _age(shop, *orders, by=TTL + timedelta(hours=2))

    with caplog.at_level(logging.INFO, logger="app.tasks"):
        assert tasks.clear_expired_idempotency_keys(shop.db, batch_size=2) == 5
    assert any("cleared 5 expired" in r.getMessage() for r in caplog.records)
    assert tasks.clear_expired_idempotency_keys(shop.db, batch_size=2) == 0
    assert tasks.cleanup_expired_idempotency_keys() == 0
    assert shop.db.query(Order).filter(Order.idempotency_key.isnot(None)).count() == 0
    assert shop.db.query(Order).count() == 5


def test_a_row_without_a_key_is_left_alone(shop):
    product = shop.product(stock=5)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    row = shop.db.get(Order, order["id"])
    row.idempotency_key = None
    row.idempotency_fingerprint = None
    _age(shop, row, by=TTL * 3)

    assert tasks.cleanup_expired_idempotency_keys() == 0


def test_the_key_header_documents_the_expiry(client):
    spec = client.get("/api/v1/openapi.json").json()
    hours = settings.IDEMPOTENCY_KEY_TTL_HOURS
    for path in ("/api/v1/orders", "/api/v1/orders/{order_id}/pay",
                 "/api/v1/orders/{order_id}/checkout-session",
                 "/api/v1/orders/{order_id}/refund"):
        parameters = spec["paths"][path]["post"]["parameters"]
        header = next(p for p in parameters if p["name"] == "Idempotency-Key")
        assert f"{hours} hours" in header["description"], path
