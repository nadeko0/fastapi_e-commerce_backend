import re
from decimal import Decimal

import pytest
from fastapi.routing import APIRoute
from hypothesis import HealthCheck, given
from hypothesis import settings as settings_hypothesis
from hypothesis import strategies as st
from sqlalchemy import text

from app.api.v1.admin import router as admin_router
from app.core.config import settings
from app.core.security import get_password_hash
from app.models.address import Address
from app.models.category import Category
from app.models.enums import UserRole
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.product import Product, ProductVariant
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


def test_admin_variant_listing_requires_authentication(client, db_session):
    # Regression test (F-12): list_product_variants was the one route under
    # /admin without get_current_admin_user, so anyone could read every
    # variant of a product - inactive ones included - with SKUs and exact
    # stock levels.
    category = _make_category(db_session)
    product = Product(
        name="Widget", description="A fine widget for all your widget needs",
        price="9.99", stock_quantity=3, category_id=category.id, images=[],
    )
    db_session.add(product)
    db_session.commit()
    db_session.add(ProductVariant(product_id=product.id, sku="HIDDEN-SKU", is_active=False))
    db_session.commit()

    response = client.get(f"{ADMIN_PREFIX}/products/{product.id}/variants")

    assert response.status_code == 401
    assert "HIDDEN-SKU" not in response.text


def _admin_routes():
    """Every (method, path) the admin router serves, read from the router
    itself rather than listed by hand: a route added later is audited by the
    test below without anyone remembering to add it there."""
    return sorted(
        (method, route.path)
        for route in admin_router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    )


def _concrete_path(path):
    # Path parameters are only parsed after the auth dependencies have run,
    # so any value works; 1 keeps the URL well-formed.
    return re.sub(r"\{[^}]+\}", "1", path)


@pytest.mark.parametrize(("method", "path"), _admin_routes())
def test_every_admin_route_rejects_anonymous_callers(client, method, path):
    response = client.request(method, f"{settings.API_V1_STR}{_concrete_path(path)}")

    assert response.status_code == 401, f"{method} {path} answered {response.status_code}"


@pytest.mark.parametrize(("method", "path"), _admin_routes())
def test_every_admin_route_rejects_non_admin_users(client, method, path):
    user_token = _register_and_login(client, _valid_registration_payload())

    response = client.request(
        method,
        f"{settings.API_V1_STR}{_concrete_path(path)}",
        headers=_auth_headers(user_token),
    )

    assert response.status_code == 403, f"{method} {path} answered {response.status_code}"


def test_admin_route_audit_sees_every_route():
    # Guards the two tests above against auditing nothing (e.g. if the
    # router were wrapped and stopped exposing APIRoute objects).
    routes = _admin_routes()
    assert ("GET", "/admin/products/{product_id}/variants") in routes
    assert ("POST", "/admin/categories") in routes


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


