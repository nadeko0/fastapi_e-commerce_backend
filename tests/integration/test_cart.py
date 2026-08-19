import threading

import pytest

from app.models.category import Category
from app.models.product import Product

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
    assert response.json()["data"]["valid"] is True
    assert response.json()["data"]["issues"] == []


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


def test_cart_requires_authentication(client):
    response = client.get(CART_PREFIX)

    assert response.status_code == 401


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


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Known lost-update race, not fixed in this pass: add_to_cart "
        "(app/api/v1/cart.py) does a plain Redis GET (RedisService.get_cart) "
        "then, after mutating the in-process Cart object, a plain SETEX "
        "(RedisService.update_cart) with no compare-and-set / WATCH-MULTI / "
        "Lua guard. Two requests that read the same cart before either "
        "writes back will both compute their own 'old quantity + delta', and "
        "the second SETEX unconditionally overwrites the first - one "
        "addition is silently lost instead of both accumulating. Reproduced "
        "reliably (3/3 runs) with N=10 concurrent threads collapsing to "
        "quantity=1 instead of 10. Fixing this correctly needs an atomic "
        "primitive (WATCH/MULTI transaction or a Lua script) added to "
        "RedisService (app/services/redis.py), which is out of scope for "
        "this pass (owned by another agent) - see "
        ".agent-notes/orders_inventory_review.md."
    ),
)
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
