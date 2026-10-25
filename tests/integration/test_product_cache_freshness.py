"""GET /products/{id} is cached in Redis, so everything that changes what the
detail shows must drop the entry once the change is committed. The detail
carries Product.stock_quantity, which an order takes, a cancellation or the
unpaid-order expiry gives back, and an admin edit sets. (It embeds no variant
and no category beyond category_name, and variants keep their own stock.)
Before this, only admin edits dropped the entry: the detail and its ETag
showed the old stock for up to an hour while the list showed the new one.
"""
import fakeredis
import pytest
from redis import ConnectionError as RedisConnectionError
from test_order_expiry import _age, _cancel, _place
from test_orders import ADMIN_PREFIX, ORDERS_PREFIX, _auth_headers, _login, _make_admin

from app import tasks
from app.core.config import settings
from app.services import redis_codec as codec


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr("app.api.v1.orders.send_order_confirmation_email", lambda *a, **k: True)
    monkeypatch.setattr("app.api.v1.orders.send_order_status_update_email", lambda *a, **k: True)
    monkeypatch.setattr("app.api.v1.orders.send_order_cancellation_email", lambda *a, **k: True)
    monkeypatch.setattr("app.tasks.send_order_cancellation_email", lambda *a, **k: True)


def _detail(client, product):
    return client.get(f"/api/v1/products/{product.id}")


def _stock_in_detail(client, product):
    return _detail(client, product).json()["stock_quantity"]


def test_placing_an_order_updates_a_primed_detail(client, db_session):
    from test_orders import (
        CART_PREFIX,
        _checkout_ready_buyer,
        _make_category,
        _make_product,
        _with_idempotency_key,
    )

    token, address_id = _checkout_ready_buyer(client, db_session)
    product = _make_product(db_session, _make_category(db_session).id, stock=10)
    assert _stock_in_detail(client, product) == 10  # primes the cache
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 3},
        headers=_auth_headers(token),
    )
    placed = client.post(
        ORDERS_PREFIX,
        json={"shipping_address_id": address_id},
        headers=_with_idempotency_key(_auth_headers(token)),
    )
    assert placed.status_code == 201, placed.text

    listed = client.get("/api/v1/products", params={"category_id": product.category_id}).json()
    assert [i["stock_quantity"] for i in listed["items"]] == [7]
    assert _stock_in_detail(client, product) == 7


def test_customer_cancel_restores_the_cached_detail(client, db_session):
    token, order, product, _ = _place(client, db_session, quantity=3)
    assert _stock_in_detail(client, product) == 7  # primes the cache

    assert _cancel(client, token, order["id"]).status_code == 200

    assert _stock_in_detail(client, product) == 10


def test_admin_cancel_restores_the_cached_detail(client, db_session):
    _, order, product, _ = _place(client, db_session, quantity=3)
    admin = _make_admin(db_session)
    admin_token = _login(client, admin.email, "AdminPass1")
    assert _stock_in_detail(client, product) == 7

    response = client.put(
        f"{ORDERS_PREFIX}/{order['id']}/status",
        json={"status": "cancelled"},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 200, response.text
    assert _stock_in_detail(client, product) == 10


def test_expiry_restores_the_cached_detail(client, db_session, tasks_db):
    _, order, product, _ = _place(client, db_session, quantity=3)
    assert _stock_in_detail(client, product) == 7
    _age(db_session, order["id"], settings.UNPAID_ORDER_EXPIRE_MINUTES + 1)

    assert tasks.cancel_expired_unpaid_orders() == 1

    assert _stock_in_detail(client, product) == 10


def test_a_variant_order_leaves_the_product_detail_alone(client, db_session):
    # The variant has its own stock; the detail shows the product's.
    _, _, product, _ = _place(client, db_session, quantity=1, variant_quantity=2)
    assert _stock_in_detail(client, product) == 9


def test_etag_follows_the_stock(client, db_session):
    token, order, product, _ = _place(client, db_session, quantity=3)
    before = _detail(client, product).headers["etag"]

    assert _cancel(client, token, order["id"]).status_code == 200

    after = _detail(client, product).headers["etag"]
    assert after != before
    assert client.get(
        f"/api/v1/products/{product.id}", headers={"If-None-Match": before}
    ).status_code == 200


def test_cached_and_database_paths_give_the_same_body(client, db_session, fake_redis):
    token, order, product, _ = _place(client, db_session, quantity=3)
    _detail(client, product)  # primes
    assert _cancel(client, token, order["id"]).status_code == 200
    _detail(client, product)  # primes again, with the fresh value
    cached = _detail(client, product)
    fake_redis.flushall()
    from_db = _detail(client, product)

    assert cached.json() == from_db.json()
    assert cached.headers["etag"] == from_db.headers["etag"]


def test_a_failing_invalidation_does_not_fail_the_request(client, db_session, monkeypatch):
    token, order, product, _ = _place(client, db_session, quantity=3)
    _detail(client, product)

    async def _down(self, *keys):
        raise RedisConnectionError("down")

    monkeypatch.setattr(fakeredis.FakeAsyncRedis, "delete", _down)

    # The order is committed: Redis being down must not turn that into an error.
    assert _cancel(client, token, order["id"]).status_code == 200


def test_the_entry_cannot_outlive_a_lost_invalidation_for_long(client, db_session, fake_redis):
    _, _, product, _ = _place(client, db_session, quantity=3)
    _detail(client, product)

    ttl = fake_redis.ttl(f"product:{product.id}")

    assert 0 < ttl <= 300
    assert codec.PRODUCT_CACHE_TTL.total_seconds() == 300


def test_admin_edit_still_refreshes_the_detail(client, db_session):
    _, _, product, _ = _place(client, db_session, quantity=3)
    admin = _make_admin(db_session)
    admin_token = _login(client, admin.email, "AdminPass1")
    assert _stock_in_detail(client, product) == 7

    response = client.patch(
        f"{ADMIN_PREFIX}/products/{product.id}",
        json={"stock_quantity": 50},
        headers={**_auth_headers(admin_token), "Content-Type": "application/merge-patch+json"},
    )

    assert response.status_code == 200, response.text
    assert _stock_in_detail(client, product) == 50
