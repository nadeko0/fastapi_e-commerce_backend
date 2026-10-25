"""A read never fails on what the database holds.

The input rules (a name of at least 3 characters, a street of at least 5, a
phone number of 5 to 15 digits...) belong to the request schemas. They were
also on the base classes the response models inherit, so a stored row that
breaks one (written before the rule, or edited in the database) made building
the response fail: a 500 for every read of the row, the lists that contain it
included. The database's own limits (NOT NULL, column types, CHECK
constraints) are the only ones a response model keeps.

Each test stores a row that breaks the input rules directly and reads it back
through every route that serves it.
"""
import pytest
from pydantic import ValidationError
from test_admin import (
    ADMIN_PREFIX,
    CATALOG_PREFIX,
    _auth_headers,
    _login_as,
    _make_admin,
    _make_category,
    _valid_product_payload,
)
from test_admin_orders import _customer, _headers, _order

from app.models.category import Category
from app.models.product import Product, ProductVariant
from app.schemas.address import AddressCreate, AddressUpdate

USERS = "/api/v1/users"
ORDERS = "/api/v1/orders"


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def _admin_headers(client, db_session):
    admin = _make_admin(db_session)
    return _auth_headers(_login_as(client, admin.email, "AdminPass1"))


def test_a_product_below_the_input_minimums_is_served(client, db_session):
    category = _make_category(db_session)
    product = Product(
        name="ab", description="short", price="1.00", stock_quantity=1,
        category_id=category.id, images=["https://example.com/a.png"],
        characteristics={"size": {"name": "", "value": 1, "unit": None, "filterable": True}},
    )
    db_session.add(product)
    db_session.commit()

    listing = _ok(client.get(f"{CATALOG_PREFIX}/products?category_id={category.id}"))
    detail = _ok(client.get(f"{CATALOG_PREFIX}/products/{product.id}"))

    assert [item["id"] for item in listing["items"]] == [product.id]
    assert (detail["name"], detail["description"]) == ("ab", "short")
    assert detail["characteristics"]["size"]["name"] == ""


def test_a_product_name_over_the_input_maximum_is_served(client, db_session):
    category = _make_category(db_session)
    product = Product(
        name="n" * 250, description="A description that is long enough", price="1.00",
        stock_quantity=1, category_id=category.id, images=["https://example.com/a.png"],
        characteristics={"size": {"name": "c" * 150, "value": 1, "unit": None, "filterable": True}},
    )
    db_session.add(product)
    db_session.commit()

    assert len(_ok(client.get(f"{CATALOG_PREFIX}/products/{product.id}"))["name"]) == 250
    _ok(client.get(f"{CATALOG_PREFIX}/products?category_id={category.id}"))


def test_a_variant_with_an_empty_sku_is_served(client, db_session):
    headers = _admin_headers(client, db_session)
    category = _make_category(db_session)
    product = Product(
        name="Widget", description="A description that is long enough", price="1.00",
        stock_quantity=1, category_id=category.id, images=["https://example.com/a.png"],
    )
    db_session.add(product)
    db_session.commit()
    variant = ProductVariant(product_id=product.id, sku="", stock_quantity=0)
    db_session.add(variant)
    db_session.commit()
    base = f"{ADMIN_PREFIX}/products/{product.id}/variants"

    listing = _ok(client.get(base, headers=headers))
    detail = _ok(client.get(f"{base}/{variant.id}", headers=headers))

    assert [item["sku"] for item in listing["items"]] == [""]
    assert detail["sku"] == ""


@pytest.mark.parametrize("name,level", [("x", 0), ("n" * 150, 0), ("Shelf", -1)])
def test_a_category_below_the_input_rules_is_served(client, db_session, name, level):
    category = Category(name=name, parent_id=None, level=level, path=[])
    db_session.add(category)
    db_session.commit()

    listing = _ok(client.get(f"{CATALOG_PREFIX}/categories"))
    detail = _ok(client.get(f"{CATALOG_PREFIX}/categories/{category.id}"))
    tree = _ok(client.get(f"{CATALOG_PREFIX}/categories/tree"))

    assert [item["id"] for item in listing["items"]] == [category.id]
    assert detail["name"] == name
    assert tree["total_categories"] == 1


