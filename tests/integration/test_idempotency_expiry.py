"""
An Idempotency-Key expires after IDEMPOTENCY_KEY_TTL_HOURS
(draft-ietf-httpapi-idempotency-key-header-08, section 2.3: "The resource
SHOULD define such expiration policy and publish it in the documentation").

Inside the TTL a key replays (same request) or is answered 422 (another
request) as before. After it, the daily sweep clears the key and fingerprint
of ORDERS so the same value is a new key again; the rows stay. The key of a
payment attempt (pay, checkout) is forwarded to Stripe, which may keep it
longer than 24 hours, so it is never cleared, whatever the status or age.
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
    assert again.json()["id"] == first.json()["id"]
    other = shop.pay(buyer, order["id"], token=PAY_3DS, key=key)
    assert other.status_code == 422
    assert other.json()["detail"] == REUSED_KEY_DETAIL


def test_an_expired_order_key_creates_a_new_order(shop):
    product = shop.product(stock=5)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product, 1).status_code == 200
    key = new_key()
    first = shop.place_order(buyer, key=key).json()["id"]
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
    assert second.json()["id"] != first
    assert shop.db.query(Order).count() == 2


@pytest.mark.parametrize("state", ["succeeded", "failed", "processing",
                                   "requires_action", "unknown"])
def test_a_payment_attempt_keeps_its_key_whatever_its_status_and_age(shop, state):
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


def test_a_reused_pay_key_after_the_sweep_replays_never_500(shop):
    # Before the fix the sweep freed the key of a settled attempt; the reuse
    # made a new local attempt, the provider replayed the old PaymentIntent
    # and the unique payment_intent_id answered 500.
    product = shop.product(price="39.98")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    first = shop.pay(buyer, order["id"], key=key)
    assert first.status_code == 200
    _age(shop, *shop.db.query(Order).all(), *shop.db.query(Payment).all(),
         by=TTL + timedelta(days=3))

    tasks.cleanup_expired_idempotency_keys()

    again = shop.pay(buyer, order["id"], key=key)
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]
    other = shop.pay(buyer, order["id"], token=PAY_3DS, key=key)
    assert other.status_code == 422
    assert shop.db.query(Payment).count() == 1


def test_a_reused_failed_pay_key_after_the_sweep_answers_as_before(shop):
    product = shop.product(price="39.98")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    first = shop.pay(buyer, order["id"], token=PAY_DECLINED, key=key)
    assert first.status_code != 500
    before = shop.pay(buyer, order["id"], token=PAY_DECLINED, key=key)
    _age(shop, *shop.db.query(Payment).all(), by=TTL + timedelta(days=3))

    tasks.cleanup_expired_idempotency_keys()

    again = shop.pay(buyer, order["id"], token=PAY_DECLINED, key=key)
    assert again.status_code == before.status_code != 500, again.text
    assert again.json()["id"] == before.json()["id"]
    assert shop.db.query(Payment).count() == 1


def test_a_reused_checkout_key_after_the_sweep_replays_never_500(shop):
    product = shop.product(price="39.98")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    first = shop.checkout_session(buyer, order["id"], key=key)
    assert first.status_code == 200, first.text
    _age(shop, *shop.db.query(Order).all(), *shop.db.query(Payment).all(),
         by=TTL + timedelta(days=3))

    tasks.cleanup_expired_idempotency_keys()

    again = shop.checkout_session(buyer, order["id"], key=key)
    assert again.status_code == 200, again.text
    for field in ("payment_id", "checkout_session_id", "url"):
        assert again.json()[field] == first.json()[field]
    assert shop.db.query(Payment).count() == 1


def test_the_task_is_idempotent_and_batched(shop, caplog):
    product = shop.product(stock=50)
    buyer = shop.buyer()
    orders = []
    for _ in range(5):
        assert shop.add_to_cart(buyer, product, 1).status_code == 200
        orders.append(shop.db.get(Order, shop.place_order(buyer).json()["id"]))
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


def test_the_key_header_documents_the_policy_of_each_endpoint(client):
    spec = client.get("/api/v1/openapi.json").json()
    hours = settings.IDEMPOTENCY_KEY_TTL_HOURS

    def description(path):
        parameters = spec["paths"][path]["post"]["parameters"]
        return next(p for p in parameters if p["name"] == "Idempotency-Key")["description"]

    assert f"{hours} hours" in description("/api/v1/orders")
    for path in ("/api/v1/orders/{order_id}/pay",
                 "/api/v1/orders/{order_id}/checkout-session"):
        text = description(path)
        assert "does not expire" in text and "hours" not in text, path
    assert "provider" in description("/api/v1/orders/{order_id}/refund")
