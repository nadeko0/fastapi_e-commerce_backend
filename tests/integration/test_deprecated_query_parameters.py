"""Four operations keep their URL in 4.0.0 and only move their data from the
query string to the JSON body. Since 3.3.0 both work; the query parameters,
not the operations, are deprecated (OpenAPI 3.1 Parameter Object `deprecated`;
RFC 9745: Deprecation/Sunset on the responses that use them). The two token
GETs are deprecated for real, with a POST successor."""
import pytest
from test_auth import _reset_token_for
from test_orders import (
    CART_PREFIX,
    ORDERS_PREFIX,
    _auth_headers,
    _checkout_ready_buyer,
    _create_order,
    _login,
    _make_admin,
    _make_category,
    _make_product,
    _make_variant,
    _with_idempotency_key,
)

from app.core.deprecation import DEPRECATED_OPERATIONS, DEPRECATED_QUERY_PARAMETERS
from app.core.security import generate_email_verification_token
from app.models.user import User

USERS = "/api/v1/users"
DEPRECATION_HEADERS = ("Deprecation", "Sunset", "Link")


def _has_deprecation(response):
    return all(name in response.headers for name in DEPRECATION_HEADERS)


def _has_none(response):
    return not any(name in response.headers for name in DEPRECATION_HEADERS)


def _product(db_session, stock=10):
    return _make_product(db_session, _make_category(db_session).id, stock=stock)


# --- POST /orders ----------------------------------------------------------


def test_order_from_json_body_carries_no_deprecation(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    product = _product(db_session)
    client.post(f"{CART_PREFIX}/items", json={"product_id": product.id, "quantity": 2}, headers=_auth_headers(token))

    response = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": address_id}, headers=_with_idempotency_key(_auth_headers(token))
    )

    assert response.status_code == 200
    assert response.json()["data"]["shipping_address"]["id"] == address_id
    assert _has_none(response)


def test_order_from_query_string_works_and_is_flagged(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    product = _product(db_session)
    client.post(f"{CART_PREFIX}/items", json={"product_id": product.id}, headers=_auth_headers(token))

    response = client.post(
        ORDERS_PREFIX,
        params={"shipping_address_id": address_id},
        headers=_with_idempotency_key(_auth_headers(token)),
    )

    assert response.status_code == 200
    assert _has_deprecation(response)
    assert 'successor-version' not in response.headers["Link"]


def test_order_with_both_sources_is_refused(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)

    response = client.post(
        ORDERS_PREFIX,
        params={"shipping_address_id": address_id},
        json={"shipping_address_id": address_id},
        headers=_with_idempotency_key(_auth_headers(token)),
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "shipping_address_id"]


def test_order_without_the_address_keeps_its_old_422(client, db_session):
    token, _ = _checkout_ready_buyer(client, db_session)

    response = client.post(ORDERS_PREFIX, headers=_with_idempotency_key(_auth_headers(token)))

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "shipping_address_id"]
    assert response.json()["detail"][0]["msg"] == "Field required"
    assert _has_none(response)


def test_order_body_is_validated_like_the_query_parameter(client, db_session):
    token, _ = _checkout_ready_buyer(client, db_session)

    response = client.post(
        ORDERS_PREFIX, json={"shipping_address_id": 2**31}, headers=_with_idempotency_key(_auth_headers(token))
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "shipping_address_id"]


# --- PUT /orders/{id}/status -----------------------------------------------


def _admin_headers(client, db_session):
    admin = _make_admin(db_session)
    return _auth_headers(_login(client, admin.email, "AdminPass1"))


