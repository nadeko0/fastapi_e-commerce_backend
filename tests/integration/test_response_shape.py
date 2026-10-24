"""A successful response is the resource itself, with no envelope around it.

The body of a 2xx is the thing the route is about, at the top level:
- a single resource has its own fields (no `success`, `data`, `timestamp`,
  `request_id`, `error` wrapper);
- a list is a page object (`items`, `total`, `page`, `size`, `has_more`), or
  an object with a named array when the list is not paginated; never a bare
  JSON array;
- a route that only confirms something answers `{"message": ...}`.
The request id travels in the X-Request-ID header.

Google AIP-131 ("the response message must be the resource itself"),
Zalando guidelines #110 (top-level JSON object) and #248 (page object with
items), the list object of https://docs.stripe.com/api/pagination.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.security import create_access_token, get_password_hash
from app.main import app
from app.models.address import Address
from app.models.category import Category
from app.models.enums import UserRole
from app.models.order import Order
from app.models.product import Product
from app.models.user import User

API = "/api/v1"
ENVELOPE_KEYS = {"success", "data", "timestamp", "request_id", "error"}
PAGE_KEYS = {"items", "total", "page", "size", "has_more"}


def _user(db_session, email, role=UserRole.CLIENT):
    user = User(
        email=email, hashed_password=get_password_hash("Str0ngPass1"), full_name="Someone",
        role=role, gdpr_consent=True, privacy_policy_accepted=True, consent_history=[],
        is_active=True, is_email_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    return user, {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest.fixture
def buyer(db_session):
    return _user(db_session, "buyer@example.com")


@pytest.fixture
def admin_headers(db_session):
    return _user(db_session, "admin@example.com", role=UserRole.ADMIN)[1]


@pytest.fixture
def product(db_session):
    category = Category(name="Shapes", level=0, path=[])
    db_session.add(category)
    db_session.commit()
    row = Product(
        name="Cube", description="A cube of ten sides", price=Decimal("10.00"), stock_quantity=5,
        images=["https://example.com/p.png"], characteristics={}, category_id=category.id,
    )
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture
def address(db_session, buyer):
    row = Address(user_id=buyer[0].id, street="1 Main St", city="Town", state="ST",
                  postal_code="10001", country="US")
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture
def orders(db_session, buyer, address):
    """Five orders of the buyer, one hour apart."""
    start = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
    rows = [
        Order(user_id=buyer[0].id, total_amount=Decimal("10.00"), shipping_address_id=address.id,
              created_at=start + timedelta(hours=i), updated_at=start + timedelta(hours=i))
        for i in range(5)
    ]
    db_session.add_all(rows)
    db_session.commit()
    return rows


def _ok(response):
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict), "a top-level JSON object, never a bare array"
    assert not ENVELOPE_KEYS & body.keys(), sorted(ENVELOPE_KEYS & body.keys())
    assert response.headers["X-Request-ID"]
    return body


# -- A single resource ---------------------------------------------------------------

def test_users_me_is_the_user(client, buyer):
    body = _ok(client.get(f"{API}/users/me", headers=buyer[1]))

    assert body["email"] == "buyer@example.com"
    assert body["id"] == buyer[0].id


def test_product_is_the_product_on_the_database_path_and_the_cache_path(client, product):
    first = _ok(client.get(f"{API}/products/{product.id}"))
    second = _ok(client.get(f"{API}/products/{product.id}"))

    assert first["name"] == "Cube"
    assert first == second


def test_category_is_the_category(client, product):
    body = _ok(client.get(f"{API}/categories/{product.category_id}"))

    assert body["name"] == "Shapes"


def test_order_is_the_order(client, buyer, orders):
    body = _ok(client.get(f"{API}/orders/{orders[0].id}", headers=buyer[1]))

    assert body["id"] == orders[0].id
    assert body["total_amount"] == "10.00"


def test_cart_is_the_cart(client, buyer):
    body = _ok(client.get(f"{API}/cart", headers=buyer[1]))

    assert body["items"] == []
    assert body["items_count"] == 0


def test_legal_document_is_the_document(client):
    body = _ok(client.get(f"{API}/legal/privacy-policy"))

    assert "content" in body


def test_admin_stats_are_the_statistics(client, admin_headers):
    body = _ok(client.get(f"{API}/admin/stats", headers=admin_headers))

    assert body["period"] == "24h"
    assert body["order_count"] == 0
    assert body["popular_products"] == []


def test_login_is_the_token_pair(client, buyer):
    body = _ok(client.post(
        f"{API}/users/login", data={"username": "buyer@example.com", "password": "Str0ngPass1"}))

    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]


# -- Lists are page objects ----------------------------------------------------------

def test_products_are_a_page_object(client, product):
    body = _ok(client.get(f"{API}/products"))

    assert body.keys() == PAGE_KEYS
    assert body["items"][0]["name"] == "Cube"


def test_categories_are_a_page_object(client, product):
    assert _ok(client.get(f"{API}/categories")).keys() == PAGE_KEYS


def test_addresses_are_a_page_object(client, buyer, address):
    body = _ok(client.get(f"{API}/users/addresses", headers=buyer[1]))

    assert PAGE_KEYS <= body.keys()
    assert body["items"][0]["street"] == "1 Main St"


def test_admin_order_list_is_a_page_object(client, admin_headers, orders):
    body = _ok(client.get(f"{API}/admin/orders", headers=admin_headers))

    assert body.keys() == PAGE_KEYS
    assert body["total"] == 5


def test_admin_discrepancies_are_a_page_object(client, admin_headers):
    body = _ok(client.get(f"{API}/admin/reconciliation/discrepancies", headers=admin_headers))

    assert body == {"items": [], "total": 0, "page": 1, "size": 20, "has_more": False}


def test_user_orders_are_a_page_object(client, buyer, orders):
    body = _ok(client.get(f"{API}/users/orders", headers=buyer[1]))

    assert body.keys() == PAGE_KEYS
    assert body["total"] == 5
    assert (body["page"], body["size"], body["has_more"]) == (1, 20, False)
    assert [o["id"] for o in body["items"]] == [o.id for o in reversed(orders)]


def test_user_orders_honour_page_and_size(client, buyer, orders):
    newest_first = [o.id for o in reversed(orders)]

    second = _ok(client.get(f"{API}/users/orders", params={"page": 2, "size": 2}, headers=buyer[1]))
    last = _ok(client.get(f"{API}/users/orders", params={"page": 3, "size": 2}, headers=buyer[1]))
    beyond = _ok(client.get(f"{API}/users/orders", params={"page": 4, "size": 2}, headers=buyer[1]))

    assert [o["id"] for o in second["items"]] == newest_first[2:4]
    assert (second["total"], second["page"], second["size"], second["has_more"]) == (5, 2, 2, True)
    assert [o["id"] for o in last["items"]] == newest_first[4:]
    assert last["has_more"] is False
    assert beyond["items"] == []
    assert beyond["total"] == 5


def test_user_orders_of_a_customer_without_orders_are_an_empty_page(client, buyer):
    body = _ok(client.get(f"{API}/users/orders", headers=buyer[1]))

    assert body == {"items": [], "total": 0, "page": 1, "size": 20, "has_more": False}


def test_a_list_that_is_not_paginated_is_an_object_with_a_named_array(client, admin_headers, orders):
    body = _ok(client.get(f"{API}/orders/{orders[0].id}/payments", headers=admin_headers))

    assert body == {"items": []}


# -- A route that only confirms ------------------------------------------------------

def test_logout_answers_with_a_message(client, buyer):
    body = _ok(client.post(f"{API}/users/logout", headers=buyer[1]))

    assert body == {"message": "Logged out successfully"}


def test_password_reset_request_answers_with_a_message(client):
    body = _ok(client.post(f"{API}/users/password/reset", json={"email": "nobody@example.com"}))

    assert body == {"message": "If the email exists, a password reset link will be sent"}


def test_deleting_an_address_answers_with_a_message(client, buyer, address):
    body = _ok(client.delete(f"{API}/users/addresses/{address.id}", headers=buyer[1]))

    assert body == {"message": "Address deleted successfully"}


def test_data_retention_is_an_object_with_named_fields(client, buyer):
    body = _ok(client.get(f"{API}/legal/data-retention", headers=buyer[1]))

    assert body.keys() == {"within_retention_period", "retention_days"}


# -- The documentation says the same -------------------------------------------------

def test_openapi_has_no_envelope_schema():
    names = app.openapi()["components"]["schemas"]

    assert [name for name in names if name.startswith("APIResponse")] == []


def test_every_success_response_documents_an_object_schema():
    schemas = app.openapi()["components"]["schemas"]
    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            for code, response in operation["responses"].items():
                schema = response.get("content", {}).get("application/json", {}).get("schema")
                if not code.startswith("2") or not schema or "$ref" not in schema:
                    continue
                target = schemas[schema["$ref"].rsplit("/", 1)[1]]
                assert target.get("type") == "object", (method, path, code, schema)
