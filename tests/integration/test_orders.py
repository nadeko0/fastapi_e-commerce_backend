import threading
import uuid
from decimal import Decimal

import fakeredis
import pytest
from redis import ConnectionError as RedisConnectionError

from app.core.security import get_password_hash
from app.models.category import Category
from app.models.enums import UserRole
from app.models.order import Order
from app.models.product import Product, ProductVariant
from app.models.user import User
from app.services.redis_async import AsyncRedisService

USERS_PREFIX = "/api/v1/users"
CART_PREFIX = "/api/v1/cart"
ORDERS_PREFIX = "/api/v1/orders"
ADMIN_PREFIX = "/api/v1/admin"


def _registration_payload(email="buyer@example.com"):
    return {
        "email": email,
        "password": "Str0ngPass1",
        "full_name": "Buyer Person",
        "gdpr_consent": True,
        "privacy_policy_accepted": True,
        "marketing_consent": False,
    }


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _with_idempotency_key(headers):
    # POST /orders requires an Idempotency-Key; tests that are not about
    # idempotency itself just need a fresh one per request. A key the test
    # set itself is kept.
    return {"Idempotency-Key": uuid.uuid4().hex, **headers}


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


def _make_admin(db_session, email="admin@example.com"):
    admin = User(
        email=email,
        hashed_password=get_password_hash("AdminPass1"),
        full_name="Admin User",
        phone="+123456789",
        role=UserRole.ADMIN,
        gdpr_consent=True,
        privacy_policy_accepted=True,
        marketing_consent=False,
        consent_history=[],
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    return admin


def _login(client, email, password):
    login_response = client.post(
        f"{USERS_PREFIX}/login",
        data={"username": email, "password": password},
    )
    return login_response.json()["access_token"]


def _checkout_ready_buyer(client, db_session, email="buyer@example.com"):
    """Register, verify email, complete profile, and add an address - all
    the preconditions create_order enforces before it'll accept an order."""
    payload = _registration_payload(email)
    client.post(f"{USERS_PREFIX}/register", json=payload)
    token = _login(client, email, payload["password"])

    client.put(
        f"{USERS_PREFIX}/me",
        json={"full_name": "Buyer Person", "phone": "+12025550123"},
        headers=_auth_headers(token),
    )

    user = db_session.query(User).filter(User.email == email).first()
    user.is_email_verified = True
    db_session.commit()

    address_response = client.post(
        f"{USERS_PREFIX}/addresses",
        json={
            "street": "123 Main Street",
            "city": "Springfield",
            "state": "Illinois",
            "postal_code": "62701",
            "country": "US",
        },
        headers=_auth_headers(token),
    )
    address_id = address_response.json()["id"]

    return token, address_id


def test_create_order_requires_email_verification(client, db_session):
    payload = _registration_payload()
    client.post(f"{USERS_PREFIX}/register", json=payload)
    token = _login(client, payload["email"], payload["password"])

    response = client.post(
        ORDERS_PREFIX,
        json={"shipping_address_id": 1},
        headers=_with_idempotency_key(_auth_headers(token)),
    )

    assert response.status_code == 403


def test_create_order_rejects_empty_cart(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)

    response = client.post(
        ORDERS_PREFIX,
        json={"shipping_address_id": address_id},
        headers=_with_idempotency_key(_auth_headers(token)),
    )

    assert response.status_code == 400


def test_full_checkout_flow_decrements_stock_and_resolves_address(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10, price="19.99")

    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 3},
        headers=_auth_headers(token),
    )

    response = client.post(
        ORDERS_PREFIX,
        json={"shipping_address_id": address_id},
        headers=_with_idempotency_key(_auth_headers(token)),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["shipping_address"]["id"] == address_id
    assert float(body["total_amount"]) == 59.97
    assert len(body["items"]) == 1
    assert body["items"][0]["quantity"] == 3

    db_session.refresh(product)
    assert product.stock_quantity == 7

    # Cart was cleared after checkout.
    cart_response = client.get(CART_PREFIX, headers=_auth_headers(token))
    assert cart_response.json()["items"] == []


def test_create_order_rejects_unknown_shipping_address(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    response = client.post(
        ORDERS_PREFIX,
        json={"shipping_address_id": 999999},
        headers=_with_idempotency_key(_auth_headers(token)),
    )

    assert response.status_code == 404


def test_create_order_rejects_a_deleted_shipping_address(client, db_session):
    # Deleting an address only deactivates it (is_active=False, kept for the
    # orders already shipped there), and create_order looked it up by id and
    # owner alone: an order could still be placed to an address the
    # customer had removed. It is now "not found", like a missing one.
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    deleted = client.delete(
        f"{USERS_PREFIX}/addresses/{address_id}", headers=_auth_headers(token)
    )
    assert deleted.status_code == 200, deleted.text

    response = client.post(
        ORDERS_PREFIX,
        json={"shipping_address_id": address_id},
        headers=_with_idempotency_key(_auth_headers(token)),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Shipping address not found"
    db_session.refresh(product)
    assert product.stock_quantity == 10
    assert db_session.query(Order).count() == 0


def test_create_order_is_idempotent_with_same_key(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 2},
        headers=_auth_headers(token),
    )

    headers = _auth_headers(token) | {"Idempotency-Key": "checkout-key-123"}
    first = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=headers
    )
    assert first.status_code == 200
    first_order_id = first.json()["id"]

    # Re-add an item so the cart isn't empty for the retried request too.
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    second = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=headers
    )

    assert second.status_code == 200
    assert second.json()["id"] == first_order_id

    db_session.refresh(product)
    # Only the first request's 2 units were ever decremented - the retry
    # returned the existing order instead of placing a second one.
    assert product.stock_quantity == 8