def test_status_from_json_body_carries_no_deprecation(client, db_session):
    _, order = _create_order(client, db_session)

    response = client.put(
        f"{ORDERS_PREFIX}/{order['id']}/status", json={"status": "confirmed"}, headers=_admin_headers(client, db_session)
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "confirmed"
    assert _has_none(response)


def test_status_from_query_string_works_and_is_flagged(client, db_session):
    _, order = _create_order(client, db_session)

    response = client.put(
        f"{ORDERS_PREFIX}/{order['id']}/status", params={"status": "confirmed"}, headers=_admin_headers(client, db_session)
    )

    assert response.status_code == 200
    assert _has_deprecation(response)


def test_status_with_both_sources_or_neither_is_422(client, db_session):
    _, order = _create_order(client, db_session)
    headers = _admin_headers(client, db_session)
    url = f"{ORDERS_PREFIX}/{order['id']}/status"

    both = client.put(url, params={"status": "confirmed"}, json={"status": "confirmed"}, headers=headers)
    neither = client.put(url, headers=headers)

    assert both.status_code == 422
    assert neither.status_code == 422
    assert neither.json()["detail"][0]["loc"] == ["query", "status"]
    assert client.put(url, json={"status": "bogus"}, headers=headers).status_code == 422


# --- POST /cart/items ------------------------------------------------------


def test_add_to_cart_from_json_body_carries_no_deprecation(client, db_session):
    token, _ = _checkout_ready_buyer(client, db_session)
    product = _product(db_session)
    variant = _make_variant(db_session, product.id)

    response = client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": 2, "variant_id": variant.id},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    line = response.json()["data"]["items"][0]
    assert (line["product_id"], line["quantity"], line["variant_id"]) == (product.id, 2, variant.id)
    assert _has_none(response)


def test_add_to_cart_body_quantity_defaults_to_one(client, db_session):
    token, _ = _checkout_ready_buyer(client, db_session)
    product = _product(db_session)

    response = client.post(f"{CART_PREFIX}/items", json={"product_id": product.id}, headers=_auth_headers(token))

    assert response.json()["data"]["items"][0]["quantity"] == 1


def test_add_to_cart_from_query_string_works_and_is_flagged(client, db_session):
    token, _ = _checkout_ready_buyer(client, db_session)
    product = _product(db_session)

    response = client.post(
        f"{CART_PREFIX}/items", params={"product_id": product.id, "quantity": 3}, headers=_auth_headers(token)
    )

    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["quantity"] == 3
    assert _has_deprecation(response)


def test_guest_add_to_cart_from_body(client, db_session):
    product = _product(db_session)

    response = client.post(f"{CART_PREFIX}/items", json={"product_id": product.id, "quantity": 2})

    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["quantity"] == 2
    assert _has_none(response)


@pytest.mark.parametrize(
    ("kwargs", "loc"),
    [
        ({"json": {"product_id": 1, "quantity": 0}}, ["body", "quantity"]),
        ({"json": {"quantity": 1}}, ["body", "product_id"]),
        ({"params": {"quantity": 1}}, ["query", "product_id"]),
        ({"params": {"product_id": 1, "quantity": 0}}, ["query", "quantity"]),
        ({"params": {"product_id": 1}, "json": {"product_id": 1}}, ["query", "product_id"]),
    ],
)
def test_add_to_cart_invalid_input_is_422(client, kwargs, loc):
    response = client.post(f"{CART_PREFIX}/items", **kwargs)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == loc


# --- PUT /cart/items/{product_id} ------------------------------------------


def test_update_cart_item_from_json_body_and_from_query(client, db_session):
    token, _ = _checkout_ready_buyer(client, db_session)
    product = _product(db_session)
    headers = _auth_headers(token)
    client.post(f"{CART_PREFIX}/items", json={"product_id": product.id}, headers=headers)
    url = f"{CART_PREFIX}/items/{product.id}"

    from_body = client.put(url, json={"quantity": 4}, headers=headers)
    from_query = client.put(url, params={"quantity": 5}, headers=headers)

    assert from_body.json()["data"]["items"][0]["quantity"] == 4
    assert _has_none(from_body)
    assert from_query.json()["data"]["items"][0]["quantity"] == 5
    assert _has_deprecation(from_query)


def test_update_cart_item_invalid_input_is_422(client, db_session):
    token, _ = _checkout_ready_buyer(client, db_session)
    headers = _auth_headers(token)
    url = f"{CART_PREFIX}/items/1"

    assert client.put(url, headers=headers).json()["detail"][0]["loc"] == ["query", "quantity"]
    assert client.put(url, json={"quantity": 0}, headers=headers).json()["detail"][0]["loc"] == ["body", "quantity"]
    both = client.put(url, params={"quantity": 1}, json={"quantity": 1}, headers=headers)
    assert both.status_code == 422


def test_only_the_deprecated_parameters_flag_a_response(client, db_session):
    # variant_id of the DELETE is not deprecated; a query parameter that is
    # not in the table does not trigger the headers.
    token, _ = _checkout_ready_buyer(client, db_session)
    product = _product(db_session)
    headers = _auth_headers(token)
    client.post(f"{CART_PREFIX}/items", json={"product_id": product.id}, headers=headers)

    deleted = client.delete(f"{CART_PREFIX}/items/{product.id}", params={"variant_id": 1}, headers=headers)
    other = client.put(f"{CART_PREFIX}/items/{product.id}", json={"quantity": 1}, params={"unrelated": "x"}, headers=headers)

    assert _has_none(deleted)
    assert _has_none(other)


# --- OpenAPI ----------------------------------------------------------------


def test_openapi_deprecates_the_parameters_not_the_operations(client):
    schema = client.get("/api/v1/openapi.json").json()

    for (method, path), names in DEPRECATED_QUERY_PARAMETERS.items():
        operation = schema["paths"]["/api/v1" + path][method.lower()]
        assert not operation.get("deprecated")
        assert "requestBody" in operation
        deprecated = {p["name"] for p in operation["parameters"] if p.get("deprecated")}
        assert deprecated == names


def test_no_operation_is_in_both_tables():
    assert not set(DEPRECATED_OPERATIONS) & set(DEPRECATED_QUERY_PARAMETERS)


# --- token GETs and their POST successors ------------------------------------


def test_verify_email_post_has_the_effect_of_the_get(client, db_session):
    client.post(f"{USERS}/register", json={
        "email": "new.person@example.com", "password": "Str0ngPass1", "full_name": "New Person",
        "gdpr_consent": True, "privacy_policy_accepted": True,
    })
    token = generate_email_verification_token("new.person@example.com")

    first = client.post(f"{USERS}/verify-email", json={"token": token})
    again = client.post(f"{USERS}/verify-email", json={"token": token})

    assert first.status_code == 200
    assert first.json()["data"]["message"] == "Email verified successfully"
    assert again.json()["data"]["message"] == "Email already verified"
    db_session.expire_all()
    assert db_session.query(User).filter(User.email == "new.person@example.com").one().is_email_verified
    assert _has_none(first)


def test_verify_email_post_errors_match_the_get(client):
    assert client.post(f"{USERS}/verify-email", json={"token": "garbage"}).status_code == 400
    unknown = generate_email_verification_token("nobody@example.com")
    assert client.post(f"{USERS}/verify-email", json={"token": unknown}).status_code == 404
    assert client.post(f"{USERS}/verify-email", json={}).status_code == 422
    assert client.post(f"{USERS}/verify-email", json={"token": ""}).status_code == 422


def test_verify_email_get_points_to_the_post(client):
    response = client.get(f"{USERS}/verify-email/garbage")

    assert _has_deprecation(response)
    assert '</api/v1/users/verify-email>; rel="successor-version"' in response.headers["Link"]


def test_reset_token_check_post_matches_the_get(client, valid_registration_payload):
    client.post(f"{USERS}/register", json=valid_registration_payload)
    token = _reset_token_for(valid_registration_payload["email"])
    url = f"{USERS}/password/reset-token/validate"

    ok = client.post(url, json={"token": token})
    bad = client.post(url, json={"token": "garbage"})

    assert ok.status_code == 200
    assert ok.json()["data"] == client.get(f"{USERS}/password/reset/{token}").json()["data"]
    assert bad.status_code == 400
    assert bad.json()["detail"] == "Invalid or expired reset token"
    assert _has_none(ok)
    # It only checks: the token is still good afterwards.
    assert client.post(url, json={"token": token}).status_code == 200


def test_reset_token_get_points_to_the_post(client):
    response = client.get(f"{USERS}/password/reset/garbage")

    assert _has_deprecation(response)
    assert '</api/v1/users/password/reset-token/validate>; rel="successor-version"' in response.headers["Link"]

