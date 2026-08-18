from app.core.security import get_password_hash
from app.models.category import Category
from app.models.enums import UserRole
from app.models.user import User

USERS_PREFIX = "/api/v1/users"
ADMIN_PREFIX = "/api/v1/admin"
CATALOG_PREFIX = "/api/v1"


def _valid_registration_payload(email="regular.user@example.com"):
    return {
        "email": email,
        "password": "Str0ngPass1",
        "full_name": "Regular User",
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


def _login_as(client, email, password):
    login_response = client.post(
        f"{USERS_PREFIX}/login",
        data={"username": email, "password": password},
    )
    return login_response.json()["data"]["access_token"]


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _make_category(db_session, name="Electronics"):
    category = Category(name=name, parent_id=None, level=0, path=[])
    db_session.add(category)
    db_session.commit()
    db_session.refresh(category)
    return category


def _valid_product_payload(category_id):
    return {
        "name": "Test Widget",
        "description": "A fine widget for all your widget needs",
        "price": "19.99",
        "stock_quantity": 15,
        "category_id": category_id,
        "images": ["https://example.com/widget.png"],
        "characteristics": {},
    }


# --- Non-admin is forbidden, not merely unauthenticated -----------------


def test_non_admin_cannot_create_category(client, db_session):
    _make_admin(db_session)  # ensures the DB has an admin so no ambiguity
    user_token = _register_and_login(client, _valid_registration_payload())

    response = client.post(
        f"{ADMIN_PREFIX}/categories",
        json={"name": "Sneaky Category"},
        headers=_auth_headers(user_token),
    )

    assert response.status_code == 403


def test_non_admin_cannot_create_product(client, db_session):
    category = _make_category(db_session)
    user_token = _register_and_login(client, _valid_registration_payload())

    response = client.post(
        f"{ADMIN_PREFIX}/products",
        json=_valid_product_payload(category.id),
        headers=_auth_headers(user_token),
    )

    assert response.status_code == 403


def test_admin_endpoint_requires_authentication(client):
    response = client.post(f"{ADMIN_PREFIX}/categories", json={"name": "No Auth"})

    assert response.status_code == 401


# --- Admin category CRUD --------------------------------------------------


def test_admin_creates_category(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")

    response = client.post(
        f"{ADMIN_PREFIX}/categories",
        json={"name": "New Category"},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 200
    assert response.json()["data"]["name"] == "New Category"
    assert response.json()["data"]["level"] == 0


def test_admin_creates_subcategory_with_correct_level_and_path(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    parent = _make_category(db_session, name="Parent")

    response = client.post(
        f"{ADMIN_PREFIX}/categories",
        json={"name": "Child", "parent_id": parent.id},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["level"] == 1
    assert body["path"] == [parent.id]


def test_admin_create_category_rejects_missing_parent(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")

    response = client.post(
        f"{ADMIN_PREFIX}/categories",
        json={"name": "Orphan", "parent_id": 999999},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 404


# --- Admin product CRUD --------------------------------------------------


def test_admin_creates_product(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)

    response = client.post(
        f"{ADMIN_PREFIX}/products",
        json=_valid_product_payload(category.id),
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["name"] == "Test Widget"
    assert body["category_name"] == category.name


def test_admin_create_product_rejects_missing_category(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")

    response = client.post(
        f"{ADMIN_PREFIX}/products",
        json=_valid_product_payload(999999),
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 404


def test_admin_updates_product(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    created = client.post(
        f"{ADMIN_PREFIX}/products",
        json=_valid_product_payload(category.id),
        headers=_auth_headers(admin_token),
    ).json()["data"]

    response = client.put(
        f"{ADMIN_PREFIX}/products/{created['id']}",
        json={"stock_quantity": 42},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 200
    assert response.json()["data"]["stock_quantity"] == 42


def test_admin_update_nonexistent_product_returns_404(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")

    response = client.put(
        f"{ADMIN_PREFIX}/products/999999",
        json={"stock_quantity": 5},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 404


def test_admin_deletes_product_with_no_orders(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    created = client.post(
        f"{ADMIN_PREFIX}/products",
        json=_valid_product_payload(category.id),
        headers=_auth_headers(admin_token),
    ).json()["data"]

    response = client.delete(
        f"{ADMIN_PREFIX}/products/{created['id']}",
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 200

    get_response = client.get(f"{CATALOG_PREFIX}/products/{created['id']}")
    assert get_response.status_code == 404


def test_admin_delete_nonexistent_product_returns_404(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")

    response = client.delete(
        f"{ADMIN_PREFIX}/products/999999",
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 404


def test_admin_lists_orders_requires_admin(client, db_session):
    user_token = _register_and_login(client, _valid_registration_payload())

    response = client.get(f"{ADMIN_PREFIX}/orders", headers=_auth_headers(user_token))

    assert response.status_code == 403


def test_admin_lists_orders_returns_empty_when_none_exist(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")

    response = client.get(f"{ADMIN_PREFIX}/orders", headers=_auth_headers(admin_token))

    assert response.status_code == 200
    assert response.json()["data"] == []


def test_admin_stats_requires_admin(client, db_session):
    user_token = _register_and_login(client, _valid_registration_payload())

    response = client.get(f"{ADMIN_PREFIX}/stats", headers=_auth_headers(user_token))

    assert response.status_code == 403


def test_admin_stats_returns_zeroed_stats_when_no_orders(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")

    response = client.get(f"{ADMIN_PREFIX}/stats", headers=_auth_headers(admin_token))

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["order_count"] == 0
    assert body["total_revenue"] == 0
