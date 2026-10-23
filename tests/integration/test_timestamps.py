"""Timestamps in API responses are RFC 3339 date-times with an offset.

RFC 3339 (section 5.6, date-time) requires the offset, and OpenAPI's
`format: date-time` means RFC 3339. Pydantic renders aware UTC as "Z".
"""
import json
import re
import uuid

import pytest
from payment_support import Shop, reset_provider

from app.core.clock import utc_now
from app.main import app
from app.models.payment import WebhookEvent

# date-time = full-date "T" full-time; full-time ends with Z or +hh:mm / -hh:mm.
RFC_3339_DATE_TIME = re.compile(
    r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])"
    r"T([01]\d|2[0-3]):[0-5]\d:([0-5]\d|60)(\.\d+)?"
    r"(Z|[+-]([01]\d|2[0-3]):[0-5]\d)$"
)
ANY_DATE_TIME_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _timestamps(node, path=""):
    """Every string in a JSON document that looks like a date-time."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _timestamps(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _timestamps(value, f"{path}[{i}]")
    elif isinstance(node, str) and ANY_DATE_TIME_LIKE.match(node):
        yield path, node


def _assert_rfc_3339(document):
    found = list(_timestamps(document))
    assert found, "the response carries no timestamp, the test checks nothing"
    for path, value in found:
        assert RFC_3339_DATE_TIME.match(value), f"{path} = {value!r} is not an RFC 3339 date-time"
    return found


def _names(found):
    return {path.rsplit(".", 1)[-1] for path, _ in found}


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def shop(client, db_session, monkeypatch):
    return Shop(client, db_session)


def test_an_order_carries_offset_timestamps(shop):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())

    found = _assert_rfc_3339(order)
    assert "created_at" in _names(found)
    assert all(value.endswith("Z") for _, value in found)
    _assert_rfc_3339(shop.get_order(buyer, order["id"]).json())
    _assert_rfc_3339(shop.list_orders(buyer).json())


def test_the_profile_carries_offset_timestamps(shop):
    buyer = shop.buyer()

    response = shop.client.get("/api/v1/users/me", headers=buyer.headers)

    assert response.status_code == 200
    assert "created_at" in _names(_assert_rfc_3339(response.json()))


def test_an_admin_webhook_event_carries_offset_timestamps(shop):
    admin = shop.admin()
    now = utc_now()
    shop.db.add(WebhookEvent(
        event_id=f"evt_{uuid.uuid4().hex[:24]}", event_type="payment_intent.succeeded",
        status="processed", payload=json.dumps({}), processed_at=now,
        created_at=now, updated_at=now))
    shop.db.commit()

    response = shop.client.get("/api/v1/admin/webhook-events", headers=admin)

    assert response.status_code == 200
    assert {"processed_at", "created_at", "updated_at"} <= _names(
        _assert_rfc_3339(response.json()))


def test_the_cart_and_the_legal_documents_carry_offset_timestamps(shop):
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, shop.product()).status_code == 200

    cart = shop.client.get("/api/v1/cart", headers=buyer.headers)
    legal = shop.client.get("/api/v1/legal/privacy-policy")

    assert cart.status_code == 200 and legal.status_code == 200
    _assert_rfc_3339(cart.json())
    _assert_rfc_3339(legal.json())


def test_openapi_declares_these_fields_as_date_time():
    schemas = app.openapi()["components"]["schemas"]
    expected = {
        "OrderResponse": ["created_at", "updated_at"],
        "UserResponse": ["created_at"],
        "WebhookEventResponse": ["created_at", "updated_at", "processed_at"],
    }
    for name, fields in expected.items():
        assert name in schemas, f"{name} is not in the OpenAPI schema"
        for field in fields:
            prop = schemas[name]["properties"][field]
            variants = prop.get("anyOf", [prop])
            assert any(v.get("format") == "date-time" for v in variants), (name, field, prop)