BAD_ADDRESS = dict(
    street="abc", city="x", state="x", postal_code="12", country="x",
    delivery_instructions="i" * 501, delivery_phone="123",
)


def test_an_address_below_the_input_rules_is_served(client, db_session):
    customer = _customer(db_session)
    user, address = customer
    for field, value in BAD_ADDRESS.items():
        setattr(address, field, value)
    db_session.commit()
    headers = _headers(client, user.email)
    order = _order(db_session, customer)

    listing = _ok(client.get(f"{USERS}/addresses", headers=headers))
    detail = _ok(client.get(f"{USERS}/addresses/{address.id}", headers=headers))
    in_order = _ok(client.get(f"{ORDERS}/{order.id}", headers=headers))

    assert [item["id"] for item in listing["items"]] == [address.id]
    assert detail["street"] == "abc"
    assert detail["delivery_phone"] == "123"
    assert detail["delivery_instructions"] == "i" * 501
    assert in_order["shipping_address"]["street"] == "abc"


def test_a_stored_phone_that_normalises_is_still_normalised(client, db_session):
    customer = _customer(db_session)
    user, address = customer
    address.delivery_phone = "+49 (30) 1234 5678"
    db_session.commit()

    detail = _ok(client.get(f"{USERS}/addresses/{address.id}", headers=_headers(client, user.email)))

    assert detail["delivery_phone"] == "+493012345678"


@pytest.mark.parametrize("full_name,phone", [("x", None), (None, "abc"), ("n" * 150, "123")])
def test_a_user_below_the_input_rules_is_served(client, db_session, full_name, phone):
    user, _ = _customer(db_session)
    user.full_name, user.phone = full_name, phone
    db_session.commit()

    me = _ok(client.get(f"{USERS}/me", headers=_headers(client, user.email)))

    assert (me["full_name"], me["phone"]) == (full_name, phone)


def test_the_admin_order_list_serves_such_an_address(client, db_session):
    customer = _customer(db_session)
    user, address = customer
    for field, value in BAD_ADDRESS.items():
        setattr(address, field, value)
    db_session.commit()
    _order(db_session, customer)
    db_session.expire_all()
    headers = _admin_headers(client, db_session)

    listing = _ok(client.get(f"{ADMIN_PREFIX}/orders", headers=headers))

    assert listing["items"][0]["shipping_address"]["city"] == "x"


# --- the request schemas keep every rule ----------------------------------------------

def _post(client, headers, path, body):
    return client.post(path, json=body, headers=headers)


def test_the_admin_routes_still_refuse_what_the_read_now_accepts(client, db_session):
    headers = _admin_headers(client, db_session)
    category = _make_category(db_session)
    product = _valid_product_payload(category.id)

    refused = [
        _post(client, headers, f"{ADMIN_PREFIX}/products", {**product, "name": "ab"}),
        _post(client, headers, f"{ADMIN_PREFIX}/products", {**product, "description": "short"}),
        _post(client, headers, f"{ADMIN_PREFIX}/products", {**product, "name": "n" * 201}),
        _post(client, headers, f"{ADMIN_PREFIX}/products", {
            **product, "characteristics": {"k": {"name": "", "value": 1}}}),
        _post(client, headers, f"{ADMIN_PREFIX}/categories", {"name": "x"}),
        _post(client, headers, f"{ADMIN_PREFIX}/categories", {"name": "n" * 101}),
    ]

    assert [r.status_code for r in refused] == [422] * len(refused)


def test_a_variant_without_a_sku_is_still_refused(client, db_session):
    headers = _admin_headers(client, db_session)
    category = _make_category(db_session)
    product = Product(
        name="Widget", description="A description that is long enough", price="1.00",
        stock_quantity=1, category_id=category.id, images=["https://example.com/a.png"],
    )
    db_session.add(product)
    db_session.commit()
    path = f"{ADMIN_PREFIX}/products/{product.id}/variants"

    assert _post(client, headers, path, {"sku": ""}).status_code == 422
    assert _post(client, headers, path, {"sku": "s" * 101}).status_code == 422