@pytest.mark.parametrize("bad_headers", [{}, {"Idempotency-Key": "short"}])
def test_create_order_requires_an_idempotency_key(client, db_session, bad_headers):
    """The header used to be optional. Without it a retried request (lost
    response, double click, second tab) placed a second order from whatever
    was still in the cart, and the server had no way to tell the retry from
    a new purchase. Now a request that cannot be deduplicated is refused
    before it touches anything."""
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 2},
        headers=_auth_headers(token),
    )

    response = client.post(
        ORDERS_PREFIX,
        json={"shipping_address_id": address_id},
        headers=_auth_headers(token) | bad_headers,
    )

    assert response.status_code == 422
    assert "Idempotency-Key" in response.text
    # Nothing happened: no order, stock untouched, cart intact.
    assert db_session.query(Order).count() == 0
    db_session.refresh(product)
    assert product.stock_quantity == 10
    cart = client.get(CART_PREFIX, headers=_auth_headers(token)).json()
    assert [item["quantity"] for item in cart["items"]] == [2]


def test_two_checkouts_with_different_keys_are_two_orders(client, db_session):
    # The other half of the contract: a new key is a new purchase.
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    order_ids = []
    for _ in range(2):
        client.post(
            f"{CART_PREFIX}/items",
            json={"product_id": product.id, "quantity": 1},
            headers=_auth_headers(token),
        )
        response = client.post(
            ORDERS_PREFIX,
            json={"shipping_address_id": address_id},
            headers=_with_idempotency_key(_auth_headers(token)),
        )
        assert response.status_code == 200
        order_ids.append(response.json()["id"])

    assert order_ids[0] != order_ids[1]
    db_session.refresh(product)
    assert product.stock_quantity == 8


def test_get_order_returns_404_for_other_users_order(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session, email="buyer1@example.com")
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=_with_idempotency_key(_auth_headers(token))
    ).json()

    other_token, _ = _checkout_ready_buyer(client, db_session, email="buyer2@example.com")

    response = client.get(
        f"{ORDERS_PREFIX}/{order['id']}", headers=_auth_headers(other_token)
    )

    assert response.status_code == 404


def test_get_order_returns_own_order(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=_with_idempotency_key(_auth_headers(token))
    ).json()

    response = client.get(f"{ORDERS_PREFIX}/{order['id']}", headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["id"] == order["id"]


def _create_order(client, db_session, email="buyer@example.com"):
    token, address_id = _checkout_ready_buyer(client, db_session, email=email)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=_with_idempotency_key(_auth_headers(token))
    ).json()
    return token, order


