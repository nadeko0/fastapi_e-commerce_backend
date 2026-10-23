"""PATCH (JSON Merge Patch, RFC 7396) on the four partial-update resources.

PATCH shares the PUT handlers: members present replace, null clears a
nullable field, null on a non-nullable field is a 422, absent members stay.
"""

import pytest
from test_admin import (
    ADMIN_PREFIX,
    CATALOG_PREFIX,
    _auth_headers,
    _create_product,
    _login_as,
    _make_admin,
    _make_category,
)
from test_users import API_PREFIX, _register_and_login, _valid_address

from app.api.merge_patch import MERGE_PATCH_MEDIA_TYPE
from app.main import app
from app.models.product import Product, ProductVariant

MERGE_PATCH = {"Content-Type": MERGE_PATCH_MEDIA_TYPE}


def _address(client, token, **extra):
    return client.post(
        f"{API_PREFIX}/addresses",
        json={**_valid_address(), **extra},
        headers=_auth_headers(token),
    ).json()["data"]


def _admin(client, db_session):
    admin = _make_admin(db_session)
    return _auth_headers(_login_as(client, admin.email, "AdminPass1"))


def _product(client, db_session):
    headers = _admin(client, db_session)
    category = _make_category(db_session)
    product = _create_product(client, headers["Authorization"].split()[1], category.id)
    return headers, product


def _variant(client, headers, product_id, **extra):
    return client.post(
        f"{ADMIN_PREFIX}/products/{product_id}/variants",
        json={"sku": "PATCH-V1", **extra},
        headers=headers,
    ).json()["data"]


# --- OpenAPI ---------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/users/me",
        "/api/v1/users/addresses/{address_id}",
        "/api/v1/admin/products/{product_id}",
        "/api/v1/admin/products/{product_id}/variants/{variant_id}",
    ],
)
def test_patch_is_documented_with_both_json_media_types(path):
    operation = app.openapi()["paths"][path]["patch"]

    assert set(operation["requestBody"]["content"]) == {
        "application/json",
        MERGE_PATCH_MEDIA_TYPE,
    }
    assert "RFC 7396" in operation["description"]
    assert "put" in app.openapi()["paths"][path]


# --- /users/me --------------------------------------------------------------


def test_patch_me_changes_only_the_given_member(client, valid_registration_payload):
    token = _register_and_login(client, valid_registration_payload)

    response = client.patch(
        f"{API_PREFIX}/me",
        json={"phone": "+123456789"},
        headers={**_auth_headers(token), **MERGE_PATCH},
    )

    data = response.json()["data"]
    assert response.status_code == 200
    assert data["phone"] == "+123456789"
    assert data["full_name"] == valid_registration_payload["full_name"]


def test_patch_me_null_clears_a_nullable_field(client, valid_registration_payload):
    token = _register_and_login(client, valid_registration_payload)
    headers = _auth_headers(token)
    client.patch(f"{API_PREFIX}/me", json={"phone": "+123456789"}, headers=headers)

    response = client.patch(f"{API_PREFIX}/me", json={"phone": None}, headers=headers)

    assert response.status_code == 200
    assert response.json()["data"]["phone"] is None


def test_patch_me_null_on_a_non_nullable_field_is_422(client, valid_registration_payload):
    token = _register_and_login(client, valid_registration_payload)

    response = client.patch(
        f"{API_PREFIX}/me", json={"marketing_consent": None}, headers=_auth_headers(token)
    )

    assert response.status_code == 422
    assert "may be omitted but cannot be null" in response.text


def test_patch_me_ignores_unknown_members(client, valid_registration_payload):
    token = _register_and_login(client, valid_registration_payload)

    response = client.patch(
        f"{API_PREFIX}/me",
        json={"full_name": "Patched Name", "role": "admin"},
        headers=_auth_headers(token),
    )

    data = response.json()["data"]
    assert response.status_code == 200
    assert data["full_name"] == "Patched Name"
    assert data["role"] != "admin"


def test_patch_me_requires_authentication(client):
    assert client.patch(f"{API_PREFIX}/me", json={"full_name": "No Token"}).status_code == 401


def test_patch_me_with_no_members_is_the_same_400_as_put(client, valid_registration_payload):
    token = _register_and_login(client, valid_registration_payload)

    response = client.patch(f"{API_PREFIX}/me", json={}, headers=_auth_headers(token))

    assert response.status_code == 400


