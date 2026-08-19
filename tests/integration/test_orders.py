import threading
from decimal import Decimal

from app.core.security import get_password_hash
from app.models.category import Category
from app.models.enums import UserRole
from app.models.order import Order
from app.models.product import Product, ProductVariant
from app.models.user import User

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
    return login_response.json()["data"]["access_token"]


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
    address_id = address_response.json()["data"]["id"]

    return token, address_id


def test_create_order_requires_email_verification(client, db_session):
    payload = _registration_payload()
    client.post(f"{USERS_PREFIX}/register", json=payload)
    token = _login(client, payload["email"], payload["password"])

    response = client.post(
        ORDERS_PREFIX,
        params={"shipping_address_id": 1},
        headers=_auth_headers(token),
    )

    assert response.status_code == 403


def test_create_order_rejects_empty_cart(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)

    response = client.post(
        ORDERS_PREFIX,
        params={"shipping_address_id": address_id},
        headers=_auth_headers(token),
    )

    assert response.status_code == 400


def test_full_checkout_flow_decrements_stock_and_resolves_address(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10, price="19.99")

    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 3},
        headers=_auth_headers(token),
    )

    response = client.post(
        ORDERS_PREFIX,
        params={"shipping_address_id": address_id},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["shipping_address"]["id"] == address_id
    assert float(body["total_amount"]) == 59.97
    assert len(body["items"]) == 1
    assert body["items"][0]["quantity"] == 3

    db_session.refresh(product)
    assert product.stock_quantity == 7

    # Cart was cleared after checkout.
    cart_response = client.get(CART_PREFIX, headers=_auth_headers(token))
    assert cart_response.json()["data"]["items"] == []


def test_create_order_rejects_unknown_shipping_address(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    response = client.post(
        ORDERS_PREFIX,
        params={"shipping_address_id": 999999},
        headers=_auth_headers(token),
    )

    assert response.status_code == 404


def test_create_order_is_idempotent_with_same_key(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 2},
        headers=_auth_headers(token),
    )

    headers = _auth_headers(token) | {"Idempotency-Key": "checkout-key-123"}
    first = client.post(
        ORDERS_PREFIX, params={"shipping_address_id": address_id}, headers=headers
    )
    assert first.status_code == 200
    first_order_id = first.json()["data"]["id"]

    # Re-add an item so the cart isn't empty for the retried request too.
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    second = client.post(
        ORDERS_PREFIX, params={"shipping_address_id": address_id}, headers=headers
    )

    assert second.status_code == 200
    assert second.json()["data"]["id"] == first_order_id

    db_session.refresh(product)
    # Only the first request's 2 units were ever decremented - the retry
    # returned the existing order instead of placing a second one.
    assert product.stock_quantity == 8


def test_get_order_returns_404_for_other_users_order(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session, email="buyer1@example.com")
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, params={"shipping_address_id": address_id}, headers=_auth_headers(token)
    ).json()["data"]

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
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, params={"shipping_address_id": address_id}, headers=_auth_headers(token)
    ).json()["data"]

    response = client.get(f"{ORDERS_PREFIX}/{order['id']}", headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["data"]["id"] == order["id"]


def _create_order(client, db_session, email="buyer@example.com"):
    token, address_id = _checkout_ready_buyer(client, db_session, email=email)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, params={"shipping_address_id": address_id}, headers=_auth_headers(token)
    ).json()["data"]
    return token, order