def test_update_order_status_requires_admin(client, db_session):
    token, order = _create_order(client, db_session)

    response = client.put(
        f"{ORDERS_PREFIX}/{order['id']}/status",
        json={"status": "confirmed"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 403


def test_admin_updates_order_status_with_valid_transition(client, db_session):
    _, order = _create_order(client, db_session)
    admin = _make_admin(db_session)
    admin_token = _login(client, admin.email, "AdminPass1")

    response = client.put(
        f"{ORDERS_PREFIX}/{order['id']}/status",
        json={"status": "confirmed"},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"


def test_admin_rejects_invalid_status_transition(client, db_session):
    _, order = _create_order(client, db_session)
    admin = _make_admin(db_session)
    admin_token = _login(client, admin.email, "AdminPass1")

    # new -> delivered skips confirmed/processing/sent entirely.
    response = client.put(
        f"{ORDERS_PREFIX}/{order['id']}/status",
        json={"status": "delivered"},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 400


def test_admin_update_status_returns_404_for_missing_order(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login(client, admin.email, "AdminPass1")

    response = client.put(
        f"{ORDERS_PREFIX}/999999/status",
        json={"status": "confirmed"},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Concurrency: last-unit checkout race
# ---------------------------------------------------------------------------

def test_concurrent_checkout_for_last_unit_only_one_succeeds(client, db_session):
    """N users simultaneously try to buy the last unit of a stock=1 product.

    Honesty note on what this actually proves: tests/conftest.py binds the
    whole suite to a single SQLite ":memory:" engine with poolclass=StaticPool,
    which means every session - including one per Python thread here - shares
    the *same* underlying sqlite3 DBAPI connection. Python's sqlite3 module
    serializes statement execution on a shared connection internally, so the
    N threads below do NOT achieve genuine interleaved/overlapping database
    transactions: each request's DB work runs to completion before the next
    one's begins, regardless of thread scheduling. This test therefore
    verifies the *end-to-end outcome* (exactly one 200, the rest a clean 400,
    final stock exactly 0) under real thread-level concurrency at the
    HTTP/dispatch layer - it does NOT prove the atomic `UPDATE ... WHERE
    stock_quantity >= quantity` clause specifically is what prevents
    overselling, because a naive read-then-write implementation would look
    identical once every DB statement is forcibly serialized like this.
    See test_atomic_decrement_ignores_stale_in_memory_read below for a test
    that isolates and proves the WHERE-clause's row-count semantics
    directly, independent of thread timing.
    """
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=1, price="9.99")

    n = 5
    accounts = []
    for i in range(n):
        token, address_id = _checkout_ready_buyer(
            client, db_session, email=f"racer{i}@example.com"
        )
        client.post(
            f"{CART_PREFIX}/items",
            json={"product_id": product.id, "quantity": 1},
            headers=_auth_headers(token),
        )
        accounts.append((token, address_id))

    results = [None] * n
    barrier = threading.Barrier(n)

    def worker(i):
        token, address_id = accounts[i]
        barrier.wait()
        response = client.post(
            ORDERS_PREFIX,
            json={"shipping_address_id": address_id},
            headers=_with_idempotency_key(_auth_headers(token)),
        )
        results[i] = response.status_code

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1
    assert results.count(400) == n - 1

    db_session.refresh(product)
    assert product.stock_quantity == 0


def test_atomic_decrement_ignores_stale_in_memory_read(db_session):
    """Directly exercises the row-count semantics of the atomic
    `UPDATE Product ... WHERE stock_quantity >= quantity` used by
    create_order (app/api/v1/orders.py), decoupled from thread timing (see
    the caveat in test_concurrent_checkout_for_last_unit_only_one_succeeds
    above about why threads alone can't prove this against the SQLite test
    DB).

    Simulates two "in-flight" checkout attempts that both believe
    stock_quantity is still 1: the first decrement succeeds and updates one
    row; the second decrement - using the exact same stale WHERE condition,
    as if it had read stock before the first committed - correctly matches
    zero rows, because the WHERE clause is evaluated against the database's
    current row value at execution time, not a value the caller cached
    earlier. This is the actual mechanism that prevents overselling; a
    naive "SELECT stock_quantity, then UPDATE if enough" implementation
    would let both of these "attempts" succeed.
    """
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=1)

    first_rows = db_session.query(Product).filter(
        Product.id == product.id,
        Product.stock_quantity >= 1,
    ).update(
        {Product.stock_quantity: Product.stock_quantity - 1},
        synchronize_session=False,
    )
    assert first_rows == 1

    second_rows = db_session.query(Product).filter(
        Product.id == product.id,
        Product.stock_quantity >= 1,
    ).update(
        {Product.stock_quantity: Product.stock_quantity - 1},
        synchronize_session=False,
    )
    assert second_rows == 0

    db_session.commit()
    db_session.refresh(product)
    assert product.stock_quantity == 0


# ---------------------------------------------------------------------------
# Cancellation across every state-machine stage
# ---------------------------------------------------------------------------

def _transition(client, admin_token, order_id, new_status):
    return client.put(
        f"{ORDERS_PREFIX}/{order_id}/status",
        json={"status": new_status},
        headers=_auth_headers(admin_token),
    )


def test_cancel_from_new_succeeds_and_restores_stock(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 3},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=_with_idempotency_key(_auth_headers(token))
    ).json()

    db_session.refresh(product)
    assert product.stock_quantity == 7

    admin = _make_admin(db_session, email="admin-new@example.com")
    admin_token = _login(client, admin.email, "AdminPass1")
    response = _transition(client, admin_token, order["id"], "cancelled")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    db_session.refresh(product)
    assert product.stock_quantity == 10


def test_cancel_from_confirmed_succeeds_and_restores_stock(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 2},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=_with_idempotency_key(_auth_headers(token))
    ).json()

    admin = _make_admin(db_session, email="admin-confirmed@example.com")
    admin_token = _login(client, admin.email, "AdminPass1")
    _transition(client, admin_token, order["id"], "confirmed")
    response = _transition(client, admin_token, order["id"], "cancelled")

    assert response.status_code == 200
    db_session.refresh(product)
    assert product.stock_quantity == 10


def test_cancel_from_processing_succeeds_and_restores_stock(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 4},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=_with_idempotency_key(_auth_headers(token))
    ).json()

    admin = _make_admin(db_session, email="admin-processing@example.com")
    admin_token = _login(client, admin.email, "AdminPass1")
    _transition(client, admin_token, order["id"], "confirmed")
    _transition(client, admin_token, order["id"], "processing")
    response = _transition(client, admin_token, order["id"], "cancelled")

    assert response.status_code == 200
    db_session.refresh(product)
    assert product.stock_quantity == 10


def test_cancel_from_sent_succeeds_and_restores_stock(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=_with_idempotency_key(_auth_headers(token))
    ).json()

    admin = _make_admin(db_session, email="admin-sent@example.com")
    admin_token = _login(client, admin.email, "AdminPass1")
    _transition(client, admin_token, order["id"], "confirmed")
    _transition(client, admin_token, order["id"], "processing")
    _transition(client, admin_token, order["id"], "sent")
    response = _transition(client, admin_token, order["id"], "cancelled")

    assert response.status_code == 200
    db_session.refresh(product)
    assert product.stock_quantity == 10


def test_cancel_from_delivered_is_rejected(client, db_session):
    """Delivered is terminal per _is_valid_status_transition - cancelling
    after delivery must be rejected, and stock must NOT be restored."""
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=_with_idempotency_key(_auth_headers(token))
    ).json()

    admin = _make_admin(db_session, email="admin-delivered@example.com")
    admin_token = _login(client, admin.email, "AdminPass1")
    _transition(client, admin_token, order["id"], "confirmed")
    _transition(client, admin_token, order["id"], "processing")
    _transition(client, admin_token, order["id"], "sent")
    _transition(client, admin_token, order["id"], "delivered")
    response = _transition(client, admin_token, order["id"], "cancelled")

    assert response.status_code == 400

    db_session.refresh(product)
    assert product.stock_quantity == 9


