"""serialize() writes with model_dump_json(); it used to write
json.dumps(model.model_dump(mode="json"), default=str). Both are JSON for
the same data, so a cart stored by one release loads in the other (an
upgrade must read old carts, a rollback must read new ones).
"""

import json
from decimal import Decimal

import pytest

import app.services.redis_codec as codec
from app.schemas.common import Cart


def _old_serialize(model) -> str:
    return json.dumps(model.model_dump(mode="json"), default=str)


def _old_deserialize_cart(raw: str) -> Cart:
    # The previous reader: json.loads, then model_validate on the dict.
    return Cart.model_validate(json.loads(raw))


@pytest.fixture
def cart() -> Cart:
    cart = Cart(user_id=7, expires_at="2030-01-01T00:00:00Z")
    cart.add_item(product_id=1, quantity=2, price=Decimal("19.99"), name="Café ☕", image="http://a.example/x.png")
    cart.add_item(product_id=2, quantity=1, price=Decimal("5"), variant_id=3)
    return cart


def test_old_value_is_read_by_new_code(cart):
    assert codec.deserialize(_old_serialize(cart), Cart) == cart


def test_new_value_is_read_by_old_code(cart):
    assert _old_deserialize_cart(codec.serialize(cart)) == cart


def test_new_value_round_trips(cart):
    assert codec.deserialize(codec.serialize(cart), Cart) == cart


def test_both_writers_store_the_same_json_document(cart):
    assert json.loads(codec.serialize(cart)) == json.loads(_old_serialize(cart))


def test_prices_stay_decimal_strings(cart):
    stored = json.loads(codec.serialize(cart))
    assert stored["items"]["1"]["price_snapshot"] == "19.99"
    assert stored["items"]["2:3"]["price_snapshot"] == "5.00"