# Every ProductUpdate field maps to a NOT NULL column. An explicit JSON null
# used to reach setattr() and end as a 500 (IntegrityError) - or, for the
# JSON columns (SQLite test DB), commit a JSON 'null' and then 500 while
# building the response.
@pytest.mark.parametrize(
    "field",
    ["name", "description", "price", "stock_quantity", "category_id", "images", "characteristics"],
)
def test_admin_update_product_rejects_explicit_null(client, db_session, field):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    created = _create_product(client, admin_token, category.id)

    response = client.put(
        f"{ADMIN_PREFIX}/products/{created['id']}",
        json={field: None},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 422
    db_session.expire_all()
    assert getattr(db_session.get(Product, created["id"]), field) is not None


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


ORDER_SORT_FIELDS = ("id", "created_at", "updated_at", "total_amount", "status", "payment_status")


# Regression test (F-25): sort_by used to accept any attribute of the Order
# class (hasattr), relationships and SQLAlchemy internals included. "user"
# and "items" are relationships and "metadata" is the declarative MetaData,
# none of which can be sorted on (a 500); idempotency_key is a real column
# but nobody should sort the admin list by an internal dedup key.
@pytest.mark.parametrize(
    "sort_by",
    ["user_asc", "items_desc", "metadata_asc", "shipping_address_desc",
     "idempotency_key_asc", "user_id_desc", "created_at", "created_at_sideways"],
)
def test_admin_list_orders_rejects_sort_fields_outside_the_whitelist(
    client, db_session, sort_by
):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")

    response = client.get(
        f"{ADMIN_PREFIX}/orders",
        params={"sort_by": sort_by},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 422, response.text


@pytest.mark.parametrize("field", ORDER_SORT_FIELDS)
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_admin_list_orders_accepts_every_whitelisted_sort_field(
    client, db_session, field, direction
):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")

    response = client.get(
        f"{ADMIN_PREFIX}/orders",
        params={"sort_by": f"{field}_{direction}"},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 200, response.text


@settings_hypothesis(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(sort_by=st.text(min_size=1, max_size=40))
def test_admin_list_orders_sort_by_is_whitelisted_or_422(client, db_session, sort_by):
    """Whatever the client sends, sort_by is either one of the whitelisted
    field_direction values (200) or rejected (422) - never a 500."""
    if not db_session.query(User).filter(User.email == "admin@example.com").first():
        _make_admin(db_session)
    admin_token = _login_as(client, "admin@example.com", "AdminPass1")

    response = client.get(
        f"{ADMIN_PREFIX}/orders",
        params={"sort_by": sort_by},
        headers=_auth_headers(admin_token),
    )

    allowed = {f"{f}_{d}" for f in ORDER_SORT_FIELDS for d in ("asc", "desc")}
    assert response.status_code == (200 if sort_by in allowed else 422), sort_by


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

    response = client.get(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        headers=_auth_headers(admin_token),
    )

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


# price_override is the one nullable variant column (NULL = inherit the
# parent product's price), so null must keep clearing it. The other fields
# are NOT NULL: null used to surface as a misleading 409 "SKU already
# exists" (the IntegrityError handler) or, for attributes, a 500.
@pytest.mark.parametrize("field", ["sku", "attributes", "stock_quantity", "is_active"])
def test_admin_update_variant_rejects_explicit_null(client, db_session, field):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)
    variant = client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": "VARIANT-N", "attributes": {"size": "M"}},
        headers=_auth_headers(admin_token),
    ).json()["data"]

    response = client.put(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants/{variant['id']}",
        json={field: None},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 422
    db_session.expire_all()
    assert getattr(db_session.get(ProductVariant, variant["id"]), field) is not None


def test_admin_update_variant_null_price_override_clears_it(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)
    variant = client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": "VARIANT-P", "price_override": "15.50"},
        headers=_auth_headers(admin_token),
    ).json()["data"]

    response = client.put(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants/{variant['id']}",
        json={"price_override": None},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 200
    assert response.json()["data"]["price_override"] is None


def _add_variant_constraint(db_session, event):
    """A database rule the request schemas know nothing about - what a CHECK
    constraint added by a later migration looks like to this code. SQLite
    reports RAISE(ABORT) from a trigger as an IntegrityError, exactly like a
    violated CHECK; the table (and with it the trigger) is dropped after
    every test."""
    db_session.execute(text(
        f"CREATE TRIGGER variant_stock_cap BEFORE {event} ON product_variants "
        "WHEN NEW.stock_quantity > 1000 "
        "BEGIN SELECT RAISE(ABORT, 'CHECK constraint failed: ck_variant_stock_cap'); END"
    ))
    db_session.commit()


def test_admin_update_variant_other_integrity_error_is_not_reported_as_sku_conflict(
    client, db_session
):
    # Regression test (X-4): every IntegrityError on commit was answered
    # 409 "A variant with this SKU already exists", whichever constraint
    # actually failed - here an update that changes nothing but the stock.
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)
    variant = client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": "VARIANT-X", "stock_quantity": 1},
        headers=_auth_headers(admin_token),
    ).json()["data"]
    _add_variant_constraint(db_session, "UPDATE")

    response = client.put(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants/{variant['id']}",
        json={"stock_quantity": 5000},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 422, response.text
    assert "SKU" not in response.text
    # The database's own message (constraint names, SQL) stays server-side.
    assert "ck_variant_stock_cap" not in response.text
    db_session.expire_all()
    assert db_session.get(ProductVariant, variant["id"]).stock_quantity == 1


def test_admin_update_variant_with_a_free_sku_and_another_integrity_error_is_not_409(
    client, db_session
):
    # The SKU changes too, but to one nobody holds: the failing constraint
    # is still not the SKU's.
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)
    variant = client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": "VARIANT-Y"},
        headers=_auth_headers(admin_token),
    ).json()["data"]
    _add_variant_constraint(db_session, "UPDATE")

    response = client.put(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants/{variant['id']}",
        json={"sku": "VARIANT-Y2", "stock_quantity": 5000},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 422, response.text
    db_session.expire_all()
    assert db_session.get(ProductVariant, variant["id"]).sku == "VARIANT-Y"


def test_admin_create_variant_other_integrity_error_is_not_reported_as_sku_conflict(
    client, db_session
):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)
    _add_variant_constraint(db_session, "INSERT")

    response = client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": "VARIANT-Z", "stock_quantity": 5000},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 422, response.text
    assert "SKU" not in response.text
    assert "ck_variant_stock_cap" not in response.text