def test_cancel_from_already_cancelled_is_rejected(client, db_session):
    """Cancelled is also terminal - re-cancelling must not double-restock."""
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 2},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=_with_idempotency_key(_auth_headers(token))
    ).json()

    admin = _make_admin(db_session, email="admin-recancel@example.com")
    admin_token = _login(client, admin.email, "AdminPass1")
    _transition(client, admin_token, order["id"], "cancelled")

    db_session.refresh(product)
    assert product.stock_quantity == 10

    response = _transition(client, admin_token, order["id"], "cancelled")

    assert response.status_code == 400
    db_session.refresh(product)
    assert product.stock_quantity == 10  # unchanged - no double-restock


# ---------------------------------------------------------------------------
# Decimal arithmetic in order totals
# ---------------------------------------------------------------------------

def test_order_total_uses_exact_decimal_arithmetic_for_uneven_prices(client, db_session):
    """9.99 * 3 = 29.97 exactly; if total_amount were accumulated with
    float instead of Decimal anywhere in create_order's path, this is
    exactly the kind of value where binary-float rounding would surface
    (e.g. 29.97 vs 29.969999999999999 or 29.970000000000002)."""
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10, price="9.99")
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 3},
        headers=_auth_headers(token),
    )

    response = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=_with_idempotency_key(_auth_headers(token))
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total_amount"] == "29.97"

    order = db_session.query(Order).filter(Order.id == body["id"]).first()
    assert order.total_amount == Decimal("29.97")


