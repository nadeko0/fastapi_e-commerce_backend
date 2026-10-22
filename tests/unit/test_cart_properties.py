"""Properties of cart pricing and cart arithmetic, over generated carts
(hypothesis). The example-based tests are in tests/unit/test_cart_schema.py
and tests/integration/test_cart.py; these check that the rules hold for any
mix of lines, prices, variants and availability."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from app.core.clock import utc_now
from app.schemas.common import Cart, CartItem, CartResponse

_MONEY = st.decimals(min_value=0, max_value=Decimal("9999.99"), places=2, allow_nan=False)

# One line: product id, quantity, the product's price, whether the product
# is on sale, and optionally a variant (its price override or None, and
# whether it is on sale).
_LINE = st.tuples(
    st.integers(min_value=1, max_value=40),
    st.integers(min_value=1, max_value=50),
    _MONEY,
    st.booleans(),
    st.none() | st.tuples(st.none() | _MONEY, st.booleans()),
)


def _build(lines):
    cart = Cart(user_id=1, expires_at=utc_now() + timedelta(days=1))
    products, variants, expected = {}, {}, Decimal("0.00")
    for index, (product_id, quantity, price, product_on_sale, variant) in enumerate(lines):
        if product_id in products:
            continue  # one product row per id; keeps the expectation simple
        products[product_id] = SimpleNamespace(
            price=price, is_active=product_on_sale, name=f"P{product_id}", images=[],
        )
        variant_id = None
        unit_price, on_sale = price, product_on_sale
        if variant is not None:
            override, variant_on_sale = variant
            variant_id = 1000 + index
            variants[variant_id] = SimpleNamespace(
                product_id=product_id, price_override=override, is_active=variant_on_sale,
            )
            unit_price = override if override is not None else price
            on_sale = product_on_sale and variant_on_sale
        cart.add_item(product_id, quantity, price=price, variant_id=variant_id)
        if on_sale:
            expected += unit_price * quantity
    return cart, products, variants, expected


@given(st.lists(_LINE, max_size=15))
def test_the_total_is_the_sum_of_the_available_lines_and_exact(lines):
    cart, products, variants, expected = _build(lines)

    priced = CartResponse.from_cart(cart, products, variants)

    assert priced.total_amount == expected
    assert priced.total_amount == sum(
        (line.line_total for line in priced.items if line.available), Decimal("0.00")
    )
    assert priced.items_count == len(cart.items)
    for line in priced.items:
        if line.available:
            assert line.line_total == line.unit_price * line.quantity
        else:
            assert (line.unit_price, line.line_total, line.price_changed) == (None, None, False)


@given(st.lists(_LINE, min_size=1, max_size=10), _MONEY)
def test_a_price_change_is_reported_exactly_when_the_price_differs(lines, new_price):
    cart, products, variants, _ = _build(lines)
    for product in products.values():
        product.price = new_price

    for line in CartResponse.from_cart(cart, products, variants).items:
        if line.available:
            assert line.price_changed is (line.price_when_added != line.unit_price)


@given(st.lists(st.integers(min_value=1, max_value=20), min_size=1, max_size=20))
def test_adding_the_same_line_again_adds_up_its_quantity(quantities):
    cart = Cart(user_id=1, expires_at=utc_now() + timedelta(days=1))
    for quantity in quantities:
        cart.add_item(7, quantity, price=Decimal("1.00"))
        cart.add_item(7, quantity, price=Decimal("1.00"), variant_id=3)

    assert len(cart.items) == 2
    assert {item.quantity for item in cart.items.values()} == {sum(quantities)}


@given(st.integers(max_value=0))
def test_a_line_never_holds_a_quantity_below_one(quantity):
    with pytest.raises(ValidationError):
        CartItem(product_id=1, quantity=quantity)
    item = CartItem(product_id=1, quantity=1)
    with pytest.raises(ValidationError):
        item.quantity = quantity
    assert item.quantity == 1