@pytest.mark.parametrize("field,value", [
    ("street", "abc"), ("city", "x"), ("state", "x"), ("postal_code", "12"),
    ("country", "x"), ("street", "s" * 256), ("delivery_instructions", "i" * 501),
    ("delivery_phone", "123"),
])
def test_an_address_is_still_refused_below_the_input_rules(client, db_session, field, value):
    user, _ = _customer(db_session)
    body = dict(street="12 Main Street", city="Springfield", state="IL",
                postal_code="62701", country="US")

    response = client.post(f"{USERS}/addresses", json={**body, field: value},
                           headers=_headers(client, user.email))

    assert response.status_code == 422, response.text


@pytest.mark.parametrize("field,value", [
    ("full_name", "x"), ("full_name", "n" * 101), ("phone", "abc"), ("phone", "+123"),
])
def test_a_registration_is_still_refused_below_the_input_rules(client, field, value):
    body = {"email": "new@example.com", "password": "Str0ngPass1", "full_name": "New User",
            "gdpr_consent": True, "privacy_policy_accepted": True, field: value}

    assert client.post(f"{USERS}/register", json=body).status_code == 422


# --- an admin write is committed only once its response is built ---------------------------

class _ResponseCannotBeBuilt(Exception):
    pass


class _Broken:
    @staticmethod
    def model_validate(*args, **kwargs):
        raise _ResponseCannotBeBuilt


def test_a_category_create_whose_response_cannot_be_built_stores_nothing(
    client, db_session, monkeypatch
):
    headers = _admin_headers(client, db_session)
    monkeypatch.setattr("app.api.v1.admin.CategoryResponse", _Broken)

    with pytest.raises(_ResponseCannotBeBuilt):
        client.post(f"{ADMIN_PREFIX}/categories", json={"name": "Garden"}, headers=headers)

    assert db_session.query(Category).count() == 0


def _stored_variant(db_session):
    category = _make_category(db_session)
    product = Product(
        name="Widget", description="A description that is long enough", price="1.00",
        stock_quantity=1, category_id=category.id, images=["https://example.com/a.png"],
    )
    db_session.add(product)
    db_session.commit()
    variant = ProductVariant(product_id=product.id, sku="KEEP", stock_quantity=3)
    db_session.add(variant)
    db_session.commit()
    return product, variant


def test_a_variant_create_whose_response_cannot_be_built_stores_nothing(
    client, db_session, monkeypatch
):
    headers = _admin_headers(client, db_session)
    product, _ = _stored_variant(db_session)
    monkeypatch.setattr("app.api.v1.admin.ProductVariantResponse", _Broken)

    with pytest.raises(_ResponseCannotBeBuilt):
        client.post(f"{ADMIN_PREFIX}/products/{product.id}/variants", json={"sku": "NEW"},
                    headers=headers)

    assert [v.sku for v in db_session.query(ProductVariant).all()] == ["KEEP"]


def test_a_variant_update_whose_response_cannot_be_built_changes_nothing(
    client, db_session, monkeypatch
):
    headers = _admin_headers(client, db_session)
    product, variant = _stored_variant(db_session)
    monkeypatch.setattr("app.api.v1.admin.ProductVariantResponse", _Broken)

    with pytest.raises(_ResponseCannotBeBuilt):
        client.patch(f"{ADMIN_PREFIX}/products/{product.id}/variants/{variant.id}",
                     json={"stock_quantity": 99}, headers=headers)

    db_session.expire_all()
    assert db_session.get(ProductVariant, variant.id).stock_quantity == 3


@pytest.mark.parametrize("schema", [AddressCreate, AddressUpdate])
def test_a_country_that_outgrows_the_limit_when_cased_is_refused_on_input(schema):
    # "ß".title() is "Ss": the length limit runs before the normalisation, so
    # the input schemas check it again after it.
    body = {"country": "ß" * 100}
    if schema is AddressCreate:
        body |= dict(street="12 Main Street", city="Springfield", state="IL", postal_code="62701")

    with pytest.raises(ValidationError):
        schema(**body)