def test_order_total_sums_multiple_odd_priced_items_without_drift(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product_a = _make_product(db_session, category.id, stock=10, price="10.10")
    product_b = _make_product(db_session, category.id, stock=10, price="0.03")

    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product_a.id, "quantity": 3},
        headers=_auth_headers(token),
    )
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product_b.id, "quantity": 7},
        headers=_auth_headers(token),
    )

    response = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=_with_idempotency_key(_auth_headers(token))
    )

    assert response.status_code == 200
    body = response.json()
    # 10.10 * 3 + 0.03 * 7 = 30.30 + 0.21 = 30.51
    assert body["total_amount"] == "30.51"


# ---------------------------------------------------------------------------
# Product variants at checkout
# ---------------------------------------------------------------------------

def test_checkout_with_variant_decrements_only_variant_stock(client, db_session):
    """The parent product's own stock_quantity must be left untouched when
    the cart line is for a variant - only ProductVariant.stock_quantity
    for that specific variant is decremented."""
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10, price="9.99")
    variant = _make_variant(db_session, product.id, stock=5, price_override="14.50")

    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "variant_id": variant.id, "quantity": 2},
        headers=_auth_headers(token),
    )

    response = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=_with_idempotency_key(_auth_headers(token))
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["variant_id"] == variant.id
    assert float(body["total_amount"]) == 29.00  # 14.50 * 2, the variant's price

    db_session.refresh(product)
    db_session.refresh(variant)
    assert variant.stock_quantity == 3
    assert product.stock_quantity == 10  # untouched


def test_checkout_rejects_insufficient_variant_stock_without_partial_decrement(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10, price="9.99")
    variant = _make_variant(db_session, product.id, stock=2)

    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "variant_id": variant.id, "quantity": 2},
        headers=_auth_headers(token),
    )
    # Stock drops below the cart's requested quantity after adding.
    db_session.query(ProductVariant).filter(ProductVariant.id == variant.id).update(
        {"stock_quantity": 1}
    )
    db_session.commit()

    response = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=_with_idempotency_key(_auth_headers(token))
    )

    assert response.status_code == 400

    db_session.refresh(variant)
    db_session.refresh(product)
    assert variant.stock_quantity == 1  # unchanged - no partial decrement
    assert product.stock_quantity == 10


def test_checkout_with_plain_and_variant_line_decrements_each_correctly(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    plain_product = _make_product(db_session, category.id, stock=10, price="5.00")
    variant_product = _make_product(db_session, category.id, stock=10, price="9.99")
    variant = _make_variant(db_session, variant_product.id, stock=5, price_override=None)

    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": plain_product.id, "quantity": 2},
        headers=_auth_headers(token),
    )
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": variant_product.id, "variant_id": variant.id, "quantity": 3},
        headers=_auth_headers(token),
    )

    response = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=_with_idempotency_key(_auth_headers(token))
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    # 5.00 * 2 + 9.99 * 3 = 10.00 + 29.97 = 39.97
    assert float(body["total_amount"]) == 39.97

    db_session.refresh(plain_product)
    db_session.refresh(variant_product)
    db_session.refresh(variant)
    assert plain_product.stock_quantity == 8
    assert variant.stock_quantity == 2
    assert variant_product.stock_quantity == 10  # the variant's parent product's own stock is untouched