def test_admin_update_variant_sku_race_returns_409(client, db_session, monkeypatch):
    # The genuine SKU conflict that only the unique constraint catches (the
    # pre-check forced to miss it, as in the create race test above) is
    # still a 409.
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)
    for sku in ("TAKEN-SKU", "OTHER-SKU"):
        client.post(
            f"{ADMIN_PREFIX}/products/{product['id']}/variants",
            json={"sku": sku},
            headers=_auth_headers(admin_token),
        )
    other = db_session.query(ProductVariant).filter(ProductVariant.sku == "OTHER-SKU").one()
    monkeypatch.setattr("app.api.v1.admin._variant_sku_exists", lambda db, sku: False)

    response = client.put(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants/{other.id}",
        json={"sku": "TAKEN-SKU"},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "A variant with this SKU already exists"


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


def test_admin_update_product_to_a_missing_category_is_404(client, db_session):
    # Regression test: create_product checks that category_id exists, but
    # update_product wrote any category_id straight to the row. On Postgres
    # the foreign key turns that into a generic 500; on a database that
    # does not enforce it (SQLite, as here) the commit succeeds and
    # building the response fails on the missing category instead.
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)

    response = client.put(
        f"{ADMIN_PREFIX}/products/{product['id']}",
        json={"category_id": 999999},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Category not found"
    db_session.expire_all()
    assert db_session.get(Product, product["id"]).category_id == category.id


def test_admin_moves_product_to_another_category(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    old = _make_category(db_session, name="Old Shelf")
    new = _make_category(db_session, name="New Shelf")
    product = _create_product(client, admin_token, old.id)

    response = client.put(
        f"{ADMIN_PREFIX}/products/{product['id']}",
        json={"category_id": new.id},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 200
    assert response.json()["data"]["category_id"] == new.id
    assert response.json()["data"]["category_name"] == "New Shelf"


# --- Catalog business rules -------------------------------------------------


def _order_line_for(db_session, product_id):
    """A real order line referencing the product, which is what makes
    delete_product keep the row (order history must survive)."""
    buyer = User(
        email="buyer@example.com", hashed_password="x", gdpr_consent=True,
        privacy_policy_accepted=True, consent_history=[],
    )
    db_session.add(buyer)
    db_session.commit()
    address = Address(
        user_id=buyer.id, street="1 Main St", city="Town", state="ST",
        postal_code="10001", country="US",
    )
    db_session.add(address)
    db_session.commit()
    order = Order(user_id=buyer.id, shipping_address_id=address.id, total_amount="19.99")
    db_session.add(order)
    db_session.commit()
    db_session.add(OrderItem(
        order_id=order.id, product_id=product_id, quantity=1, price_at_time="19.99",
    ))
    db_session.commit()
    return order


def test_admin_delete_product_with_orders_deactivates_instead_of_deleting(
    client, db_session
):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)
    order = _order_line_for(db_session, product["id"])

    response = client.delete(
        f"{ADMIN_PREFIX}/products/{product['id']}", headers=_auth_headers(admin_token)
    )

    assert response.status_code == 200
    db_session.expire_all()
    kept = db_session.get(Product, product["id"])
    assert kept is not None and kept.is_active is False
    assert db_session.query(OrderItem).filter(OrderItem.order_id == order.id).count() == 1
    # Gone from the storefront all the same.
    assert client.get(f"{CATALOG_PREFIX}/products/{product['id']}").status_code == 404
    listing = client.get(f"{CATALOG_PREFIX}/products").json()["data"]
    assert product["id"] not in [p["id"] for p in listing["items"]]


def test_admin_delete_product_drops_its_cached_copy(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)
    _order_line_for(db_session, product["id"])
    assert client.get(f"{CATALOG_PREFIX}/products/{product['id']}").status_code == 200

    client.delete(f"{ADMIN_PREFIX}/products/{product['id']}", headers=_auth_headers(admin_token))

    assert client.get(f"{CATALOG_PREFIX}/products/{product['id']}").status_code == 404


def test_admin_update_product_is_visible_through_the_cache(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)
    before = client.get(f"{CATALOG_PREFIX}/products/{product['id']}").json()["data"]
    assert before["price"] == "19.99"

    client.put(
        f"{ADMIN_PREFIX}/products/{product['id']}",
        json={"price": "25.00", "stock_quantity": 0},
        headers=_auth_headers(admin_token),
    )

    after = client.get(f"{CATALOG_PREFIX}/products/{product['id']}").json()["data"]
    assert (after["price"], after["stock_quantity"]) == ("25.00", 0)


def test_admin_create_category_shows_up_in_the_cached_tree(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    _make_category(db_session, name="Existing")
    first = client.get(f"{CATALOG_PREFIX}/categories/tree").json()["data"]

    client.post(
        f"{ADMIN_PREFIX}/categories", json={"name": "Brand New"},
        headers=_auth_headers(admin_token),
    )

    second = client.get(f"{CATALOG_PREFIX}/categories/tree").json()["data"]
    assert "Brand New" not in str(first)
    assert "Brand New" in str(second)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price", "-0.01"),
        ("stock_quantity", -1),
        ("name", "ab"),
        ("name", "x" * 201),
        ("description", "too short"),
        ("images", []),
        ("images", ["not a url"]),
        ("category_id", None),
    ],
)
def test_admin_create_product_rejects_invalid_fields(client, db_session, field, value):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    payload = {**_valid_product_payload(category.id), field: value}

    response = client.post(
        f"{ADMIN_PREFIX}/products", json=payload, headers=_auth_headers(admin_token)
    )

    assert response.status_code == 422
    assert db_session.query(Product).count() == 0


@pytest.mark.parametrize("payload", [{"name": "Kid", "parent_id": 0}, {"name": "K"}])
def test_admin_create_category_rejects_invalid_fields(client, db_session, payload):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")

    response = client.post(
        f"{ADMIN_PREFIX}/categories", json=payload, headers=_auth_headers(admin_token)
    )

    assert response.status_code == 422
    assert db_session.query(Category).count() == 0


@settings_hypothesis(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    price=st.decimals(min_value=0, max_value=10**7, places=2, allow_nan=False),
    stock=st.integers(min_value=0, max_value=10**6),
)
def test_admin_product_price_and_stock_round_trip_exactly(client, db_session, price, stock):
    """Money is a Decimal with two places all the way through: what the
    admin sends is exactly what is stored and served back, never a float
    approximation."""
    if not db_session.query(User).filter(User.email == "admin@example.com").first():
        _make_admin(db_session)
    if not db_session.query(Category).first():
        _make_category(db_session)
    category_id = db_session.query(Category.id).first()[0]
    admin_token = _login_as(client, "admin@example.com", "AdminPass1")
    payload = {
        **_valid_product_payload(category_id), "price": str(price), "stock_quantity": stock,
    }

    created = client.post(
        f"{ADMIN_PREFIX}/products", json=payload, headers=_auth_headers(admin_token)
    ).json()["data"]

    assert Decimal(created["price"]) == price
    assert created["stock_quantity"] == stock
    db_session.expire_all()
    stored = db_session.get(Product, created["id"])
    assert (stored.price, stored.stock_quantity) == (price, stock)


def test_admin_stock_edit_to_negative_is_rejected_and_leaves_stock(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)

    response = client.put(
        f"{ADMIN_PREFIX}/products/{product['id']}",
        json={"stock_quantity": -5},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 422
    db_session.expire_all()
    assert db_session.get(Product, product["id"]).stock_quantity == 15


@pytest.mark.parametrize(
    "payload",
    [
        {"sku": ""},
        {"sku": "S" * 101},
        {"sku": "NEG-STOCK", "stock_quantity": -1},
        {"sku": "NEG-PRICE", "price_override": "-1.00"},
    ],
)
def test_admin_create_variant_rejects_invalid_fields(client, db_session, payload):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)

    response = client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json=payload,
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 422
    assert db_session.query(ProductVariant).count() == 0


def test_admin_variant_price_override_is_rounded_to_cents(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)

    response = client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": "ROUNDED", "price_override": "9.999"},
        headers=_auth_headers(admin_token),
    )

    assert response.json()["data"]["price_override"] == "10.00"


