import json
import threading
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from redis import ConnectionError as RedisConnectionError
from sqlalchemy import event

import app.core.database as database_module
from app.core.config import Settings, settings
from app.models.category import Category
from app.models.product import Product, ProductVariant
from app.models.user import User
from app.schemas.cart import CART_KEY_PREFIX
from app.services.redis import RedisService

USERS_PREFIX = "/api/v1/users"
CART_PREFIX = "/api/v1/cart"


def _valid_registration_payload():
    return {
        "email": "cart.user@example.com",
        "password": "Str0ngPass1",
        "full_name": "Cart User",
        "gdpr_consent": True,
        "privacy_policy_accepted": True,
        "marketing_consent": False,
    }


def _register_and_login(client, payload):
    client.post(f"{USERS_PREFIX}/register", json=payload)
    login_response = client.post(
        f"{USERS_PREFIX}/login",
        data={"username": payload["email"], "password": payload["password"]},
    )
    return login_response.json()["data"]["access_token"]


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _make_category(db_session):
    category = Category(name="Electronics", parent_id=None, level=0, path=[])
    db_session.add(category)
    db_session.commit()
    db_session.refresh(category)
    return category


def _make_product(db_session, category_id, stock=10, price="9.99"):
    product = Product(
        name="Widget",
        description="A fine widget for all your widget needs",
        price=price,
        stock_quantity=stock,
        images=["https://example.com/widget.png"],
        characteristics={},
        category_id=category_id,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def _make_variant(db_session, product_id, sku="WIDGET-M-RED", stock=5, price_override=None, is_active=True):
    variant = ProductVariant(
        product_id=product_id,
        sku=sku,
        attributes={"size": "M", "color": "red"},
        price_override=price_override,
        stock_quantity=stock,
        is_active=is_active,
    )
    db_session.add(variant)
    db_session.commit()
    db_session.refresh(variant)
    return variant


def test_get_cart_starts_empty(client, valid_registration_payload):
    token = _register_and_login(client, valid_registration_payload)

    response = client.get(CART_PREFIX, headers=_auth_headers(token))

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == []
    assert body["items_count"] == 0


def test_add_item_to_cart(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 2},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items_count"] == 1
    assert body["items"][0]["quantity"] == 2


def test_add_item_rejects_unknown_product(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": 999999, "quantity": 1},
        headers=_auth_headers(token),
    )

    assert response.status_code == 404


def test_add_item_rejects_quantity_over_stock(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=2)

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 3},
        headers=_auth_headers(token),
    )

    assert response.status_code == 400