def test_cancel_order_with_variant_line_restocks_variant_not_product(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10, price="9.99")
    variant = _make_variant(db_session, product.id, stock=5)

    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "variant_id": variant.id, "quantity": 2},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=_with_idempotency_key(_auth_headers(token))
    ).json()

    db_session.refresh(variant)
    assert variant.stock_quantity == 3

    admin = _make_admin(db_session, email="admin-variant-cancel@example.com")
    admin_token = _login(client, admin.email, "AdminPass1")
    response = _transition(client, admin_token, order["id"], "cancelled")

    assert response.status_code == 200

    db_session.refresh(variant)
    db_session.refresh(product)
    assert variant.stock_quantity == 5  # restored
    assert product.stock_quantity == 10  # never touched


def test_concurrent_checkout_for_last_variant_unit_only_one_succeeds(client, db_session):
    """Mirrors test_concurrent_checkout_for_last_unit_only_one_succeeds
    above, but for a variant's own stock_quantity=1 instead of the
    product's - the same atomic conditional-UPDATE pattern must prevent
    overselling the variant."""
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=100, price="9.99")
    variant = _make_variant(db_session, product.id, stock=1)

    n = 5
    accounts = []
    for i in range(n):
        token, address_id = _checkout_ready_buyer(
            client, db_session, email=f"variant-racer{i}@example.com"
        )
        client.post(
            f"{CART_PREFIX}/items",
            json={"product_id": product.id, "variant_id": variant.id, "quantity": 1},
            headers=_auth_headers(token),
        )
        accounts.append((token, address_id))

    results = [None] * n
    barrier = threading.Barrier(n)

    def worker(i):
        token, address_id = accounts[i]
        barrier.wait()
        response = client.post(
            ORDERS_PREFIX,
            json={"shipping_address_id": address_id},
            headers=_with_idempotency_key(_auth_headers(token)),
        )
        results[i] = response.status_code

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1
    assert results.count(400) == n - 1

    db_session.refresh(variant)
    db_session.refresh(product)
    assert variant.stock_quantity == 0
    assert product.stock_quantity == 100  # untouched throughout


# ---------------------------------------------------------------------------
# Cart review findings D and E (scripts/loadtest/results/cart-review.md)
# ---------------------------------------------------------------------------

def _add_to_cart(client, token, product_id, quantity, variant_id=None):
    params = {"product_id": product_id, "quantity": quantity}
    if variant_id is not None:
        params["variant_id"] = variant_id
    return client.post(f"{CART_PREFIX}/items", json=params, headers=_auth_headers(token))


def _place_order(client, token, address_id):
    return client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=_with_idempotency_key(_auth_headers(token))
    )


def test_create_order_rejects_product_deactivated_after_adding(client, db_session):
    """A product can be switched off while it sits in someone's cart; the
    order used to go through anyway."""
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    still_sold = _make_product(db_session, category.id, stock=10)
    withdrawn = _make_product(db_session, category.id, stock=10)
    _add_to_cart(client, token, still_sold.id, 2)
    _add_to_cart(client, token, withdrawn.id, 1)
    db_session.query(Product).filter(Product.id == withdrawn.id).update({"is_active": False})
    db_session.commit()

    response = _place_order(client, token, address_id)

    assert response.status_code == 400
    assert db_session.query(Order).count() == 0
    # The line processed before the withdrawn one must be rolled back too.
    db_session.refresh(still_sold)
    db_session.refresh(withdrawn)
    assert still_sold.stock_quantity == 10
    assert withdrawn.stock_quantity == 10
    cart = client.get(CART_PREFIX, headers=_auth_headers(token)).json()
    assert cart["items_count"] == 2


