"""Strict input (4.0.0): a request body names only the fields the API knows,
money arrives as a decimal string, and a datetime carries its time zone.

- An unknown member of a JSON body (or of an object nested in it) is a 422
  validation problem of type extra_forbidden, with the member in its loc and
  without the submitted value.
- Money on input is a JSON string with at most two fraction digits. A JSON
  number is a binary float in most clients (0.1 + 0.2), so it is refused.
- GET /admin/stats writes its amounts like every other money output.
- A datetime on input without an offset is a 422.
"""
from dataclasses import dataclass

import pytest
from payment_support import PAY_OK, Shop, new_key, reset_provider

API = "/api/v1"
MERGE_PATCH = {"Content-Type": "application/merge-patch+json"}
BOGUS = {"bogus": "value-that-must-not-be-echoed"}


@pytest.fixture(autouse=True)
def _clean_provider():
    reset_provider()
    yield
    reset_provider()


@dataclass
class World:
    shop: Shop
    admin: dict
    buyer: object
    product: object
    order: dict

    @property
    def client(self):
        return self.shop.client


@pytest.fixture
def world(client, db_session):
    shop = Shop(client, db_session)
    buyer = shop.buyer()
    product = shop.product(price="25.00")
    # Shop's description is shorter than ProductBase allows in a response.
    product.description = "A widget for the strict tests"
    db_session.commit()
    order = shop.order_of(buyer, product)
    return World(shop, shop.admin(), buyer, product, order)


def _with_key(headers):
    return {**headers, "Idempotency-Key": new_key()}


def _address():
    return {"street": "123 Main Street", "city": "Springfield", "state": "Illinois",
            "postal_code": "62701", "country": "US"}


def _product_body(category_id, **extra):
    return {"name": "Strict Widget", "description": "A widget for the strict tests",
            "price": "9.99", "stock_quantity": 1, "category_id": category_id,
            "images": ["https://example.com/w.png"], "characteristics": {}, **extra}


