from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.clock import utc_now
from app.schemas.common import Cart, CartItem, CartResponse


def _cart_with_one_line(quantity=3):
    cart = Cart(user_id=1, expires_at=utc_now() + timedelta(days=1))
    cart.add_item(product_id=7, quantity=quantity, price=Decimal("1.50"), name="n", image="i")
    return cart


@pytest.mark.parametrize("quantity", [0, -1])
def test_update_quantity_refuses_a_non_positive_quantity(quantity):
    """Regression test: CartItem.quantity is gt=0, but that was only checked
    on construction - assigning to it afterwards stored anything, and the
    cart then failed validation (and read back as empty) once it had been
    through Redis."""
    cart = _cart_with_one_line()

    with pytest.raises(ValidationError):
        cart.update_quantity(7, quantity)

    assert cart.items["7"].quantity == 3


@pytest.mark.parametrize("quantity", [-3, -4])
def test_add_item_refuses_to_take_an_existing_line_to_zero_or_below(quantity):
    cart = _cart_with_one_line()

    with pytest.raises(ValidationError):
        cart.add_item(product_id=7, quantity=quantity, price=Decimal("1.50"), name="n", image="i")

    assert cart.items["7"].quantity == 3


def test_a_cart_that_passed_the_schema_survives_a_json_round_trip():
    cart = _cart_with_one_line()
    cart.update_quantity(7, 5)

    assert Cart.model_validate_json(cart.model_dump_json()).items["7"].quantity == 5


# --- Live pricing (CartResponse.from_cart) ---------------------------------
#
# The pricing itself is a pure function of the stored cart and the catalog
# rows loaded for it, so it is tested here without HTTP, Redis or a database.

def _product(price, name="Current name", images=("current.png",), is_active=True):
    return SimpleNamespace(
        price=Decimal(price), name=name, images=list(images), is_active=is_active,
        stock_quantity=10,
    )


def _variant(product_id, price_override=None, is_active=True):
    return SimpleNamespace(
        product_id=product_id,
        price_override=None if price_override is None else Decimal(price_override),
        is_active=is_active,
        stock_quantity=10,
    )


def _cart(*lines):
    cart = Cart(user_id=1, expires_at=utc_now() + timedelta(days=1))
    for product_id, variant_id, quantity, price in lines:
        cart.add_item(
            product_id=product_id, variant_id=variant_id, quantity=quantity,
            price=Decimal(price), name="Name when added", image="added.png",
        )
    return cart


def test_pricing_uses_the_catalog_price_not_the_recorded_one():
    cart = _cart((7, None, 2, "9.99"))

    priced = CartResponse.from_cart(cart, {7: _product("12.50")}, {})

    line = priced.items[0]
    assert (line.unit_price, line.line_total) == (Decimal("12.50"), Decimal("25.00"))
    assert priced.total_amount == Decimal("25.00")
    assert (line.price_when_added, line.price_changed) == (Decimal("9.99"), True)
    assert (line.name, line.image) == ("Current name", "current.png")


def test_pricing_money_is_decimal_and_exact_where_float_is_not():
    assert 0.1 * 3 != 0.3  # what the float total used to do
    cart = _cart((7, None, 3, "0.10"), (8, None, 7, "0.03"), (9, None, 3, "10.10"))
    products = {7: _product("0.10"), 8: _product("0.03"), 9: _product("10.10")}

    priced = CartResponse.from_cart(cart, products, {})

    assert [line.line_total for line in priced.items] == [
        Decimal("0.30"), Decimal("0.21"), Decimal("30.30"),
    ]
    assert priced.total_amount == Decimal("30.81")
    for value in [priced.total_amount] + [line.unit_price for line in priced.items]:
        assert type(value) is Decimal
    assert priced.model_dump(mode="json")["total_amount"] == "30.81"


def test_pricing_prefers_the_variant_override_and_falls_back_to_the_product():
    cart = _cart((7, 1, 1, "14.50"), (7, 2, 1, "9.99"))
    variants = {1: _variant(7, "14.50"), 2: _variant(7, None)}

    priced = CartResponse.from_cart(cart, {7: _product("9.99")}, variants)

    assert [line.unit_price for line in priced.items] == [Decimal("14.50"), Decimal("9.99")]
    assert [line.price_changed for line in priced.items] == [False, False]


def test_pricing_a_zero_price_override_is_a_price_not_a_missing_one():
    cart = _cart((7, 1, 2, "0.00"))

    priced = CartResponse.from_cart(cart, {7: _product("9.99")}, {1: _variant(7, "0.00")})

    assert priced.items[0].unit_price == Decimal("0.00")
    assert priced.total_amount == Decimal("0.00")


@pytest.mark.parametrize("products, variants, reason", [
    ({}, {}, "product_unavailable"),
    ({7: _product("9.99", is_active=False)}, {1: _variant(7)}, "product_unavailable"),
    ({7: _product("9.99")}, {}, "variant_unavailable"),
    ({7: _product("9.99")}, {1: _variant(7, is_active=False)}, "variant_unavailable"),
    # A variant id that exists but belongs to another product.
    ({7: _product("9.99")}, {1: _variant(8)}, "variant_unavailable"),
])
def test_pricing_marks_unavailable_lines_and_keeps_them_out_of_the_total(products, variants, reason):
    cart = _cart((7, 1, 2, "9.99"), (5, None, 1, "1.25"))

    priced = CartResponse.from_cart(cart, {**products, 5: _product("1.25")}, variants)

    gone, kept = priced.items
    assert (gone.available, gone.unavailable_reason) == (False, reason)
    assert (gone.unit_price, gone.line_total, gone.price_changed) == (None, None, False)
    assert gone.price_when_added == Decimal("9.99")
    assert kept.available is True
    assert priced.total_amount == Decimal("1.25")
    assert priced.items_count == 2