def test_update_order_status_requires_admin(client, db_session):
    token, order = _create_order(client, db_session)

    response = client.put(
        f"{ORDERS_PREFIX}/{order['id']}/status",
        params={"status": "confirmed"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 403


def test_admin_updates_order_status_with_valid_transition(client, db_session):
    _, order = _create_order(client, db_session)
    admin = _make_admin(db_session)
    admin_token = _login(client, admin.email, "AdminPass1")

    response = client.put(
        f"{ORDERS_PREFIX}/{order['id']}/status",
        params={"status": "confirmed"},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "confirmed"


def test_admin_rejects_invalid_status_transition(client, db_session):
    _, order = _create_order(client, db_session)
    admin = _make_admin(db_session)
    admin_token = _login(client, admin.email, "AdminPass1")

    # new -> delivered skips confirmed/processing/sent entirely.
    response = client.put(
        f"{ORDERS_PREFIX}/{order['id']}/status",
        params={"status": "delivered"},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 400


def test_admin_update_status_returns_404_for_missing_order(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login(client, admin.email, "AdminPass1")

    response = client.put(
        f"{ORDERS_PREFIX}/999999/status",
        params={"status": "confirmed"},
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
            params={"product_id": product.id, "quantity": 1},
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
            params={"shipping_address_id": address_id},
            headers=_auth_headers(token),
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
        params={"status": new_status},
        headers=_auth_headers(admin_token),
    )


def test_cancel_from_new_succeeds_and_restores_stock(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 3},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, params={"shipping_address_id": address_id}, headers=_auth_headers(token)
    ).json()["data"]

    db_session.refresh(product)
    assert product.stock_quantity == 7

    admin = _make_admin(db_session, email="admin-new@example.com")
    admin_token = _login(client, admin.email, "AdminPass1")
    response = _transition(client, admin_token, order["id"], "cancelled")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "cancelled"

    db_session.refresh(product)
    assert product.stock_quantity == 10


def test_cancel_from_confirmed_succeeds_and_restores_stock(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 2},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, params={"shipping_address_id": address_id}, headers=_auth_headers(token)
    ).json()["data"]

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
        params={"product_id": product.id, "quantity": 4},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, params={"shipping_address_id": address_id}, headers=_auth_headers(token)
    ).json()["data"]

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
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, params={"shipping_address_id": address_id}, headers=_auth_headers(token)
    ).json()["data"]

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
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, params={"shipping_address_id": address_id}, headers=_auth_headers(token)
    ).json()["data"]

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
        params={"product_id": product.id, "quantity": 2},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, params={"shipping_address_id": address_id}, headers=_auth_headers(token)
    ).json()["data"]

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
        params={"product_id": product.id, "quantity": 3},
        headers=_auth_headers(token),
    )

    response = client.post(
        ORDERS_PREFIX, params={"shipping_address_id": address_id}, headers=_auth_headers(token)
    )

    assert response.status_code == 200
    body = response.json()["data"]
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
        params={"product_id": product_a.id, "quantity": 3},
        headers=_auth_headers(token),
    )
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product_b.id, "quantity": 7},
        headers=_auth_headers(token),
    )

    response = client.post(
        ORDERS_PREFIX, params={"shipping_address_id": address_id}, headers=_auth_headers(token)
    )

    assert response.status_code == 200
    body = response.json()["data"]
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
        params={"product_id": product.id, "variant_id": variant.id, "quantity": 2},
        headers=_auth_headers(token),
    )

    response = client.post(
        ORDERS_PREFIX, params={"shipping_address_id": address_id}, headers=_auth_headers(token)
    )

    assert response.status_code == 200
    body = response.json()["data"]
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
        params={"product_id": product.id, "variant_id": variant.id, "quantity": 2},
        headers=_auth_headers(token),
    )
    # Stock drops below the cart's requested quantity after adding.
    db_session.query(ProductVariant).filter(ProductVariant.id == variant.id).update(
        {"stock_quantity": 1}
    )
    db_session.commit()

    response = client.post(
        ORDERS_PREFIX, params={"shipping_address_id": address_id}, headers=_auth_headers(token)
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
        params={"product_id": plain_product.id, "quantity": 2},
        headers=_auth_headers(token),
    )
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": variant_product.id, "variant_id": variant.id, "quantity": 3},
        headers=_auth_headers(token),
    )

    response = client.post(
        ORDERS_PREFIX, params={"shipping_address_id": address_id}, headers=_auth_headers(token)
    )

    assert response.status_code == 200
    body = response.json()["data"]
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
        params={"product_id": product.id, "variant_id": variant.id, "quantity": 2},
        headers=_auth_headers(token),
    )
    order = client.post(
        ORDERS_PREFIX, params={"shipping_address_id": address_id}, headers=_auth_headers(token)
    ).json()["data"]

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
            params={"product_id": product.id, "variant_id": variant.id, "quantity": 1},
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
            params={"shipping_address_id": address_id},
            headers=_auth_headers(token),
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