# Each case sends a request that would be valid, plus one unknown member.
# (id, callable(world) -> response)
UNKNOWN_FIELD_CASES = [
    ("register", lambda w: w.client.post(f"{API}/users/register", json={
        "email": "strict@example.com", "password": "Str0ngPass99", "gdpr_consent": True,
        "privacy_policy_accepted": True, **BOGUS})),
    ("refresh", lambda w: w.client.post(f"{API}/users/refresh", json={
        "refresh_token": "x", **BOGUS})),
    ("logout", lambda w: w.client.post(f"{API}/users/logout", headers=w.buyer.headers,
                                       json=BOGUS)),
    ("password-reset", lambda w: w.client.post(f"{API}/users/password/reset", json={
        "email": "a@example.com", **BOGUS})),
    ("password-reset-confirm", lambda w: w.client.post(
        f"{API}/users/password/reset/confirm",
        json={"token": "t", "new_password": "NewStr0ngPass", **BOGUS})),
    ("verify-email", lambda w: w.client.post(f"{API}/users/verify-email", json={
        "token": "t", **BOGUS})),
    ("update-me-put", lambda w: w.client.put(f"{API}/users/me", headers=w.buyer.headers,
                                             json={"full_name": "New Name", **BOGUS})),
    ("update-me-merge-patch", lambda w: w.client.patch(
        f"{API}/users/me", headers={**w.buyer.headers, **MERGE_PATCH},
        json={"full_name": "New Name", **BOGUS})),
    ("address-create", lambda w: w.client.post(
        f"{API}/users/addresses", headers=w.buyer.headers, json={**_address(), **BOGUS})),
    ("address-put", lambda w: w.client.put(
        f"{API}/users/addresses/{w.buyer.address_id}", headers=w.buyer.headers,
        json={"city": "Elsewhere", **BOGUS})),
    ("address-merge-patch", lambda w: w.client.patch(
        f"{API}/users/addresses/{w.buyer.address_id}",
        headers={**w.buyer.headers, **MERGE_PATCH}, json={"city": "Elsewhere", **BOGUS})),
    ("address-default", lambda w: w.client.post(
        f"{API}/users/addresses/default", headers=w.buyer.headers,
        json={"address_id": w.buyer.address_id, **BOGUS})),
    ("cart-add", lambda w: w.client.post(
        f"{API}/cart/items", headers=w.buyer.headers,
        json={"product_id": w.product.id, "quantity": 1, **BOGUS})),
    ("cart-update", lambda w: w.client.put(
        f"{API}/cart/items/{w.product.id}", headers=w.buyer.headers,
        json={"quantity": 2, **BOGUS})),
    ("order-create", lambda w: w.client.post(
        f"{API}/orders", headers=_with_key(w.buyer.headers),
        json={"shipping_address_id": w.buyer.address_id, **BOGUS})),
    ("order-status", lambda w: w.client.put(
        f"{API}/orders/{w.order['id']}/status", headers=w.admin,
        json={"status": "confirmed", **BOGUS})),
    ("pay", lambda w: w.client.post(
        f"{API}/orders/{w.order['id']}/pay", headers=_with_key(w.buyer.headers),
        json={"order_id": w.order["id"], "payment_method": "stripe", "amount": "25.00",
              "payment_method_token": PAY_OK, **BOGUS})),
    ("checkout-session", lambda w: w.client.post(
        f"{API}/orders/{w.order['id']}/checkout-session",
        headers=_with_key(w.buyer.headers), json=BOGUS)),
    ("refund", lambda w: w.client.post(
        f"{API}/orders/{w.order['id']}/refund", headers=_with_key(w.admin),
        json={"amount": "1.00", **BOGUS})),
    ("category-create", lambda w: w.client.post(
        f"{API}/admin/categories", headers=w.admin, json={"name": "Shelf", **BOGUS})),
    ("product-create", lambda w: w.client.post(
        f"{API}/admin/products", headers=w.admin,
        json=_product_body(w.product.category_id, **BOGUS))),
    ("product-put", lambda w: w.client.put(
        f"{API}/admin/products/{w.product.id}", headers=w.admin,
        json={"stock_quantity": 3, **BOGUS})),
    ("product-merge-patch", lambda w: w.client.patch(
        f"{API}/admin/products/{w.product.id}", headers={**w.admin, **MERGE_PATCH},
        json={"stock_quantity": 3, **BOGUS})),
    ("variant-create", lambda w: w.client.post(
        f"{API}/admin/products/{w.product.id}/variants", headers=w.admin,
        json={"sku": "STRICT-1", **BOGUS})),
    ("variant-put", lambda w: w.client.put(
        f"{API}/admin/products/{w.product.id}/variants/{w.shop.variant(w.product).id}",
        headers=w.admin, json={"stock_quantity": 3, **BOGUS})),
    ("variant-merge-patch", lambda w: w.client.patch(
        f"{API}/admin/products/{w.product.id}/variants/{w.shop.variant(w.product).id}",
        headers={**w.admin, **MERGE_PATCH}, json={"stock_quantity": 3, **BOGUS})),
    ("consent", lambda w: w.client.post(
        f"{API}/legal/consent", headers=w.buyer.headers,
        json={"marketing_consent": True, "privacy_policy_accepted": True, **BOGUS})),
    ("data-request", lambda w: w.client.post(
        f"{API}/legal/data-request", headers=w.buyer.headers,
        json={"request_type": "export", **BOGUS})),
]


def _extra_forbidden(response, *loc):
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["type"].endswith("/validation-error")
    assert any(
        e["type"] == "extra_forbidden" and e["loc"] == ["body", *loc]
        for e in body["errors"]
    ), body["errors"]
    assert all("input" not in e for e in body["errors"])
    assert BOGUS["bogus"] not in response.text


@pytest.mark.parametrize(
    "send", [c[1] for c in UNKNOWN_FIELD_CASES], ids=[c[0] for c in UNKNOWN_FIELD_CASES])
def test_an_unknown_body_field_is_a_422(world, send):
    _extra_forbidden(send(world), "bogus")


def test_an_unknown_field_of_a_nested_object_is_a_422(world):
    characteristics = {"color": {"name": "color", "value": "red", **BOGUS}}
    response = world.client.post(
        f"{API}/admin/products", headers=world.admin,
        json=_product_body(world.product.category_id, characteristics=characteristics))

    _extra_forbidden(response, "characteristics", "color", "bogus")


def test_password_reset_confirm_refuses_a_current_password(client, valid_registration_payload):
    """Old clients used to send current_password here; it was ignored."""
    client.post(f"{API}/users/register", json=valid_registration_payload)
    from test_auth import _reset_token_for

    token = _reset_token_for(valid_registration_payload["email"])
    response = client.post(f"{API}/users/password/reset/confirm", json={
        "token": token, "current_password": "anything", "new_password": "NewStr0ngPass"})

    _extra_forbidden(response, "current_password")


