from app.core.security import get_password_hash
from app.models.category import Category
from app.models.enums import UserRole
from app.models.product import Product
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