# --- /users/addresses/{id} ---------------------------------------------------


def test_patch_address_changes_only_the_given_member(client, valid_registration_payload):
    token = _register_and_login(client, valid_registration_payload)
    created = _address(client, token)

    response = client.patch(
        f"{API_PREFIX}/addresses/{created['id']}",
        json={"city": "Gotham"},
        headers={**_auth_headers(token), **MERGE_PATCH},
    )

    data = response.json()["data"]
    assert response.status_code == 200
    assert data["city"] == "Gotham"
    assert data["street"] == created["street"]


@pytest.mark.parametrize("field", ["delivery_instructions", "delivery_phone"])
def test_patch_address_null_clears_a_nullable_field(client, valid_registration_payload, field):
    token = _register_and_login(client, valid_registration_payload)
    created = _address(
        client, token, delivery_instructions="Leave at the door", delivery_phone="+123456789"
    )

    response = client.patch(
        f"{API_PREFIX}/addresses/{created['id']}",
        json={field: None},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert response.json()["data"][field] is None


@pytest.mark.parametrize("field", ["street", "city", "country", "is_default"])
def test_patch_address_null_on_a_non_nullable_field_is_422(
    client, valid_registration_payload, field
):
    token = _register_and_login(client, valid_registration_payload)
    created = _address(client, token)

    response = client.patch(
        f"{API_PREFIX}/addresses/{created['id']}",
        json={field: None},
        headers=_auth_headers(token),
    )

    assert response.status_code == 422
    assert "may be omitted but cannot be null" in response.text


def test_patch_address_ignores_unknown_members(client, valid_registration_payload):
    token = _register_and_login(client, valid_registration_payload)
    created = _address(client, token)

    response = client.patch(
        f"{API_PREFIX}/addresses/{created['id']}",
        json={"city": "Gotham", "user_id": 999},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert response.json()["data"]["city"] == "Gotham"


def _first_city(client, token):
    listing = client.get(f"{API_PREFIX}/addresses", headers=_auth_headers(token))
    return listing.json()["data"]["items"][0]["city"]


def test_patch_address_of_another_user_is_404(client, valid_registration_payload):
    owner = _register_and_login(client, valid_registration_payload)
    created = _address(client, owner)
    other = _register_and_login(
        client, {**valid_registration_payload, "email": "other.user@example.com"}
    )

    response = client.patch(
        f"{API_PREFIX}/addresses/{created['id']}",
        json={"city": "Hijacked"},
        headers=_auth_headers(other),
    )

    assert response.status_code == 404
    assert _first_city(client, owner) == created["city"]


def test_patch_address_default_unsets_the_old_default(client, valid_registration_payload):
    token = _register_and_login(client, valid_registration_payload)
    _address(client, token, is_default=True)
    second = _address(client, token, street="456 Side Street")

    client.patch(
        f"{API_PREFIX}/addresses/{second['id']}",
        json={"is_default": True},
        headers=_auth_headers(token),
    )

    listing = client.get(f"{API_PREFIX}/addresses", headers=_auth_headers(token))
    defaults = {a["id"] for a in listing.json()["data"]["items"] if a["is_default"]}
    assert defaults == {second["id"]}


def test_patch_address_requires_authentication(client):
    assert client.patch(f"{API_PREFIX}/addresses/1", json={"city": "X"}).status_code == 401


# --- /admin/products/{id} ----------------------------------------------------


def test_patch_product_changes_only_the_given_member(client, db_session):
    headers, product = _product(client, db_session)

    response = client.patch(
        f"{ADMIN_PREFIX}/products/{product['id']}",
        json={"stock_quantity": 7},
        headers={**headers, **MERGE_PATCH},
    )

    data = response.json()["data"]
    assert response.status_code == 200
    assert data["stock_quantity"] == 7
    assert data["name"] == product["name"]
    assert data["price"] == product["price"]


@pytest.mark.parametrize(
    "field", ["name", "description", "price", "stock_quantity", "category_id", "images"]
)
def test_patch_product_null_on_a_non_nullable_field_is_422(client, db_session, field):
    headers, product = _product(client, db_session)

    response = client.patch(
        f"{ADMIN_PREFIX}/products/{product['id']}", json={field: None}, headers=headers
    )

    assert response.status_code == 422
    assert "may be omitted but cannot be null" in response.text
    db_session.expire_all()
    assert getattr(db_session.get(Product, product["id"]), field) is not None


def test_patch_product_ignores_unknown_members(client, db_session):
    headers, product = _product(client, db_session)

    response = client.patch(
        f"{ADMIN_PREFIX}/products/{product['id']}",
        json={"stock_quantity": 3, "bogus": 1},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["data"]["stock_quantity"] == 3


def test_patch_product_invalidates_the_cache(client, db_session):
    headers, product = _product(client, db_session)
    before = client.get(f"{CATALOG_PREFIX}/products/{product['id']}").json()["data"]
    assert before["price"] == "19.99"

    client.patch(
        f"{ADMIN_PREFIX}/products/{product['id']}", json={"price": "25.00"}, headers=headers
    )

    after = client.get(f"{CATALOG_PREFIX}/products/{product['id']}").json()["data"]
    assert after["price"] == "25.00"


def test_patch_product_to_a_missing_category_is_404(client, db_session):
    headers, product = _product(client, db_session)

    response = client.patch(
        f"{ADMIN_PREFIX}/products/{product['id']}", json={"category_id": 99999}, headers=headers
    )

    assert response.status_code == 404


def test_patch_product_unknown_id_is_404(client, db_session):
    headers = _admin(client, db_session)

    response = client.patch(
        f"{ADMIN_PREFIX}/products/99999", json={"stock_quantity": 1}, headers=headers
    )

    assert response.status_code == 404


def test_patch_product_needs_an_admin(client, valid_registration_payload):
    anonymous = client.patch(f"{ADMIN_PREFIX}/products/1", json={"stock_quantity": 1})
    user_token = _register_and_login(client, valid_registration_payload)
    forbidden = client.patch(
        f"{ADMIN_PREFIX}/products/1",
        json={"stock_quantity": 1},
        headers=_auth_headers(user_token),
    )

    assert anonymous.status_code == 401
    assert forbidden.status_code == 403


# --- /admin/products/{id}/variants/{id} --------------------------------------


def test_patch_variant_changes_only_the_given_member(client, db_session):
    headers, product = _product(client, db_session)
    variant = _variant(client, headers, product["id"], stock_quantity=1)

    response = client.patch(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants/{variant['id']}",
        json={"price_override": "15.50"},
        headers={**headers, **MERGE_PATCH},
    )

    data = response.json()["data"]
    assert response.status_code == 200
    assert data["price_override"] == "15.50"
    assert data["sku"] == "PATCH-V1"
    assert data["stock_quantity"] == 1


def test_patch_variant_null_clears_price_override(client, db_session):
    headers, product = _product(client, db_session)
    variant = _variant(client, headers, product["id"], price_override="15.50")

    response = client.patch(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants/{variant['id']}",
        json={"price_override": None},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["data"]["price_override"] is None


@pytest.mark.parametrize("field", ["sku", "attributes", "stock_quantity", "is_active"])
def test_patch_variant_null_on_a_non_nullable_field_is_422(client, db_session, field):
    headers, product = _product(client, db_session)
    variant = _variant(client, headers, product["id"], attributes={"size": "M"})

    response = client.patch(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants/{variant['id']}",
        json={field: None},
        headers=headers,
    )

    assert response.status_code == 422
    assert "may be omitted but cannot be null" in response.text
    db_session.expire_all()
    assert getattr(db_session.get(ProductVariant, variant["id"]), field) is not None


def test_patch_variant_ignores_unknown_members_and_keeps_its_own_sku(client, db_session):
    headers, product = _product(client, db_session)
    variant = _variant(client, headers, product["id"])

    response = client.patch(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants/{variant['id']}",
        json={"sku": "PATCH-V1", "bogus": True, "stock_quantity": 9},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["data"]["stock_quantity"] == 9


def test_patch_variant_unknown_variant_is_404_and_needs_an_admin(
    client, db_session, valid_registration_payload
):
    headers, product = _product(client, db_session)
    url = f"{ADMIN_PREFIX}/products/{product['id']}/variants/99999"
    user_token = _register_and_login(client, valid_registration_payload)

    assert client.patch(url, json={"stock_quantity": 1}, headers=headers).status_code == 404
    assert client.patch(url, json={"stock_quantity": 1}).status_code == 401
    forbidden = client.patch(url, json={"stock_quantity": 1}, headers=_auth_headers(user_token))
    assert forbidden.status_code == 403