def test_the_form_login_is_not_affected(client, valid_registration_payload):
    from test_auth import _reset_token_for  # noqa: F401  (same fixtures as the auth tests)

    client.post(f"{API}/users/register", json=valid_registration_payload)

    response = client.post(f"{API}/users/login", data={
        "username": valid_registration_payload["email"],
        "password": valid_registration_payload["password"],
        "unrelated_form_field": "ignored"})

    assert response.status_code in (200, 401, 403)
    assert response.status_code != 422


# --- money: a decimal string, never a JSON number ------------------------------------

def _create_product(w, price):
    return w.client.post(f"{API}/admin/products", headers=w.admin,
                         json=_product_body(w.product.category_id, price=price))


def _update_product(w, price):
    return w.client.put(f"{API}/admin/products/{w.product.id}", headers=w.admin,
                        json={"price": price})


def _create_variant(w, price):
    return w.client.post(f"{API}/admin/products/{w.product.id}/variants", headers=w.admin,
                         json={"sku": f"M-{new_key()[:8]}", "price_override": price})


def _update_variant(w, price):
    variant = w.shop.variant(w.product)
    return w.client.put(f"{API}/admin/products/{w.product.id}/variants/{variant.id}",
                        headers=w.admin, json={"price_override": price})


def _pay(w, amount):
    return w.client.post(
        f"{API}/orders/{w.order['id']}/pay", headers=_with_key(w.buyer.headers),
        json={"order_id": w.order["id"], "payment_method": "stripe", "amount": amount,
              "payment_method_token": PAY_OK})


def _refund(w, amount):
    assert _pay(w, "25.00").status_code == 200
    return w.client.post(f"{API}/orders/{w.order['id']}/refund",
                         headers=_with_key(w.admin), json={"amount": amount})


MONEY_WRITES = [
    pytest.param(_create_product, id="product-create"),
    pytest.param(_update_product, id="product-update"),
    pytest.param(_create_variant, id="variant-create"),
    pytest.param(_update_variant, id="variant-update"),
    pytest.param(_pay, id="payment"),
    pytest.param(_refund, id="refund"),
]


@pytest.mark.parametrize("write", MONEY_WRITES)
@pytest.mark.parametrize("number", [10.5, 10, 0.1, 1e3, True])
def test_money_as_a_json_number_is_a_422(world, write, number):
    response = write(world, number)

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["type"].endswith("/validation-error")
    assert body["errors"][0]["loc"][0] == "body"


@pytest.mark.parametrize("text", ["1e1", " 10.50", "10.50 ", "NaN", "Infinity", "10.", ".5", "1,5"])
def test_money_that_is_not_a_plain_decimal_string_is_a_422(world, text):
    assert _create_product(world, text).status_code == 422


@pytest.mark.parametrize("write,expect", [
    pytest.param(_create_product, 201, id="product-create"),
    pytest.param(_update_product, 200, id="product-update"),
    pytest.param(_create_variant, 201, id="variant-create"),
    pytest.param(_update_variant, 200, id="variant-update"),
])
def test_money_as_a_decimal_string_is_accepted(world, write, expect):
    response = write(world, "10.5")

    assert response.status_code == expect, response.text


def test_a_payment_and_a_refund_take_money_as_strings(world):
    assert _pay(world, "25.00").status_code == 200
    refund = world.client.post(
        f"{API}/orders/{world.order['id']}/refund", headers=_with_key(world.admin),
        json={"amount": "5.00"})

    assert refund.status_code == 200, refund.text


def test_openapi_describes_money_inputs_as_strings(client):
    schemas = client.get(f"{API}/openapi.json").json()["components"]["schemas"]

    for model, field in [("ProductCreate", "price"), ("PaymentCreate", "amount"),
                         ("ProductVariantCreate", "price_override")]:
        prop = schemas[model]["properties"][field]
        variants = prop.get("anyOf", [prop])
        typed = [v for v in variants if v.get("type") != "null"]
        assert len(typed) == 1 and typed[0]["type"] == "string", (model, prop)
        assert "pattern" in typed[0]
    refund = schemas["RefundCreate"]["properties"]["amount"]
    assert any(v.get("type") == "string" and "pattern" in v for v in refund["anyOf"])