def test_admin_update_variant_to_a_sku_another_variant_holds_is_409(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)
    for sku in ("HELD-SKU", "MOVER-SKU"):
        client.post(
            f"{ADMIN_PREFIX}/products/{product['id']}/variants",
            json={"sku": sku},
            headers=_auth_headers(admin_token),
        )
    mover = db_session.query(ProductVariant).filter(ProductVariant.sku == "MOVER-SKU").one()

    response = client.put(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants/{mover.id}",
        json={"sku": "HELD-SKU"},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 409
    db_session.expire_all()
    assert db_session.get(ProductVariant, mover.id).sku == "MOVER-SKU"


def test_admin_update_variant_resending_its_own_sku_is_not_a_conflict(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)
    variant = client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": "SAME-SKU"},
        headers=_auth_headers(admin_token),
    ).json()["data"]

    response = client.put(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants/{variant['id']}",
        json={"sku": "SAME-SKU", "is_active": False},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 200
    assert response.json()["data"]["is_active"] is False


def test_admin_variant_of_another_product_is_not_found_through_this_one(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    owner = _create_product(client, admin_token, category.id)
    other = _create_product(client, admin_token, category.id)
    variant = client.post(
        f"{ADMIN_PREFIX}/products/{owner['id']}/variants",
        json={"sku": "OWNED-SKU", "stock_quantity": 2},
        headers=_auth_headers(admin_token),
    ).json()["data"]

    response = client.put(
        f"{ADMIN_PREFIX}/products/{other['id']}/variants/{variant['id']}",
        json={"stock_quantity": 99},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 404
    db_session.expire_all()
    assert db_session.get(ProductVariant, variant["id"]).stock_quantity == 2


def test_admin_variant_listing_shows_inactive_variants_in_creation_order(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    category = _make_category(db_session)
    product = _create_product(client, admin_token, category.id)
    for sku, active in (("V-1", True), ("V-2", False), ("V-3", True)):
        client.post(
            f"{ADMIN_PREFIX}/products/{product['id']}/variants",
            json={"sku": sku, "is_active": active},
            headers=_auth_headers(admin_token),
        )

    body = client.get(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        headers=_auth_headers(admin_token),
    ).json()["data"]

    assert [(v["sku"], v["is_active"]) for v in body["items"]] == [
        ("V-1", True), ("V-2", False), ("V-3", True),
    ]
    assert body["total"] == 3


def test_admin_variant_listing_for_a_missing_product_is_404(client, db_session):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")

    response = client.get(
        f"{ADMIN_PREFIX}/products/999999/variants", headers=_auth_headers(admin_token)
    )

    assert response.status_code == 404