def test_create_order_rejects_variant_deactivated_after_adding(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    variant = _make_variant(db_session, product.id, stock=5)
    _add_to_cart(client, token, product.id, 1, variant_id=variant.id)
    db_session.query(ProductVariant).filter(ProductVariant.id == variant.id).update(
        {"is_active": False}
    )
    db_session.commit()

    response = _place_order(client, token, address_id)

    assert response.status_code == 400
    assert db_session.query(Order).count() == 0
    db_session.refresh(variant)
    assert variant.stock_quantity == 5


def test_create_order_keeps_items_added_to_the_cart_during_checkout(client, db_session, monkeypatch):
    """Checkout reads the cart, builds the order from that read, and then
    cleared the whole cart - so an item added in between (a second tab, a
    slow request) was deleted without ever having been ordered. The add is
    injected here right after checkout's read returns."""
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    ordered = _make_product(db_session, category.id, stock=10)
    added_late = _make_product(db_session, category.id, stock=10)
    _add_to_cart(client, token, ordered.id, 2)

    real_get_cart = AsyncRedisService.get_cart

    async def _get_cart_then_concurrent_add(self, user_id):
        cart = await real_get_cart(self, user_id)
        await self.add_to_cart_atomic(
            user_id=user_id, product_id=added_late.id, quantity=3, price=Decimal("9.99"),
            name="Widget", image="", stock_quantity=10,
        )
        return cart

    monkeypatch.setattr(AsyncRedisService, "get_cart", _get_cart_then_concurrent_add)
    response = _place_order(client, token, address_id)
    monkeypatch.setattr(AsyncRedisService, "get_cart", real_get_cart)

    assert response.status_code == 200
    order_items = response.json()["items"]
    assert [(item["product_id"], item["quantity"]) for item in order_items] == [(ordered.id, 2)]

    cart = client.get(CART_PREFIX, headers=_auth_headers(token)).json()
    assert [(item["product_id"], item["quantity"]) for item in cart["items"]] == [(added_late.id, 3)]


def test_create_order_still_succeeds_when_the_cart_cannot_be_cleared(client, db_session, monkeypatch):
    """The order is committed before the cart is touched, so a Redis failure
    at that point must not turn a placed order into an error response."""
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    _add_to_cart(client, token, product.id, 2)

    # The routes use the async client; every one the test run hands out is a
    # FakeAsyncRedis, so its class is what breaks.
    async def _raise(*args, **kwargs):
        raise RedisConnectionError("connection lost")

    monkeypatch.setattr(fakeredis.FakeAsyncRedis, "transaction", _raise)
    monkeypatch.setattr(fakeredis.FakeAsyncRedis, "delete", _raise)

    response = _place_order(client, token, address_id)

    assert response.status_code == 200
    assert db_session.query(Order).count() == 1


# ---------------------------------------------------------------------------
# Cart and order agree on the total
# ---------------------------------------------------------------------------

def test_order_charges_the_total_the_cart_showed_just_before(client, db_session):
    """The cart used to total the prices recorded when each line was added
    while the order charged the live ones, so after a price change the
    customer was shown one total and charged another. Every price here is
    changed after the line went into the cart."""
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    plain = _make_product(db_session, category.id, stock=10, price="9.99")
    with_variants = _make_product(db_session, category.id, stock=10, price="20.00")
    overridden = _make_variant(db_session, with_variants.id, sku="V-OVERRIDE", price_override="14.50")
    inherits = _make_variant(db_session, with_variants.id, sku="V-INHERIT", price_override=None)
    _add_to_cart(client, token, plain.id, 3)
    _add_to_cart(client, token, with_variants.id, 2, variant_id=overridden.id)
    _add_to_cart(client, token, with_variants.id, 1, variant_id=inherits.id)

    db_session.query(Product).filter(Product.id == plain.id).update({"price": "12.49"})
    db_session.query(Product).filter(Product.id == with_variants.id).update({"price": "21.10"})
    db_session.query(ProductVariant).filter(ProductVariant.id == overridden.id).update(
        {"price_override": "7.77"}
    )
    db_session.commit()

    cart = client.get(CART_PREFIX, headers=_auth_headers(token)).json()
    response = _place_order(client, token, address_id)

    assert response.status_code == 200
    order = response.json()
    # 12.49 * 3 + 7.77 * 2 + 21.10 * 1; at the prices recorded on adding it
    # would have been 9.99 * 3 + 14.50 * 2 + 20.00 = 78.97.
    assert cart["total_amount"] == "74.11"
    assert order["total_amount"] == cart["total_amount"]
    assert Decimal(order["total_amount"]) == db_session.query(Order).one().total_amount
    cart_lines = {
        (line["product_id"], line["variant_id"]): line["unit_price"] for line in cart["items"]
    }
    order_lines = {
        (line["product_id"], line["variant_id"]): line["price_at_time"] for line in order["items"]
    }
    assert order_lines == cart_lines