def test_pricing_a_missing_product_shows_what_was_recorded_when_added():
    cart = _cart((7, None, 1, "9.99"))

    line = CartResponse.from_cart(cart, {}, {}).items[0]

    assert (line.name, line.image) == ("Name when added", "added.png")


def test_pricing_a_line_with_nothing_recorded_reports_no_price_change():
    """The recorded price is optional: a line without one is priced like any
    other and simply has nothing to compare with."""
    cart = Cart(
        user_id=1, expires_at=utc_now() + timedelta(days=1),
        items={"7": CartItem(product_id=7, quantity=2)},
    )

    line = CartResponse.from_cart(cart, {7: _product("9.99", images=())}, {}).items[0]

    assert (line.unit_price, line.price_when_added, line.price_changed) == (Decimal("9.99"), None, False)
    assert line.image is None


# --- The stored line --------------------------------------------------------

def test_old_format_line_with_float_price_loads_as_an_exact_decimal():
    item = CartItem.model_validate({
        "product_id": 7, "variant_id": None, "quantity": 2,
        "added_at": "2026-10-01T10:00:00",
        "price_snapshot": 14.5, "name_snapshot": "n", "image_snapshot": "i",
    })

    assert item.price_snapshot == Decimal("14.50")
    assert item.model_dump(mode="json")["price_snapshot"] == "14.50"


def test_recorded_price_survives_a_json_round_trip_exactly():
    cart = _cart((7, None, 3, "0.10"))

    restored = Cart.model_validate_json(cart.model_dump_json())

    assert restored.items["7"].price_snapshot == Decimal("0.10")
    assert type(restored.items["7"].price_snapshot) is Decimal


def test_adding_to_an_existing_line_re_records_what_was_seen_but_keeps_added_at():
    cart = _cart((7, None, 1, "9.99"))
    first_added_at = cart.items["7"].added_at

    cart.add_item(product_id=7, quantity=2, price=Decimal("12.50"), name="New name", image="new.png")

    line = cart.items["7"]
    assert line.quantity == 3
    assert (line.price_snapshot, line.name_snapshot, line.image_snapshot) == (
        Decimal("12.50"), "New name", "new.png",
    )
    assert line.added_at == first_added_at


def test_cart_has_no_total_of_its_own():
    """A total computed from the stored lines would be a total at recorded
    prices; the only total is the one priced from the catalog."""
    assert not hasattr(_cart((7, None, 1, "9.99")), "total_amount")


# --- Merging one cart into another (a guest cart into an account cart) ------

def _line_quantities(cart):
    return {key: item.quantity for key, item in cart.items.items()}


def test_merge_adds_up_a_line_present_in_both_and_takes_the_rest():
    mine = _cart((7, None, 2, "9.99"), (8, 3, 1, "5.00"))
    theirs = _cart((7, None, 3, "9.99"), (8, None, 1, "5.00"), (9, 4, 2, "1.00"))

    left_out = mine.merge(theirs, max_lines=100)

    assert left_out == 0
    assert _line_quantities(mine) == {"7": 5, "8:3": 1, "8": 1, "9:4": 2}
    # The other cart is read, not changed, and shares no line object.
    assert _line_quantities(theirs) == {"7": 3, "8": 1, "9:4": 2}
    assert mine.items["8"] is not theirs.items["8"]


def test_merged_line_takes_what_the_other_cart_recorded_and_keeps_the_earlier_added_at():
    mine = _cart((7, None, 2, "9.99"))
    theirs = _cart((7, None, 1, "12.50"))
    theirs.items["7"].name_snapshot = "Seen as a guest"
    earlier = mine.items["7"].added_at - timedelta(days=3)
    theirs.items["7"].added_at = earlier

    mine.merge(theirs, max_lines=100)

    line = mine.items["7"]
    assert (line.quantity, line.price_snapshot, line.name_snapshot) == (3, Decimal("12.50"), "Seen as a guest")
    assert line.added_at == earlier


def test_merge_does_not_look_at_stock_or_cap_a_quantity():
    mine = _cart((7, None, 9, "9.99"))
    theirs = _cart((7, None, 8, "9.99"))

    assert mine.merge(theirs, max_lines=1) == 0
    assert mine.items["7"].quantity == 17


def test_merge_stops_taking_new_lines_at_the_cap_but_still_adds_up_shared_ones():
    mine = _cart((7, None, 1, "1.00"), (8, None, 1, "1.00"))
    theirs = _cart((9, None, 1, "1.00"), (10, None, 1, "1.00"), (7, None, 4, "1.00"), (11, None, 1, "1.00"))

    left_out = mine.merge(theirs, max_lines=3)

    assert left_out == 2
    assert _line_quantities(mine) == {"7": 5, "8": 1, "9": 1}


def test_merge_into_a_cart_already_over_the_cap_adds_no_line_and_drops_none_of_its_own():
    mine = _cart((7, None, 1, "1.00"), (8, None, 1, "1.00"), (9, None, 1, "1.00"))
    theirs = _cart((10, None, 1, "1.00"), (8, None, 2, "1.00"))

    assert mine.merge(theirs, max_lines=2) == 1
    assert _line_quantities(mine) == {"7": 1, "8": 3, "9": 1}


def test_a_guest_cart_has_no_user_id_and_survives_a_json_round_trip():
    cart = Cart(expires_at=utc_now() + timedelta(days=1))
    cart.add_item(product_id=7, quantity=2, price=Decimal("1.50"), name="n", image="i")

    restored = Cart.model_validate_json(cart.model_dump_json())

    assert restored.user_id is None
    assert restored.items["7"].quantity == 2