def test_update_cart_item_quantity(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    response = client.put(
        f"{CART_PREFIX}/items/{product.id}",
        params={"quantity": 5},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["quantity"] == 5


def test_update_cart_item_rejects_quantity_over_stock(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=3)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    response = client.put(
        f"{CART_PREFIX}/items/{product.id}",
        params={"quantity": 10},
        headers=_auth_headers(token),
    )

    assert response.status_code == 400


def test_update_item_not_in_cart_returns_404(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)

    response = client.put(
        f"{CART_PREFIX}/items/{product.id}",
        params={"quantity": 1},
        headers=_auth_headers(token),
    )

    assert response.status_code == 404


def test_remove_item_from_cart(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    response = client.delete(
        f"{CART_PREFIX}/items/{product.id}", headers=_auth_headers(token)
    )

    assert response.status_code == 200
    assert response.json()["data"]["items"] == []


def test_remove_item_not_in_cart_returns_404(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())

    response = client.delete(f"{CART_PREFIX}/items/999999", headers=_auth_headers(token))

    assert response.status_code == 404


def test_clear_cart(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    response = client.delete(CART_PREFIX, headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["data"]["items"] == []


def test_validate_cart_reports_no_issues_when_stock_sufficient(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 2},
        headers=_auth_headers(token),
    )

    response = client.post(f"{CART_PREFIX}/validate", headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["data"] == {"valid": True, "issues": [], "price_changes": []}


def test_validate_cart_reports_issue_when_stock_reduced_after_adding(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=5)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 3},
        headers=_auth_headers(token),
    )

    # Stock drops below what's in the cart after the item was added.
    db_session.query(Product).filter(Product.id == product.id).update(
        {"stock_quantity": 1}
    )
    db_session.commit()

    response = client.post(f"{CART_PREFIX}/validate", headers=_auth_headers(token))

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["valid"] is False
    assert body["issues"][0]["product_id"] == product.id


def test_cart_without_authorization_is_an_empty_guest_cart(client, fake_redis):
    """Was test_cart_requires_authentication, which expected a 401 here. A
    request without an Authorization header is now a guest's: with no guest
    cart cookie that is an empty cart, nothing stored and no cookie set.
    Guest carts are covered in tests/integration/test_guest_cart.py."""
    response = client.get(CART_PREFIX)

    assert response.status_code == 200
    assert response.json()["data"]["items"] == []
    assert "set-cookie" not in response.headers
    assert list(fake_redis.scan_iter("*cart:*")) == []


def test_cart_with_an_invalid_token_is_a_401_not_a_guest_cart(client):
    """What the old test protected, where it still applies: a caller who
    claims to be an account and is not gets a 401 on every cart route."""
    headers = _auth_headers("not-a-valid-token")

    responses = [
        client.get(CART_PREFIX, headers=headers),
        client.post(f"{CART_PREFIX}/items", params={"product_id": 1}, headers=headers),
        client.put(f"{CART_PREFIX}/items/1", params={"quantity": 1}, headers=headers),
        client.delete(f"{CART_PREFIX}/items/1", headers=headers),
        client.delete(CART_PREFIX, headers=headers),
        client.post(f"{CART_PREFIX}/validate", headers=headers),
    ]

    assert [response.status_code for response in responses] == [401] * 6
    assert responses[0].json()["detail"] == "Could not validate credentials"


def test_account_cart_requests_never_set_a_cookie(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    headers = _auth_headers(token)

    responses = [
        client.post(f"{CART_PREFIX}/items", params={"product_id": product.id}, headers=headers),
        client.put(f"{CART_PREFIX}/items/{product.id}", params={"quantity": 2}, headers=headers),
        client.get(CART_PREFIX, headers=headers),
        client.delete(f"{CART_PREFIX}/items/{product.id}", headers=headers),
        client.delete(CART_PREFIX, headers=headers),
    ]

    assert [response.status_code for response in responses] == [200] * 5
    assert all("set-cookie" not in response.headers for response in responses)


def test_an_emptied_account_cart_stays_stored(client, db_session, fake_redis):
    """Only a guest's cart is deleted when its last line goes; an account's
    is kept, empty, as before."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    headers = _auth_headers(token)
    client.post(f"{CART_PREFIX}/items", params={"product_id": product.id}, headers=headers)

    client.delete(f"{CART_PREFIX}/items/{product.id}", headers=headers)

    (cart_key,) = fake_redis.scan_iter(f"{CART_KEY_PREFIX}*")
    assert json.loads(fake_redis.get(cart_key))["items"] == {}
    client.delete(CART_PREFIX, headers=headers)
    assert json.loads(fake_redis.get(cart_key))["items"] == {}


def test_clearing_an_account_cart_that_does_not_exist_stores_nothing(client, fake_redis):
    """DELETE /cart on an account with no cart used to write an empty one.
    The answer is the same empty cart; there is just nothing to store."""
    token = _register_and_login(client, _valid_registration_payload())

    response = client.delete(CART_PREFIX, headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["data"]["items"] == []
    assert list(fake_redis.scan_iter(f"{CART_KEY_PREFIX}*")) == []


# ---------------------------------------------------------------------------
# Concurrency: rapid add-to-cart for the same product
# ---------------------------------------------------------------------------

def test_sequential_add_to_cart_accumulates_quantity(client, db_session):
    """Two add-to-cart calls for the same product, one after another,
    must accumulate (2 + 3 = 5), not overwrite. This proves add_to_cart's
    read-modify-write logic is correct when calls do not overlap - it does
    NOT by itself prove safety under genuine concurrent requests; see
    test_concurrent_add_to_cart_can_lose_updates below for that."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)

    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 2},
        headers=_auth_headers(token),
    )
    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 3},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items_count"] == 1
    assert body["items"][0]["quantity"] == 5


def test_concurrent_add_to_cart_can_lose_updates(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=100)

    n = 10
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        response = client.post(
            f"{CART_PREFIX}/items",
            params={"product_id": product.id, "quantity": 1},
            headers=_auth_headers(token),
        )
        results[i] = response.status_code

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(code == 200 for code in results)

    final = client.get(CART_PREFIX, headers=_auth_headers(token)).json()["data"]
    final_quantity = final["items"][0]["quantity"] if final["items"] else 0

    # Desired behavior: all 10 concurrent +1 additions accumulate to 10.
    # Currently fails: last writer wins, earlier additions are lost.
    assert final_quantity == n


def test_high_concurrency_add_to_cart_sums_exactly(client, db_session):
    """Same lost-update scenario as above but at higher concurrency (40
    threads) to make sure the WATCH/MULTI retry loop in
    RedisService._atomic_mutate_cart holds up under heavier contention,
    not just enough to beat a handful of threads."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=1000)

    n = 40
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        response = client.post(
            f"{CART_PREFIX}/items",
            params={"product_id": product.id, "quantity": 1},
            headers=_auth_headers(token),
        )
        results[i] = response.status_code

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(code == 200 for code in results)

    final = client.get(CART_PREFIX, headers=_auth_headers(token)).json()["data"]
    final_quantity = final["items"][0]["quantity"] if final["items"] else 0
    assert final_quantity == n


def test_concurrent_add_different_products_no_cross_clobbering(client, db_session):
    """Concurrent adds of two different products to the same cart must
    both land with correct quantities - the WATCH/MULTI retry loop must
    not let one product's write stomp the other's."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product_a = _make_product(db_session, category.id, stock=100)
    product_b = _make_product(db_session, category.id, stock=100)

    n = 10
    barrier = threading.Barrier(n * 2)
    results = [None] * (n * 2)

    def worker(i, product_id):
        barrier.wait()
        response = client.post(
            f"{CART_PREFIX}/items",
            params={"product_id": product_id, "quantity": 1},
            headers=_auth_headers(token),
        )
        results[i] = response.status_code

    threads = []
    for i in range(n):
        threads.append(threading.Thread(target=worker, args=(i, product_a.id)))
    for i in range(n):
        threads.append(threading.Thread(target=worker, args=(n + i, product_b.id)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(code == 200 for code in results)

    final = client.get(CART_PREFIX, headers=_auth_headers(token)).json()["data"]
    quantities = {item["product_id"]: item["quantity"] for item in final["items"]}
    assert quantities == {product_a.id: n, product_b.id: n}


def test_concurrent_add_and_remove_same_item_is_consistent(client, db_session):
    """A concurrent add and remove of the same cart item must not crash
    (e.g. KeyError from a mutator running against a stale copy) and must
    leave the cart in a valid state: either the item is gone, or it's
    present with a positive quantity - never a phantom zero-quantity
    entry or an unhandled exception."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=100)

    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 5},
        headers=_auth_headers(token),
    )

    n = 8
    barrier = threading.Barrier(n)
    results = [None] * n

    def add_worker(i):
        barrier.wait()
        response = client.post(
            f"{CART_PREFIX}/items",
            params={"product_id": product.id, "quantity": 1},
            headers=_auth_headers(token),
        )
        results[i] = response.status_code

    def remove_worker(i):
        barrier.wait()
        response = client.delete(
            f"{CART_PREFIX}/items/{product.id}", headers=_auth_headers(token)
        )
        results[i] = response.status_code

    threads = [threading.Thread(target=add_worker, args=(i,)) for i in range(n // 2)]
    threads += [
        threading.Thread(target=remove_worker, args=(n // 2 + i,))
        for i in range(n // 2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every add succeeds (200); a remove either succeeds (200, it was
    # first) or 404s (the item was already gone) - never anything else.
    assert all(code in (200, 404) for code in results)

    final = client.get(CART_PREFIX, headers=_auth_headers(token)).json()["data"]
    if final["items"]:
        assert len(final["items"]) == 1
        assert final["items"][0]["product_id"] == product.id
        assert final["items"][0]["quantity"] > 0


def test_concurrent_add_and_quantity_update_same_item(client, db_session):
    """A concurrent add (+3) and a quantity-update (set to 10) of the same
    item must serialize through the WATCH/MULTI guard into one of the two
    valid outcomes for their real execution order - never a lost update
    (e.g. collapsing back to the pre-existing quantity of 5) and never a
    crash."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=100)

    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 5},
        headers=_auth_headers(token),
    )

    barrier = threading.Barrier(2)
    results = [None, None]

    def add_worker():
        barrier.wait()
        response = client.post(
            f"{CART_PREFIX}/items",
            params={"product_id": product.id, "quantity": 3},
            headers=_auth_headers(token),
        )
        results[0] = response.status_code

    def update_worker():
        barrier.wait()
        response = client.put(
            f"{CART_PREFIX}/items/{product.id}",
            params={"quantity": 10},
            headers=_auth_headers(token),
        )
        results[1] = response.status_code

    threads = [threading.Thread(target=add_worker), threading.Thread(target=update_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(code == 200 for code in results)

    final = client.get(CART_PREFIX, headers=_auth_headers(token)).json()["data"]
    final_quantity = final["items"][0]["quantity"]
    # update-wins-last -> 10; add-wins-last (applied on top of the update's
    # 10) -> 13. Either is a valid serialization; 5 (update lost) or 8
    # (add lost, update never applied) would mean an update was dropped.
    assert final_quantity in (10, 13)


def test_stock_limit_enforced_under_concurrent_adds(client, db_session):
    """20 concurrent +1 adds against a product with only 10 units of
    stock must not collectively oversell it: exactly 10 requests succeed
    and the rest are rejected with 400, because the stock check inside
    the atomic mutator always runs against the freshest cart quantity on
    every WATCH retry."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)

    n = 20
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        response = client.post(
            f"{CART_PREFIX}/items",
            params={"product_id": product.id, "quantity": 1},
            headers=_auth_headers(token),
        )
        results[i] = response.status_code

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(code in (200, 400) for code in results)
    succeeded = sum(1 for code in results if code == 200)
    assert succeeded == 10

    final = client.get(CART_PREFIX, headers=_auth_headers(token)).json()["data"]
    final_quantity = final["items"][0]["quantity"] if final["items"] else 0
    assert final_quantity == 10


def test_cart_ttl_refreshed_after_concurrent_writes(client, db_session, fake_redis):
    """The cart key's TTL must still be (re)set correctly after the
    WATCH/MULTI-guarded concurrent writes, matching the existing
    REDIS_CART_TTL_DAYS behavior of the old plain SETEX path."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=100)

    n = 10
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()
        client.post(
            f"{CART_PREFIX}/items",
            params={"product_id": product.id, "quantity": 1},
            headers=_auth_headers(token),
        )

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Find the user id from the cart key namespace by scanning - there's
    # only one cart in this test's fake Redis instance.
    cart_keys = list(fake_redis.scan_iter(f"{CART_KEY_PREFIX}*"))
    assert len(cart_keys) == 1

    ttl_seconds = fake_redis.ttl(cart_keys[0])
    assert ttl_seconds > 0
    assert ttl_seconds <= settings.REDIS_CART_TTL_DAYS * 24 * 3600


# ---------------------------------------------------------------------------
# Product variants in the cart
# ---------------------------------------------------------------------------

def test_add_variant_to_cart_prices_and_stocks_against_variant(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10, price="9.99")
    variant = _make_variant(db_session, product.id, stock=3, price_override="14.50")

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant.id, "quantity": 2},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items_count"] == 1
    item = body["items"][0]
    assert item["product_id"] == product.id
    assert item["variant_id"] == variant.id
    assert item["quantity"] == 2
    # Variant's price_override (14.50) is used, not the product's base price.
    assert item["unit_price"] == "14.50"
    assert item["line_total"] == "29.00"
    assert body["total_amount"] == "29.00"


def test_add_variant_without_price_override_uses_product_price(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10, price="9.99")
    variant = _make_variant(db_session, product.id, stock=3, price_override=None)

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["unit_price"] == "9.99"


def test_add_variant_rejects_quantity_over_variant_stock(client, db_session):
    """The variant's own stock (3) is the limit, even though the parent
    product has plenty of stock (100) - checking the wrong counter would
    let this through."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=100, price="9.99")
    variant = _make_variant(db_session, product.id, stock=3)

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant.id, "quantity": 4},
        headers=_auth_headers(token),
    )

    assert response.status_code == 400


def test_add_variant_rejects_id_belonging_to_different_product(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product_a = _make_product(db_session, category.id)
    product_b = _make_product(db_session, category.id)
    variant_of_b = _make_variant(db_session, product_b.id, sku="B-VARIANT")

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product_a.id, "variant_id": variant_of_b.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    assert response.status_code == 404


def test_add_variant_rejects_inactive_variant(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    variant = _make_variant(db_session, product.id, is_active=False)

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    assert response.status_code == 404


def test_add_variant_rejects_unknown_variant_id(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": 999999, "quantity": 1},
        headers=_auth_headers(token),
    )

    assert response.status_code == 404


def test_different_variants_of_same_product_are_distinct_cart_lines(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10, price="9.99")
    variant_red = _make_variant(db_session, product.id, sku="WIDGET-RED", stock=5)
    variant_blue = _make_variant(db_session, product.id, sku="WIDGET-BLUE", stock=5)

    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant_red.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant_blue.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    # And the plain product itself (no variant) is a third, separate line.
    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items_count"] == 3
    variant_ids = {item["variant_id"] for item in body["items"]}
    assert variant_ids == {variant_red.id, variant_blue.id, None}


def test_same_variant_added_twice_merges_quantity(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10, price="9.99")
    variant = _make_variant(db_session, product.id, stock=10)

    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant.id, "quantity": 2},
        headers=_auth_headers(token),
    )
    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant.id, "quantity": 3},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items_count"] == 1
    assert body["items"][0]["quantity"] == 5


def test_remove_variant_line_leaves_plain_product_line_untouched(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10, price="9.99")
    variant = _make_variant(db_session, product.id, stock=5)

    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    response = client.delete(
        f"{CART_PREFIX}/items/{product.id}",
        params={"variant_id": variant.id},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items_count"] == 1
    assert body["items"][0]["variant_id"] is None


# ---------------------------------------------------------------------------
# Regression tests for the cart review findings
# (scripts/loadtest/results/cart-review.md, findings A-H)
# ---------------------------------------------------------------------------

def _add(client, token, product_id, quantity, variant_id=None):
    params = {"product_id": product_id, "quantity": quantity}
    if variant_id is not None:
        params["variant_id"] = variant_id
    return client.post(f"{CART_PREFIX}/items", params=params, headers=_auth_headers(token))


def _cart(client, token):
    return client.get(CART_PREFIX, headers=_auth_headers(token)).json()["data"]


# --- A: non-positive quantity -----------------------------------------------

@pytest.mark.parametrize("quantity", [0, -1])
def test_add_item_rejects_non_positive_quantity(client, db_session, quantity):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)

    response = _add(client, token, product.id, quantity)

    assert response.status_code == 422
    assert _cart(client, token)["items"] == []


@pytest.mark.parametrize("quantity", [0, -3, -5])
def test_add_non_positive_quantity_leaves_existing_line_intact(client, db_session, quantity):
    """Adding 0, or a negative amount that takes the line to zero or below,
    used to be stored; the stored cart then failed CartItem validation on
    the next read and the whole cart, other lines included, came back
    empty."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    other = _make_product(db_session, category.id)
    _add(client, token, product.id, 3)
    _add(client, token, other.id, 1)

    response = _add(client, token, product.id, quantity)

    assert response.status_code == 422
    quantities = {item["product_id"]: item["quantity"] for item in _cart(client, token)["items"]}
    assert quantities == {product.id: 3, other.id: 1}


@pytest.mark.parametrize("quantity", [0, -2])
def test_update_cart_item_rejects_non_positive_quantity(client, db_session, quantity):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    other = _make_product(db_session, category.id)
    _add(client, token, product.id, 3)
    _add(client, token, other.id, 1)

    response = client.put(
        f"{CART_PREFIX}/items/{product.id}",
        params={"quantity": quantity},
        headers=_auth_headers(token),
    )

    assert response.status_code == 422
    quantities = {item["product_id"]: item["quantity"] for item in _cart(client, token)["items"]}
    assert quantities == {product.id: 3, other.id: 1}


# --- B: Redis failure during a mutation -------------------------------------

def _break_cart_transaction(monkeypatch):
    def _raise(*args, **kwargs):
        raise RedisConnectionError("connection lost")
    monkeypatch.setattr(RedisService()._redis, "transaction", _raise)


def test_add_item_returns_503_when_redis_fails(client, db_session, monkeypatch):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    _break_cart_transaction(monkeypatch)

    response = _add(client, token, product.id, 1)

    assert response.status_code == 503


def test_update_item_returns_503_when_redis_fails(client, db_session, monkeypatch):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    _add(client, token, product.id, 1)
    _break_cart_transaction(monkeypatch)

    response = client.put(
        f"{CART_PREFIX}/items/{product.id}",
        params={"quantity": 2},
        headers=_auth_headers(token),
    )

    assert response.status_code == 503


def test_remove_item_returns_503_when_redis_fails(client, db_session, monkeypatch):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    _add(client, token, product.id, 1)
    _break_cart_transaction(monkeypatch)

    response = client.delete(f"{CART_PREFIX}/items/{product.id}", headers=_auth_headers(token))

    assert response.status_code == 503


def test_clear_cart_returns_503_when_redis_fails(client, db_session, monkeypatch):
    token = _register_and_login(client, _valid_registration_payload())
    _break_cart_transaction(monkeypatch)

    response = client.delete(CART_PREFIX, headers=_auth_headers(token))

    assert response.status_code == 503


# --- C: GET /cart must not write --------------------------------------------

def test_get_cart_does_not_persist_an_empty_cart(client, fake_redis):
    token = _register_and_login(client, _valid_registration_payload())

    response = client.get(CART_PREFIX, headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["data"]["items"] == []
    assert list(fake_redis.scan_iter(f"{CART_KEY_PREFIX}*")) == []


def test_validate_cart_does_not_persist_an_empty_cart(client, fake_redis):
    token = _register_and_login(client, _valid_registration_payload())

    response = client.post(f"{CART_PREFIX}/validate", headers=_auth_headers(token))

    assert response.status_code == 200
    assert list(fake_redis.scan_iter(f"{CART_KEY_PREFIX}*")) == []


def test_get_cart_does_not_erase_an_add_landing_during_the_read(client, db_session, monkeypatch):
    """The first-ever GET /cart read "no cart" and then wrote an empty one
    unconditionally; an add that landed between the read and that write was
    overwritten. The add is injected here right after the read returns."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)

    real_get_cart = RedisService.get_cart
    injected = []

    def _get_cart_then_concurrent_add(self, user_id):
        cart = real_get_cart(self, user_id)
        if not injected:
            injected.append(user_id)
            self.add_to_cart_atomic(
                user_id=user_id, product_id=product.id, quantity=2, price=Decimal("9.99"),
                name="Widget", image="", stock_quantity=10,
            )
        return cart

    monkeypatch.setattr(RedisService, "get_cart", _get_cart_then_concurrent_add)
    client.get(CART_PREFIX, headers=_auth_headers(token))
    monkeypatch.setattr(RedisService, "get_cart", real_get_cart)

    items = _cart(client, token)["items"]
    assert [(item["product_id"], item["quantity"]) for item in items] == [(product.id, 2)]


def test_get_cart_does_not_overwrite_an_unreadable_stored_cart(client, db_session, fake_redis):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    _add(client, token, product.id, 1)
    cart_key = next(fake_redis.scan_iter(f"{CART_KEY_PREFIX}*"))
    fake_redis.set(cart_key, "not-json-at-all{{{")

    response = client.get(CART_PREFIX, headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["data"]["items"] == []
    assert fake_redis.get(cart_key) == "not-json-at-all{{{"


# --- E: inactive products ---------------------------------------------------

def _deactivate(db_session, model, row_id):
    db_session.query(model).filter(model.id == row_id).update({"is_active": False})
    db_session.commit()


def test_add_item_rejects_inactive_product(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    _deactivate(db_session, Product, product.id)

    response = _add(client, token, product.id, 1)

    assert response.status_code == 404
    assert _cart(client, token)["items"] == []


def test_add_variant_rejects_inactive_parent_product(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    variant = _make_variant(db_session, product.id)
    _deactivate(db_session, Product, product.id)

    response = _add(client, token, product.id, 1, variant_id=variant.id)

    assert response.status_code == 404


def test_update_cart_item_rejects_product_deactivated_after_adding(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    _add(client, token, product.id, 1)
    _deactivate(db_session, Product, product.id)

    response = client.put(
        f"{CART_PREFIX}/items/{product.id}",
        params={"quantity": 4},
        headers=_auth_headers(token),
    )

    assert response.status_code == 404
    assert _cart(client, token)["items"][0]["quantity"] == 1


def test_validate_cart_reports_product_deactivated_after_adding(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    _add(client, token, product.id, 1)
    _deactivate(db_session, Product, product.id)

    body = client.post(f"{CART_PREFIX}/validate", headers=_auth_headers(token)).json()["data"]

    assert body["valid"] is False
    assert body["issues"] == [{
        "product_id": product.id,
        "variant_id": None,
        "code": "product_unavailable",
        "error": "Product no longer available",
        "available": None,
        "requested": None,
    }]


def test_validate_cart_reports_variant_deactivated_after_adding(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    variant = _make_variant(db_session, product.id)
    _add(client, token, product.id, 1, variant_id=variant.id)
    _deactivate(db_session, ProductVariant, variant.id)

    body = client.post(f"{CART_PREFIX}/validate", headers=_auth_headers(token)).json()["data"]

    assert body["valid"] is False
    assert body["issues"] == [{
        "product_id": product.id,
        "variant_id": variant.id,
        "code": "variant_unavailable",
        "error": "Product variant no longer available",
        "available": None,
        "requested": None,
    }]


def test_update_variant_line_rejects_variant_deactivated_after_adding(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    variant = _make_variant(db_session, product.id)
    _add(client, token, product.id, 1, variant_id=variant.id)

    params = {"quantity": 2, "variant_id": variant.id}
    still_active = client.put(
        f"{CART_PREFIX}/items/{product.id}", params=params, headers=_auth_headers(token)
    )
    _deactivate(db_session, ProductVariant, variant.id)
    deactivated = client.put(
        f"{CART_PREFIX}/items/{product.id}", params=params | {"quantity": 3}, headers=_auth_headers(token)
    )

    assert still_active.status_code == 200
    assert deactivated.status_code == 404
    assert _cart(client, token)["items"][0]["quantity"] == 2


# --- G: one TTL source, and an expires_at that tracks it --------------------

def test_cart_ttl_comes_from_settings(client, db_session, fake_redis, monkeypatch):
    """settings.REDIS_CART_TTL_DAYS was only used for the expires_at of a
    first-ever GET; the TTL actually applied was a separate hard-coded 7."""
    monkeypatch.setattr(settings, "REDIS_CART_TTL_DAYS", 3)
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)

    _add(client, token, product.id, 1)

    cart_key = next(fake_redis.scan_iter(f"{CART_KEY_PREFIX}*"))
    three_days = 3 * 24 * 3600
    assert three_days - 60 < fake_redis.ttl(cart_key) <= three_days


def test_cart_expires_at_follows_the_ttl_refreshed_by_a_mutation(client, db_session, fake_redis):
    """Every cart write re-arms the Redis TTL, but expires_at kept the value
    from the day the cart was created."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    _add(client, token, product.id, 1)
    cart_key = next(fake_redis.scan_iter(f"{CART_KEY_PREFIX}*"))

    # Age the stored cart: it now claims to expire in an hour.
    stored = json.loads(fake_redis.get(cart_key))
    stored["expires_at"] = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    fake_redis.set(cart_key, json.dumps(stored))

    _add(client, token, product.id, 1)

    expires_at = datetime.fromisoformat(json.loads(fake_redis.get(cart_key))["expires_at"])
    key_expiry = datetime.utcnow() + timedelta(seconds=fake_redis.ttl(cart_key))
    assert abs((expires_at - key_expiry).total_seconds()) < 60


# --- H: price drift between cart and order ----------------------------------
#
# These three used to assert that a changed price made the cart invalid
# ("Price changed" in `issues`, valid=false until the line was removed and
# re-added). The cart is now priced live, so a changed price can no longer
# make the order differ from what the cart shows: it is reported in
# `price_changes` and leaves `valid` alone.

def _set_price(db_session, product_id, price):
    db_session.query(Product).filter(Product.id == product_id).update({"price": price})
    db_session.commit()


def _validate(client, token):
    response = client.post(f"{CART_PREFIX}/validate", headers=_auth_headers(token))
    assert response.status_code == 200
    return response.json()["data"]


def test_validate_cart_reports_price_changed_since_adding(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="9.99")
    _add(client, token, product.id, 2)

    _set_price(db_session, product.id, "12.50")

    body = _validate(client, token)

    assert body["valid"] is True
    assert body["issues"] == []
    assert body["price_changes"] == [{
        "product_id": product.id,
        "variant_id": None,
        "price_when_added": "9.99",
        "current_price": "12.50",
    }]


def test_validate_cart_reports_variant_price_override_changed(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="9.99")
    variant = _make_variant(db_session, product.id, price_override="14.50")
    _add(client, token, product.id, 1, variant_id=variant.id)

    # Dropping the override makes the variant fall back to the product price.
    db_session.query(ProductVariant).filter(ProductVariant.id == variant.id).update(
        {"price_override": None}
    )
    db_session.commit()

    body = _validate(client, token)

    assert body["valid"] is True
    assert body["issues"] == []
    assert body["price_changes"] == [{
        "product_id": product.id,
        "variant_id": variant.id,
        "price_when_added": "14.50",
        "current_price": "9.99",
    }]


def test_validate_cart_reports_both_price_and_stock_issues(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=5, price="9.99")
    _add(client, token, product.id, 3)

    db_session.query(Product).filter(Product.id == product.id).update(
        {"price": "8.00", "stock_quantity": 1}
    )
    db_session.commit()

    body = _validate(client, token)

    # Only the stock makes it unorderable; the price is reported beside it.
    assert body["valid"] is False
    assert body["issues"] == [{
        "product_id": product.id,
        "variant_id": None,
        "code": "insufficient_stock",
        "error": "Not enough stock",
        "available": 1,
        "requested": 3,
    }]
    assert [change["current_price"] for change in body["price_changes"]] == ["8.00"]


def test_validate_cart_unchanged_price_is_not_reported(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="9.99")
    _add(client, token, product.id, 2)

    assert _validate(client, token) == {"valid": True, "issues": [], "price_changes": []}


def test_validate_cart_unavailable_line_has_no_price_change(client, db_session):
    """A withdrawn product has no current price to compare with: it is an
    availability issue only, even if its price was also edited."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="9.99")
    _add(client, token, product.id, 1)
    db_session.query(Product).filter(Product.id == product.id).update(
        {"price": "1.00", "is_active": False}
    )
    db_session.commit()

    body = _validate(client, token)

    assert body["valid"] is False
    assert [issue["code"] for issue in body["issues"]] == ["product_unavailable"]
    assert body["price_changes"] == []


def test_validate_cart_reports_variant_stock_not_product_stock(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=100)
    variant = _make_variant(db_session, product.id, stock=5)
    _add(client, token, product.id, 4, variant_id=variant.id)
    db_session.query(ProductVariant).filter(ProductVariant.id == variant.id).update(
        {"stock_quantity": 2}
    )
    db_session.commit()

    body = _validate(client, token)

    assert body["valid"] is False
    assert [(i["code"], i["available"], i["requested"]) for i in body["issues"]] == [
        ("insufficient_stock", 2, 4)
    ]


# ---------------------------------------------------------------------------
# Live pricing: the cart stores what the customer wants, every price and
# total is computed from the catalog at the moment of the request
# ---------------------------------------------------------------------------

def _line(cart, product_id, variant_id=None):
    return next(
        item for item in cart["items"]
        if item["product_id"] == product_id and item["variant_id"] == variant_id
    )


def test_cart_total_follows_a_product_price_change(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="9.99")
    added = _add(client, token, product.id, 2).json()["data"]
    assert added["total_amount"] == "19.98"

    _set_price(db_session, product.id, "12.50")

    cart = _cart(client, token)
    line = cart["items"][0]
    assert line["unit_price"] == "12.50"
    assert line["line_total"] == "25.00"
    assert cart["total_amount"] == "25.00"
    assert line["price_when_added"] == "9.99"
    assert line["price_changed"] is True


def test_cart_total_follows_a_variant_price_override_change(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="9.99")
    variant = _make_variant(db_session, product.id, price_override="14.50")
    _add(client, token, product.id, 2, variant_id=variant.id)

    db_session.query(ProductVariant).filter(ProductVariant.id == variant.id).update(
        {"price_override": "11.25"}
    )
    db_session.commit()

    cart = _cart(client, token)
    line = cart["items"][0]
    assert line["unit_price"] == "11.25"
    assert line["line_total"] == "22.50"
    assert cart["total_amount"] == "22.50"
    assert line["price_when_added"] == "14.50"
    assert line["price_changed"] is True


def test_variant_without_override_follows_the_product_price(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="9.99")
    variant = _make_variant(db_session, product.id, price_override=None)
    _add(client, token, product.id, 1, variant_id=variant.id)

    _set_price(db_session, product.id, "5.00")

    cart = _cart(client, token)
    assert cart["items"][0]["unit_price"] == "5.00"
    assert cart["total_amount"] == "5.00"


def test_unchanged_price_is_not_flagged(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="9.99")
    _add(client, token, product.id, 2)

    line = _cart(client, token)["items"][0]

    assert line["unit_price"] == "9.99"
    assert line["price_when_added"] == "9.99"
    assert line["price_changed"] is False
    assert line["available"] is True
    assert line["unavailable_reason"] is None


def test_mutation_responses_are_priced_live_too(client, db_session):
    """Not only GET /cart: the cart returned by update and remove carries
    the price of that moment, for every line and not just the one touched."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="9.99")
    other = _make_product(db_session, category.id, price="1.00")
    _add(client, token, product.id, 1)
    _add(client, token, other.id, 1)
    _set_price(db_session, product.id, "20.00")

    updated = client.put(
        f"{CART_PREFIX}/items/{other.id}", params={"quantity": 3}, headers=_auth_headers(token)
    ).json()["data"]
    assert updated["total_amount"] == "23.00"
    assert _line(updated, product.id)["price_changed"] is True

    removed = client.delete(
        f"{CART_PREFIX}/items/{other.id}", headers=_auth_headers(token)
    ).json()["data"]
    assert removed["total_amount"] == "20.00"

    cleared = client.delete(CART_PREFIX, headers=_auth_headers(token)).json()["data"]
    assert cleared["items"] == []
    assert cleared["total_amount"] == "0.00"


def test_cart_shows_the_current_name_and_image(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    _add(client, token, product.id, 1)

    db_session.query(Product).filter(Product.id == product.id).update(
        {"name": "Widget Pro", "images": ["https://example.com/pro.png"]}
    )
    db_session.commit()

    line = _cart(client, token)["items"][0]
    assert line["name"] == "Widget Pro"
    assert line["image"] == "https://example.com/pro.png"


def test_adding_more_of_a_line_re_records_the_price_seen(client, db_session):
    """price_when_added is what the customer last saw when putting the item
    in the cart: adding again after a price change is that moment again, so
    the notice goes away. Changing the quantity is not."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="9.99")
    _add(client, token, product.id, 1)
    _set_price(db_session, product.id, "12.50")

    after_update = client.put(
        f"{CART_PREFIX}/items/{product.id}", params={"quantity": 2}, headers=_auth_headers(token)
    ).json()["data"]["items"][0]
    assert (after_update["price_when_added"], after_update["price_changed"]) == ("9.99", True)

    after_add = _add(client, token, product.id, 1).json()["data"]["items"][0]
    assert after_add["quantity"] == 3
    assert (after_add["price_when_added"], after_add["price_changed"]) == ("12.50", False)


def test_empty_cart_total_is_a_two_decimal_zero(client):
    token = _register_and_login(client, _valid_registration_payload())

    assert _cart(client, token)["total_amount"] == "0.00"


# --- Unavailable lines -------------------------------------------------------

def test_inactive_product_line_is_kept_marked_and_left_out_of_the_total(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    kept = _make_product(db_session, category.id, price="9.99")
    withdrawn = _make_product(db_session, category.id, price="100.00")
    _add(client, token, kept.id, 2)
    _add(client, token, withdrawn.id, 1)
    _deactivate(db_session, Product, withdrawn.id)

    cart = _cart(client, token)

    assert cart["items_count"] == 2
    assert cart["total_amount"] == "19.98"
    gone = _line(cart, withdrawn.id)
    assert gone["available"] is False
    assert gone["unavailable_reason"] == "product_unavailable"
    assert gone["unit_price"] is None
    assert gone["line_total"] is None
    assert gone["price_changed"] is False
    assert gone["price_when_added"] == "100.00"
    assert gone["quantity"] == 1
    assert gone["name"] == "Widget"
    still = _line(cart, kept.id)
    assert still["available"] is True
    assert still["line_total"] == "19.98"


def test_deleted_product_line_falls_back_to_what_was_recorded(client, db_session):
    """A hard-deleted product has no current row to take a name from; the
    line still has to tell the customer what it was."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    kept = _make_product(db_session, category.id, price="9.99")
    deleted = _make_product(db_session, category.id, price="3.00")
    _add(client, token, kept.id, 1)
    _add(client, token, deleted.id, 2)
    db_session.query(Product).filter(Product.id == deleted.id).delete()
    db_session.commit()

    cart = _cart(client, token)

    assert cart["total_amount"] == "9.99"
    gone = _line(cart, deleted.id)
    assert gone["available"] is False
    assert gone["unavailable_reason"] == "product_unavailable"
    assert gone["name"] == "Widget"
    assert gone["image"] == "https://example.com/widget.png"
    assert _line(cart, kept.id)["available"] is True


def test_inactive_variant_line_is_marked_and_left_out_of_the_total(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="9.99")
    variant = _make_variant(db_session, product.id, price_override="14.50")
    _add(client, token, product.id, 1)
    _add(client, token, product.id, 1, variant_id=variant.id)
    _deactivate(db_session, ProductVariant, variant.id)

    cart = _cart(client, token)

    assert cart["total_amount"] == "9.99"
    gone = _line(cart, product.id, variant.id)
    assert gone["available"] is False
    assert gone["unavailable_reason"] == "variant_unavailable"
    assert _line(cart, product.id)["available"] is True


def test_unavailable_line_can_still_be_removed(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    _add(client, token, product.id, 1)
    _deactivate(db_session, Product, product.id)

    response = client.delete(f"{CART_PREFIX}/items/{product.id}", headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["data"]["items"] == []


# --- Decimal money -----------------------------------------------------------

def test_cart_totals_are_exact_decimals(client, db_session):
    """0.10 * 3 is 0.30000000000000004 in binary floating point, and
    10.10 * 3 + 0.03 * 7 is 30.509999999999998; the cart used to total in
    float and return exactly those."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    dime = _make_product(db_session, category.id, price="0.10")
    _add(client, token, dime.id, 3)

    cart = _cart(client, token)
    assert cart["items"][0]["line_total"] == "0.30"
    assert cart["total_amount"] == "0.30"

    client.delete(CART_PREFIX, headers=_auth_headers(token))
    product_a = _make_product(db_session, category.id, price="10.10")
    product_b = _make_product(db_session, category.id, price="0.03")
    _add(client, token, product_a.id, 3)
    _add(client, token, product_b.id, 7)

    cart = _cart(client, token)
    assert _line(cart, product_a.id)["line_total"] == "30.30"
    assert _line(cart, product_b.id)["line_total"] == "0.21"
    assert cart["total_amount"] == "30.51"


def test_cart_stores_the_recorded_price_as_an_exact_decimal_string(client, db_session, fake_redis):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="9.99")
    _add(client, token, product.id, 1)

    stored = json.loads(fake_redis.get(next(fake_redis.scan_iter(f"{CART_KEY_PREFIX}*"))))

    assert stored["items"][str(product.id)]["price_snapshot"] == "9.99"


def test_stored_line_stays_readable_by_the_previous_cart_format(client, db_session, fake_redis):
    """During a deploy, or after a rollback, a worker running the previous
    code reads carts this code wrote. That code required a number-like
    price and string name and image on every line - including for a product
    with no image, where a null would have made it drop the whole cart."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="9.99")
    db_session.query(Product).filter(Product.id == product.id).update({"images": []})
    db_session.commit()
    response = _add(client, token, product.id, 1)

    line = json.loads(fake_redis.get(next(fake_redis.scan_iter(f"{CART_KEY_PREFIX}*"))))["items"][str(product.id)]

    assert float(line["price_snapshot"]) == 9.99
    assert isinstance(line["name_snapshot"], str)
    assert line["image_snapshot"] == ""
    # The response says "no image" as null, whatever is stored.
    assert response.json()["data"]["items"][0]["image"] is None


# --- Carts written before this change ----------------------------------------

def _store_old_format_cart(fake_redis, user_id, lines):
    """A cart exactly as the previous code wrote it: every line carries
    price_snapshot (a float), name_snapshot and image_snapshot."""
    now = datetime.utcnow()
    payload = {
        "user_id": user_id,
        "items": {
            (f"{pid}:{vid}" if vid is not None else str(pid)): {
                "product_id": pid,
                "variant_id": vid,
                "quantity": qty,
                "added_at": (now - timedelta(days=2)).isoformat(),
                "price_snapshot": price,
                "name_snapshot": "Widget (old name)",
                "image_snapshot": "https://example.com/old.png",
            }
            for (pid, vid, qty, price) in lines
        },
        "created_at": (now - timedelta(days=2)).isoformat(),
        "updated_at": (now - timedelta(days=2)).isoformat(),
        "expires_at": (now + timedelta(days=5)).isoformat(),
    }
    fake_redis.set(f"{CART_KEY_PREFIX}{user_id}", json.dumps(payload), ex=5 * 24 * 3600)


def test_old_format_cart_loads_and_is_priced_live(client, db_session, fake_redis):
    payload = _valid_registration_payload()
    token = _register_and_login(client, payload)
    user = db_session.query(User).filter(User.email == payload["email"]).one()
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="12.50")
    variant_product = _make_product(db_session, category.id, price="9.99")
    variant = _make_variant(db_session, variant_product.id, price_override="14.50")
    # Added at 9.99 (now 12.50) and at 14.5 (unchanged, stored as a float).
    _store_old_format_cart(fake_redis, user.id, [
        (product.id, None, 2, 9.99),
        (variant_product.id, variant.id, 1, 14.5),
    ])

    cart = _cart(client, token)

    assert cart["items_count"] == 2
    assert cart["total_amount"] == "39.50"
    changed = _line(cart, product.id)
    assert changed["unit_price"] == "12.50"
    assert changed["price_when_added"] == "9.99"
    assert changed["price_changed"] is True
    assert changed["name"] == "Widget"
    assert changed["image"] == "https://example.com/widget.png"
    same = _line(cart, variant_product.id, variant.id)
    assert same["unit_price"] == "14.50"
    assert same["price_when_added"] == "14.50"
    assert same["price_changed"] is False

    # And it is still a cart that can be changed and validated.
    assert _add(client, token, product.id, 1).json()["data"]["total_amount"] == "52.00"
    assert _validate(client, token)["valid"] is True


# --- Query count -------------------------------------------------------------

def _is_catalog_read(statement):
    return "FROM products" in statement or "FROM product_variants" in statement


class _AsyncCatalogReads:
    """Records the product and variant SELECTs run on the AsyncSession
    engine. Only catalog reads: authentication may load the user through
    the same engine, and that is not what is being counted."""

    def __init__(self):
        self.statements = []
        self._engine = database_module.AsyncSessionLocal.kw["bind"].sync_engine

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        if _is_catalog_read(statement):
            self.statements.append(statement)

    def __enter__(self):
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc):
        event.remove(self._engine, "before_cursor_execute", self._record)


def test_cart_query_count_does_not_grow_with_the_number_of_lines(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)

    def _add_plain_and_variant_line(n):
        product = _make_product(db_session, category.id)
        variant = _make_variant(db_session, product.id, sku=f"SKU-{n}")
        _add(client, token, product.id, 1)
        _add(client, token, product.id, 1, variant_id=variant.id)

    def _selects(call):
        with _AsyncCatalogReads() as counter:
            assert call().status_code == 200
        return len(counter.statements)

    def get_cart():
        return client.get(CART_PREFIX, headers=_auth_headers(token))

    def validate_cart():
        return client.post(f"{CART_PREFIX}/validate", headers=_auth_headers(token))

    _add_plain_and_variant_line(0)
    assert len(_cart(client, token)["items"]) == 2
    small = (_selects(get_cart), _selects(validate_cart))

    for n in range(1, 6):
        _add_plain_and_variant_line(n)
    assert len(_cart(client, token)["items"]) == 12
    large = (_selects(get_cart), _selects(validate_cart))

    # One query for the products, one for the variants, however many lines.
    assert small == (2, 2)
    assert large == (2, 2)


def test_cart_without_variant_lines_skips_the_variant_query(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    for _ in range(3):
        _add(client, token, _make_product(db_session, category.id).id, 1)

    with _AsyncCatalogReads() as counter:
        client.get(CART_PREFIX, headers=_auth_headers(token))

    assert len(counter.statements) == 1


def test_empty_cart_runs_no_catalog_query(client):
    token = _register_and_login(client, _valid_registration_payload())

    with _AsyncCatalogReads() as counter:
        client.get(CART_PREFIX, headers=_auth_headers(token))
        client.post(f"{CART_PREFIX}/validate", headers=_auth_headers(token))
        client.delete(CART_PREFIX, headers=_auth_headers(token))

    assert counter.statements == []


def test_cart_routes_read_the_catalog_through_the_async_session_only(client, db_session):
    """Add, update and validate used to look products up on the sync
    Session, inline on the event loop. No cart route may issue a catalog
    query on the sync engine."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    variant = _make_variant(db_session, product.id)
    # Read the ids now: db_session shares the engine being watched, and
    # touching an attribute it has expired would be a catalog read of its own.
    product_id, variant_id = product.id, variant.id
    sync_catalog_reads = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        if _is_catalog_read(statement):
            sync_catalog_reads.append(statement)

    event.listen(db_session.get_bind(), "before_cursor_execute", _record)
    try:
        _add(client, token, product_id, 1, variant_id=variant_id)
        client.put(
            f"{CART_PREFIX}/items/{product_id}",
            params={"quantity": 2, "variant_id": variant_id},
            headers=_auth_headers(token),
        )
        client.get(CART_PREFIX, headers=_auth_headers(token))
        client.post(f"{CART_PREFIX}/validate", headers=_auth_headers(token))
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", _record)

    assert sync_catalog_reads == []


# --- Cart lifetime -----------------------------------------------------------

def test_cart_ttl_default_is_thirty_days():
    """With live prices a long-lived cart can no longer show a stale price,
    which was the reason to keep it at 7."""
    assert Settings.model_fields["REDIS_CART_TTL_DAYS"].default == 30
