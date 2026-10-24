"""4.0.0 removed what 3.3.0 deprecated: five operations, and the query
parameters of four actions that now take only the JSON body."""
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
    _with_idempotency_key,
)

USERS = "/api/v1/users"


def _product(db_session):
    return _make_product(db_session, _make_category(db_session).id, stock=10)


# --- removed operations -------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", f"{USERS}/verify-email/sometoken"),
        ("GET", f"{USERS}/data/export"),
        ("POST", f"{USERS}/data/delete"),
        ("POST", f"{USERS}/consent"),
    ],
)
def test_removed_route_is_a_404(client, method, url):
    assert client.request(method, url).status_code == 404


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_reset_token_path_is_gone_for_every_method(client, method):
    response = client.request(
        method, f"{USERS}/password/reset/sometoken",
        json={"new_password": "NewStr0ngPass"} if method == "POST" else None,
    )

    # No route has this path any more (the token moved to the body of
    # POST /password/reset/confirm), so routing answers 404, not 405: 405
    # would mean the path still existed for another method.
    assert response.status_code == 404


def test_reset_confirm_takes_the_token_from_the_body_only(client):
    # A missing token is a validation error, not an invalid-token 400.
    missing = client.post(f"{USERS}/password/reset/confirm", json={"new_password": "NewStr0ngPass"})
    assert missing.status_code == 422
    assert missing.json()["errors"][0]["loc"] == ["body", "token"]

    empty = client.post(
        f"{USERS}/password/reset/confirm", json={"token": "", "new_password": "NewStr0ngPass"}
    )
    assert empty.status_code == 422

    # A token in the query string is not read.
    in_query = client.post(
        f"{USERS}/password/reset/confirm?token=sometoken", json={"new_password": "NewStr0ngPass"}
    )
    assert in_query.status_code == 422

    bad = client.post(
        f"{USERS}/password/reset/confirm", json={"token": "sometoken", "new_password": "NewStr0ngPass"}
    )
    assert bad.status_code == 400


def test_reset_token_never_appears_in_the_request_line(client, valid_registration_payload):

    client.post(f"{USERS}/register", json=valid_registration_payload)
    token = _reset_token_for(valid_registration_payload["email"])

    response = client.post(
        f"{USERS}/password/reset/confirm", json={"token": token, "new_password": "NewStr0ngPass"}
    )

    assert response.status_code == 200
    assert token not in str(response.request.url)
    assert token not in response.request.url.path
    assert token in response.request.content.decode()


def test_openapi_has_no_deprecated_anywhere(client):
    def flagged(node):
        if isinstance(node, dict):
            return node.get("deprecated") is True or any(flagged(v) for v in node.values())
        if isinstance(node, list):
            return any(flagged(v) for v in node)
        return False

    assert not flagged(client.get("/api/v1/openapi.json").json())


def test_no_deprecation_headers_are_exposed_to_browsers(client):
    response = client.get("/health", headers={"Origin": "http://localhost:3000"})

    exposed = {h.strip() for h in response.headers["access-control-expose-headers"].split(",")}
    assert not exposed & {"Deprecation", "Sunset", "Link"}


# --- body-only actions ----------------------------------------------------------


def _is_missing_body(response):
    assert response.status_code == 422
    detail = response.json()["errors"][0]
    assert (detail["type"], detail["loc"]) == ("missing", ["body"])


def test_order_without_a_body_is_422_even_with_the_old_query_parameter(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)

    response = client.post(
        ORDERS_PREFIX,
        params={"shipping_address_id": address_id},
        headers=_with_idempotency_key(_auth_headers(token)),
    )

    _is_missing_body(response)


def test_order_status_without_a_body_is_422_even_with_the_old_query_parameter(client, db_session):
    _, order = _create_order(client, db_session)
    admin = _make_admin(db_session)
    headers = _auth_headers(_login(client, admin.email, "AdminPass1"))

    response = client.put(f"{ORDERS_PREFIX}/{order['id']}/status", params={"status": "confirmed"}, headers=headers)

    _is_missing_body(response)


def test_add_to_cart_without_a_body_is_422_even_with_the_old_query_parameters(client, db_session):
    product = _product(db_session)

    response = client.post(f"{CART_PREFIX}/items", params={"product_id": product.id, "quantity": 2})

    _is_missing_body(response)


def test_update_cart_item_without_a_body_is_422_even_with_the_old_query_parameters(client, db_session):
    token, _ = _checkout_ready_buyer(client, db_session)
    product = _product(db_session)
    headers = _auth_headers(token)
    client.post(f"{CART_PREFIX}/items", json={"product_id": product.id}, headers=headers)

    response = client.put(f"{CART_PREFIX}/items/{product.id}", params={"quantity": 5}, headers=headers)

    _is_missing_body(response)


def test_old_query_parameters_next_to_a_body_are_ignored(client, db_session):
    # FastAPI does not reject query parameters a route does not declare, and
    # the body wins: no 422 for a client that still appends them.
    product = _product(db_session)

    response = client.post(
        f"{CART_PREFIX}/items", params={"product_id": 999999, "quantity": 7}, json={"product_id": product.id, "quantity": 2}
    )

    assert response.status_code == 200
    line = response.json()["items"][0]
    assert (line["product_id"], line["quantity"]) == (product.id, 2)


def test_old_query_parameters_are_not_in_openapi(client):
    paths = client.get("/api/v1/openapi.json").json()["paths"]

    def query_names(path, method):
        return {p["name"] for p in paths[path][method].get("parameters", []) if p["in"] == "query"}

    assert not query_names("/api/v1/orders", "post") & {"shipping_address_id"}
    assert not query_names("/api/v1/orders/{order_id}/status", "put") & {"status"}
    assert not query_names("/api/v1/cart/items", "post") & {"product_id", "quantity", "variant_id"}
    assert not query_names("/api/v1/cart/items/{product_id}", "put") & {"quantity", "variant_id"}
    for path, method in [
        ("/api/v1/orders", "post"),
        ("/api/v1/orders/{order_id}/status", "put"),
        ("/api/v1/cart/items", "post"),
        ("/api/v1/cart/items/{product_id}", "put"),
    ]:
        assert paths[path][method]["requestBody"]["required"] is True
