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


def test_admin_creates_grandchild_category_with_correct_level_and_path(client, db_session):
    # Verifies level/path computation walks correctly beyond a single
    # parent->child hop: a grandchild's path must be [root.id, child.id],
    # not just [child.id] or a repeat of the child's own path.
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    root = _make_category(db_session, name="Root")

    child_response = client.post(
        f"{ADMIN_PREFIX}/categories",
        json={"name": "Child", "parent_id": root.id},
        headers=_auth_headers(admin_token),
    )
    assert child_response.status_code == 200
    child = child_response.json()["data"]
    assert child["level"] == 1
    assert child["path"] == [root.id]

    grandchild_response = client.post(
        f"{ADMIN_PREFIX}/categories",
        json={"name": "Grandchild", "parent_id": child["id"]},
        headers=_auth_headers(admin_token),
    )
    assert grandchild_response.status_code == 200
    grandchild = grandchild_response.json()["data"]
    assert grandchild["level"] == 2
    assert grandchild["path"] == [root.id, child["id"]]


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
    # Regression test: this endpoint used to return a bare list with no
    # pagination metadata at all (total/page/size/has_more were computed
    # and then silently discarded) - now wraps in OrderListResponse like
    # every other list endpoint in this API.
    data = response.json()["data"]
    assert data["items"] == []
    assert data["total"] == 0
    assert data["has_more"] is False


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


# --- Admin product variant CRUD -------------------------------------------
#
# ProductVariant is additive data model + CRUD only - not wired into
# cart/checkout stock logic (see app/models/product.py docstring).


def _create_product(client, admin_token, category_id):
    return client.post(
        f"{ADMIN_PREFIX}/products",
        json=_valid_product_payload(category_id),
        headers=_auth_headers(admin_token),
    ).json()["data"]


def test_admin_creates_product_variant(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)

    response = client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={
            "sku": "WIDGET-RED-M",
            "attributes": {"size": "M", "color": "red"},
            "stock_quantity": 5,
        },
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["sku"] == "WIDGET-RED-M"
    assert body["attributes"] == {"size": "M", "color": "red"}
    assert body["stock_quantity"] == 5
    assert body["price_override"] is None
    assert body["product_id"] == product["id"]


def test_admin_create_variant_rejects_missing_product(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")

    response = client.post(
        f"{ADMIN_PREFIX}/products/999999/variants",
        json={"sku": "GHOST-SKU"},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 404


def test_admin_create_variant_rejects_duplicate_sku(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)

    client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": "DUP-SKU"},
        headers=_auth_headers(admin_token),
    )
    response = client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": "DUP-SKU"},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 409


def test_admin_create_variant_race_returns_409_not_500(client, db_session, monkeypatch):
    # Regression test: create_product_variant's SKU-uniqueness check is a
    # plain read-then-write (query for an existing SKU, then insert) with no
    # row locking - two concurrent requests for the same never-before-seen
    # SKU can both pass the pre-check before either commits, leaving the
    # DB's unique constraint on ProductVariant.sku as the real guarantee.
    # Before this fix, the loser's IntegrityError from that constraint was
    # uncaught in the endpoint: still caught safely upstream by
    # app.core.database.get_db's session_scope (so no raw SQL ever leaked to
    # the client), but surfaced as a generic 500 instead of the same 409 the
    # sequential-duplicate case already returns.
    #
    # Forcing genuine thread interleaving against this test suite's single
    # shared SQLite connection (StaticPool) is unreliable (see the identical
    # note in test_payment.py's webhook concurrency test), so the race
    # window is instead forced deterministically: monkeypatch the pre-check
    # helper to report "no conflict" on the second call despite a real
    # conflicting row already committed, exercising exactly the code path a
    # true race would hit - the commit-time IntegrityError catch.
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)

    first = client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": "RACE-SKU"},
        headers=_auth_headers(admin_token),
    )
    assert first.status_code == 200

    monkeypatch.setattr("app.api.v1.admin._variant_sku_exists", lambda db, sku: False)

    second = client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": "RACE-SKU"},
        headers=_auth_headers(admin_token),
    )
    assert second.status_code == 409


def test_admin_lists_product_variants(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)
    client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": "VARIANT-A"},
        headers=_auth_headers(admin_token),
    )
    client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": "VARIANT-B"},
        headers=_auth_headers(admin_token),
    )

    response = client.get(f"{ADMIN_PREFIX}/products/{product['id']}/variants")

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["total"] == 2
    assert {v["sku"] for v in body["items"]} == {"VARIANT-A", "VARIANT-B"}


def test_admin_updates_product_variant(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)
    variant = client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": "VARIANT-C", "stock_quantity": 1},
        headers=_auth_headers(admin_token),
    ).json()["data"]

    response = client.put(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants/{variant['id']}",
        json={"stock_quantity": 20, "price_override": "15.50"},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["stock_quantity"] == 20
    assert body["price_override"] == "15.50"


def test_admin_update_variant_returns_404_for_missing_variant(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)

    response = client.put(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants/999999",
        json={"stock_quantity": 5},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 404


def test_admin_variant_endpoints_require_admin(client, db_session):
    category = _make_category(db_session)
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    product = _create_product(client, admin_token, category.id)
    user_token = _register_and_login(client, _valid_registration_payload())

    response = client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": "SNEAKY-SKU"},
        headers=_auth_headers(user_token),
    )

    assert response.status_code == 403
