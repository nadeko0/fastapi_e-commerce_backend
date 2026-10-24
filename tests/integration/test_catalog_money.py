"""A catalog price is what Numeric(10, 2) holds: at most 10 digits, whole
cents. A larger one used to answer 500 (the quantize in the validator raises
decimal.InvalidOperation past 28 digits, or the database refuses the value)
and a fraction of a cent was rounded away without a word: 10.999 was stored
as 11.00."""

import pytest
from payment_support import Shop

API = "/api/v1"

# (what the client sends, what the API answers)
ACCEPTED = [
    ("19.99", "19.99"),
    ("19.9", "19.90"),
    ("10.500", "10.50"),
    ("0", "0.00"),
    ("99999999.99", "99999999.99"),
]
# Too many digits, too many places; and a JSON number, whatever it says (a
# price is a decimal string since 4.0.0, see test_strict_input.py).
REFUSED = [1e30, "1e30", "100000000000", 123456789.01, 10.999, "10.999", "0.001", 99999999.999]


def _product_body(category_id, price):
    return {
        "name": "Test Widget",
        "description": "A fine widget for all your widget needs",
        "price": price,
        "stock_quantity": 1,
        "category_id": category_id,
        "images": ["https://example.com/widget.png"],
        "characteristics": {},
    }


@pytest.fixture
def shop(client, db_session):
    shop = Shop(client, db_session)
    shop.headers = shop.admin()
    shop.product_row = shop.product()
    # Shop's description is shorter than ProductBase allows in a response.
    shop.product_row.description = "A fine widget for all your widget needs"
    db_session.commit()
    return shop


def _create_product(shop, price):
    return shop.client.post(f"{API}/admin/products", headers=shop.headers,
                            json=_product_body(shop.product_row.category_id, price))


def _update_product(shop, price):
    return shop.client.put(f"{API}/admin/products/{shop.product_row.id}",
                           headers=shop.headers, json={"price": price})


def _create_variant(shop, price):
    return shop.client.post(
        f"{API}/admin/products/{shop.product_row.id}/variants", headers=shop.headers,
        json={"sku": "SKU-MONEY", "price_override": price},
    )


def _update_variant(shop, price):
    variant = shop.variant(shop.product_row)
    return shop.client.put(
        f"{API}/admin/products/{shop.product_row.id}/variants/{variant.id}",
        headers=shop.headers, json={"price_override": price},
    )


WRITES = [
    pytest.param(_create_product, "price", id="create-product"),
    pytest.param(_update_product, "price", id="update-product"),
    pytest.param(_create_variant, "price_override", id="create-variant"),
    pytest.param(_update_variant, "price_override", id="update-variant"),
]


@pytest.mark.parametrize("write, field", WRITES)
@pytest.mark.parametrize("price", REFUSED)
def test_a_price_the_column_cannot_hold_is_422(shop, write, field, price):
    response = write(shop, price)

    assert response.status_code == 422, response.text
    assert any(error["loc"][-1] == field for error in response.json()["errors"])


@pytest.mark.parametrize("write, field", WRITES)
@pytest.mark.parametrize("sent, stored", ACCEPTED)
def test_a_price_the_column_holds_is_stored_as_sent(shop, write, field, sent, stored):
    response = write(shop, sent)

    assert response.status_code in (200, 201), response.text
    assert response.json()[field] == stored


@pytest.mark.parametrize("param", ["min_price", "max_price"])
def test_a_price_filter_beyond_the_column_is_not_a_server_error(shop, param):
    response = shop.client.get(f"{API}/products", params={param: "1e30"})

    assert response.status_code < 500
