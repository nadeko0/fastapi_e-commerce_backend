"""Who may call the account and legal routes, and the address book and
order list behind them.

The access matrix is built from the OpenAPI document, so a route added
under /users or /legal with a bearer requirement is covered without being
listed here.
"""

import re
import time
import uuid
from decimal import Decimal

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.errors import ProblemType
from app.core.security import get_password_hash
from app.main import app
from app.models.address import Address
from app.models.enums import UserRole
from app.models.order import Order
from app.models.user import User

USERS = f"{settings.API_V1_STR}/users"
PASSWORD = "Str0ngPass1"


def _protected_operations():
    operations = []
    for path, methods in app.openapi()["paths"].items():
        if not path.startswith((USERS, f"{settings.API_V1_STR}/legal")):
            continue
        for method, operation in methods.items():
            if operation.get("security"):
                operations.append((method.upper(), re.sub(r"\{[^}]+\}", "1", path)))
    return sorted(operations)


PROTECTED = _protected_operations()


def _call(client, method, path, headers=None):
    return client.request(method, path, headers=headers or {}, json={})


def _register(client, email="owner@example.com"):
    client.post(f"{USERS}/register", json={
        "email": email, "password": PASSWORD, "full_name": "Route Tester",
        "gdpr_consent": True, "privacy_policy_accepted": True,
    })


def _tokens(client, email="owner@example.com"):
    _register(client, email)
    response = client.post(f"{USERS}/login", data={"username": email, "password": PASSWORD})
    return response.json()


def _auth(client, email="owner@example.com"):
    return {"Authorization": f"Bearer {_tokens(client, email)['access_token']}"}


def test_the_matrix_covers_the_routes_it_should():
    # A guard on the guard: if the OpenAPI document stopped marking these
    # routes, every test below would pass over an empty list.
    assert ("GET", f"{USERS}/me") in PROTECTED
    assert ("POST", f"{settings.API_V1_STR}/legal/data-request") in PROTECTED
    assert ("POST", f"{USERS}/logout") in PROTECTED
    assert len(PROTECTED) >= 16
    assert ("POST", f"{USERS}/login") not in PROTECTED


@pytest.mark.parametrize("method,path", PROTECTED)
def test_an_anonymous_caller_is_401(client, method, path):
    response = _call(client, method, path)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("method,path", PROTECTED)
def test_a_refresh_token_is_not_an_access_token_anywhere(client, method, path):
    refresh = _tokens(client)["refresh_token"]

    assert _call(client, method, path, {"Authorization": f"Bearer {refresh}"}).status_code == 401


@pytest.mark.parametrize("method,path", PROTECTED)
def test_a_token_for_another_application_is_401(client, method, path):
    foreign = jwt.encode(
        {"sub": "1", "type": "access", "exp": int(time.time()) + 600},
        "somebody-else's-key-of-32-bytes!",
        algorithm="HS256",
    )

    assert _call(client, method, path, {"Authorization": f"Bearer {foreign}"}).status_code == 401


@pytest.mark.parametrize("method,path", PROTECTED)
def test_a_deactivated_account_is_403_account_inactive_everywhere(client, db_session, method, path):
    headers = _auth(client)
    db_session.query(User).update({User.is_active: False})
    db_session.commit()

    response = _call(client, method, path, headers)

    assert response.status_code == 403
    assert response.json()["type"] == ProblemType.ACCOUNT_INACTIVE
    assert response.json()["detail"] == "Inactive user"


def test_an_admin_uses_the_account_routes_as_a_user_too(client, db_session):
    db_session.add(User(
        email="admin@example.com", hashed_password=get_password_hash(PASSWORD),
        role=UserRole.ADMIN, gdpr_consent=True, privacy_policy_accepted=True,
    ))
    db_session.commit()
    response = client.post(f"{USERS}/login", data={"username": "admin@example.com", "password": PASSWORD})
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    me = client.get(f"{USERS}/me", headers=headers)

    assert me.status_code == 200
    assert me.json()["role"] == "admin"


def test_a_client_cannot_reach_the_admin_router(client):
    assert client.get(f"{settings.API_V1_STR}/admin/stats", headers=_auth(client)).status_code == 403


# --- the address book ------------------------------------------------------


def _address(**overrides):
    return {
        "street": f"{uuid.uuid4().hex[:6]} Main Street", "city": "Springfield",
        "state": "Illinois", "postal_code": "62701", "country": "US", **overrides,
    }


