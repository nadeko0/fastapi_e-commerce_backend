"""Validation rules of app/schemas/product.py and app/schemas/address.py,
the rules an admin's product edits and a customer's addresses go through."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from app.schemas.address import AddressCreate, AddressUpdate
from app.schemas.product import (
    ProductCreate,
    ProductFilter,
    ProductSearch,
    ProductUpdate,
    ProductVariantCreate,
    ProductVariantUpdate,
)

_PRODUCT = {
    "name": "Widget",
    "description": "A fine widget for all needs",
    "price": "9.99",
    "stock_quantity": 3,
    "category_id": 1,
    "images": ["https://example.com/a.png"],
}

_ADDRESS = {
    "street": "1 Main Street",
    "city": "Berlin",
    "state": "Berlin",
    "postal_code": "10115",
    "country": "de",
}


# --- products -----------------------------------------------------------------


@given(st.decimals(min_value=0, max_value=Decimal("99999999.99"), places=2, allow_nan=False))
def test_a_price_is_kept_to_the_cent(price):
    created = ProductCreate(**{**_PRODUCT, "price": price})
    assert created.price == price
    assert created.price.as_tuple().exponent == -2


@pytest.mark.parametrize("price,expected", [("10", "10.00"), ("0.1", "0.10"), ("2.50", "2.50")])
def test_a_price_is_written_to_two_places(price, expected):
    assert str(ProductCreate(**{**_PRODUCT, "price": price}).price) == expected


@pytest.mark.parametrize("price", ["2.005", "2.015", "0.001"])
def test_a_fraction_of_a_cent_is_refused_not_rounded(price):
    # Numeric(10,2) cannot hold it; rounding it would change the price silently.
    with pytest.raises(ValidationError):
        ProductCreate(**{**_PRODUCT, "price": price})


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", "-0.01"), ("stock_quantity", -1), ("name", "ab"), ("name", "x" * 201),
        ("description", "too short"), ("images", []), ("images", ["not a url"]),
    ],
)
def test_product_create_refuses(field, value):
    with pytest.raises(ValidationError):
        ProductCreate(**{**_PRODUCT, field: value})


def test_product_images_are_deduplicated_in_order_and_stored_as_strings():
    images = ["https://example.com/b.png", "https://example.com/a.png", "https://example.com/b.png"]
    assert ProductCreate(**{**_PRODUCT, "images": images}).images == [
        "https://example.com/b.png", "https://example.com/a.png",
    ]


def test_product_update_takes_any_subset():
    assert ProductUpdate().model_dump(exclude_unset=True) == {}
    update = ProductUpdate(price="3", images=["https://example.com/a.png"] * 2)
    assert update.price == Decimal("3.00")
    assert update.images == ["https://example.com/a.png"]


@pytest.mark.parametrize(
    "field", ["name", "description", "price", "stock_quantity", "category_id", "images", "characteristics"]
)
def test_product_update_refuses_an_explicit_null(field):
    with pytest.raises(ValidationError, match="cannot be null"):
        ProductUpdate(**{field: None})


@pytest.mark.parametrize("field,value", [("images", []), ("price", "-1"), ("stock_quantity", -5)])
def test_product_update_refuses_what_create_refuses(field, value):
    with pytest.raises(ValidationError):
        ProductUpdate(**{field: value})


def test_variant_price_override_is_optional_and_kept_to_the_cent():
    assert ProductVariantCreate(sku="S-1").price_override is None
    assert ProductVariantCreate(sku="S-1", price_override="4.5").price_override == Decimal("4.50")
    with pytest.raises(ValidationError):
        ProductVariantCreate(sku="S-1", price_override="-0.01")
    with pytest.raises(ValidationError):
        ProductVariantCreate(sku="", price_override="1")


def test_variant_update_null_clears_only_the_price_override():
    assert ProductVariantUpdate(price_override=None).price_override is None
    assert ProductVariantUpdate(price_override="7").price_override == Decimal("7.00")
    for field in ("sku", "attributes", "stock_quantity", "is_active"):
        with pytest.raises(ValidationError, match="cannot be null"):
            ProductVariantUpdate(**{field: None})


@pytest.mark.parametrize(
    "min_price,max_price,ok",
    [(None, None, True), ("1", None, True), (None, "1", True), ("1", "1", True), ("1", "2", True), ("2", "1", False)],
)
def test_filter_price_range(min_price, max_price, ok):
    params = {k: v for k, v in (("min_price", min_price), ("max_price", max_price)) if v is not None}
    if ok:
        ProductFilter(**params)
    else:
        with pytest.raises(ValidationError, match="max_price must be greater than min_price"):
            ProductFilter(**params)


@given(st.text(min_size=3, max_size=100).filter(lambda s: len(s.strip()) == len(s)))
def test_search_text_between_3_and_100_characters_is_accepted(query):
    assert ProductSearch(query=query).query == query


# --- addresses ----------------------------------------------------------------


@pytest.mark.parametrize(
    "postal_code,expected", [("10115", "10115"), (" 101 15 ", "10115"), ("SW1A 1AA", "SW1A1AA")]
)
def test_postal_code_loses_its_spaces(postal_code, expected):
    assert AddressCreate(**{**_ADDRESS, "postal_code": postal_code}).postal_code == expected
    assert AddressUpdate(postal_code=postal_code).postal_code == expected


@pytest.mark.parametrize("schema", [AddressCreate, AddressUpdate])
def test_a_postal_code_that_is_mostly_spaces_is_too_short(schema):
    payload = {**_ADDRESS, "postal_code": "1  2"} if schema is AddressCreate else {"postal_code": "1  2"}
    with pytest.raises(ValidationError, match="Invalid postal code length"):
        schema(**payload)


@pytest.mark.parametrize("country,expected", [("de", "DE"), ("germany", "Germany"), ("UNITED KINGDOM", "United Kingdom")])
def test_country_code_upper_case_name_title_case(country, expected):
    assert AddressCreate(**{**_ADDRESS, "country": country}).country == expected
    assert AddressUpdate(country=country).country == expected


@pytest.mark.parametrize(
    "phone,expected",
    [("+49 30 1234567", "+49301234567"), ("(415) 555-2671", "+4155552671"), ("", ""), (None, None)],
)
def test_delivery_phone_is_normalised(phone, expected):
    assert AddressCreate(**{**_ADDRESS, "delivery_phone": phone}).delivery_phone == expected
    assert AddressUpdate(delivery_phone=phone).delivery_phone == expected


@pytest.mark.parametrize("phone", ["1234", "1" * 16, "call me"])
def test_delivery_phone_of_the_wrong_length_is_refused(phone):
    with pytest.raises(ValidationError, match="Invalid phone number length"):
        AddressCreate(**{**_ADDRESS, "delivery_phone": phone})
    with pytest.raises(ValidationError, match="Invalid phone number length"):
        AddressUpdate(delivery_phone=phone)


@pytest.mark.parametrize("field", ["street", "city", "state", "postal_code", "country", "address_type", "is_default"])
def test_address_update_refuses_an_explicit_null(field):
    with pytest.raises(ValidationError, match="cannot be null"):
        AddressUpdate(**{field: None})


def test_address_update_null_clears_the_optional_fields():
    update = AddressUpdate(delivery_instructions=None, delivery_phone=None)
    assert update.model_dump(exclude_unset=True) == {"delivery_instructions": None, "delivery_phone": None}


@pytest.mark.parametrize(
    "field,value",
    [("street", "abc"), ("city", "B"), ("country", "D"), ("address_type", "castle"), ("delivery_instructions", "x" * 501)],
)
def test_address_create_refuses(field, value):
    with pytest.raises(ValidationError):
        AddressCreate(**{**_ADDRESS, field: value})