def _create(client, headers, **overrides):
    response = client.post(f"{USERS}/addresses", json=_address(**overrides), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _defaults(client, headers):
    items = client.get(f"{USERS}/addresses", headers=headers).json()["items"]
    return [item["id"] for item in items if item["is_default"]]


def test_a_new_default_address_takes_over_from_the_old_one(client):
    headers = _auth(client)
    first = _create(client, headers)
    second = _create(client, headers, is_default=True)

    assert _defaults(client, headers) == [second["id"]]
    assert first["is_default"] is True


def test_making_an_address_default_by_update_unsets_the_old_default(client):
    headers = _auth(client)
    first = _create(client, headers)
    second = _create(client, headers)

    client.put(f"{USERS}/addresses/{second['id']}", json={"is_default": True}, headers=headers)

    assert _defaults(client, headers) == [second["id"]]
    assert first["id"] != second["id"]


def test_the_same_address_twice_is_409(client):
    headers = _auth(client)
    fields = _address()
    assert client.post(f"{USERS}/addresses", json=fields, headers=headers).status_code == 200

    again = client.post(f"{USERS}/addresses", json=fields, headers=headers)

    assert again.status_code == 409


def test_a_deleted_address_can_be_neither_changed_nor_deleted_nor_made_default(client):
    headers = _auth(client)
    address = _create(client, headers)
    assert client.delete(f"{USERS}/addresses/{address['id']}", headers=headers).status_code == 204

    assert client.put(
        f"{USERS}/addresses/{address['id']}", json={"city": "Elsewhere"}, headers=headers
    ).status_code == 409
    # Gone for the client: a second DELETE is a 404, not a conflict.
    assert client.delete(f"{USERS}/addresses/{address['id']}", headers=headers).status_code == 404
    assert client.post(
        f"{USERS}/addresses/default", json={"address_id": address["id"]}, headers=headers
    ).status_code == 409


def test_an_unknown_or_foreign_address_cannot_be_made_default(client):
    mine = _auth(client)
    theirs = _auth(client, "other@example.com")
    foreign = _create(client, theirs)

    for address_id in (foreign["id"], 999999):
        response = client.post(
            f"{USERS}/addresses/default", json={"address_id": address_id}, headers=mine
        )
        assert response.status_code == 404
    assert _defaults(client, theirs) == [foreign["id"]]


def test_a_foreign_address_cannot_be_updated(client):
    theirs = _auth(client, "other@example.com")
    foreign = _create(client, theirs)

    response = client.put(
        f"{USERS}/addresses/{foreign['id']}", json={"city": "Hijacked"}, headers=_auth(client)
    )

    assert response.status_code == 404


def test_the_address_list_is_paginated(client):
    headers = _auth(client)
    for _ in range(3):
        _create(client, headers)

    page = client.get(f"{USERS}/addresses", params={"page": 2, "size": 2}, headers=headers)

    data = page.json()
    assert (data["total"], len(data["items"]), data["has_more"]) == (3, 1, False)


# --- the order list --------------------------------------------------------


def _order(db_session, user_id, created_at):
    address = Address(
        user_id=user_id, street=f"{uuid.uuid4().hex[:6]} Road", city="Springfield",
        state="Illinois", postal_code="62701", country="US",
    )
    db_session.add(address)
    db_session.commit()
    order = Order(
        user_id=user_id, status="new", payment_status="pending",
        total_amount=Decimal("10.00"), created_at=created_at, shipping_address_id=address.id,
    )
    db_session.add(order)
    db_session.commit()
    return order.id


def test_the_order_list_shows_only_ones_own_orders_newest_first(client, db_session):
    from datetime import UTC, datetime

    headers = _auth(client)
    _register(client, "other@example.com")
    me = db_session.query(User).filter(User.email == "owner@example.com").one()
    other = db_session.query(User).filter(User.email == "other@example.com").one()
    older = _order(db_session, me.id, datetime(2026, 1, 1, tzinfo=UTC))
    newer = _order(db_session, me.id, datetime(2026, 2, 1, tzinfo=UTC))
    _order(db_session, other.id, datetime(2026, 3, 1, tzinfo=UTC))

    response = client.get(f"{USERS}/orders", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert [order["id"] for order in body["items"]] == [newer, older]
    assert body["total"] == 2


def test_an_empty_order_list_is_an_empty_page(client):
    response = client.get(f"{USERS}/orders", headers=_auth(client))

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "page": 1, "size": 20, "has_more": False}


def test_another_address_still_reaches_the_protected_routes():
    # Nothing in authentication depends on where a request comes from.
    elsewhere = TestClient(app, client=("198.51.100.9", 50000))
    _register(elsewhere)
    assert elsewhere.get(f"{USERS}/me", headers=_auth(elsewhere)).status_code == 200


def test_a_country_that_outgrows_the_limit_when_title_cased_is_422_without_echoing_it(client):
    """'ß'.title() is 'Ss': the cased value is one character longer than the
    100 the create schema checked, so building the response from the stored
    row raises a ValidationError. That 422 goes through the same sanitiser
    as request validation (app/core/errors.py): no `input`, no raw `ctx`."""
    headers = _auth(client)
    country = "ß" * 100

    response = client.post(f"{USERS}/addresses", json=_address(country=country), headers=headers)

    assert response.status_code == 422
    assert country not in response.text
    problem = response.json()
    assert problem["detail"] == "Invalid address data"
    assert problem["errors"]
    for error in problem["errors"]:
        assert "input" not in error
        assert set(error) <= {"type", "loc", "msg", "ctx", "url"}
