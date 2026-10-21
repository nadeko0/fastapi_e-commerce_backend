"""
app/schemas/order.py and the order models without a database: how an order
line becomes an OrderItemResponse from each shape it can arrive in, and the
validators of the payment-facing request bodies (PaymentCreate,
RefundCreate, CheckoutSessionCreate) at the edges of what they accept.
"""
from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.order_items import OrderItem
from app.models.product import Product, ProductVariant
from app.schemas.order import (
    CheckoutSessionCreate,
    OrderItemResponse,
    PaymentCreate,
    RefundCreate,
)

CREATED = datetime(2026, 1, 2, 3, 4, 5)


def _line(**overrides):
    values = dict(id=7, order_id=3, product_id=11, variant_id=None, quantity=2,
                  price_at_time=Decimal("9.50"), created_at=CREATED)
    values.update(overrides)
    return OrderItem(**values)


def _product(images):
    return Product(id=11, name="Widget", description="d", price=Decimal("9.50"),
                   category_id=1, images=images)


# -- OrderItemResponse ----------------------------------------------------------

def test_an_order_line_takes_its_display_fields_from_its_product():
    line = _line(product=_product(["https://img.example/w.png", "https://img.example/2.png"]))

    item = OrderItemResponse.model_validate(line)

    assert (item.product_name, item.product_image) == ("Widget", "https://img.example/w.png")
    assert (item.id, item.order_id, item.quantity, item.price_at_time) == (
        7, 3, 2, Decimal("9.50")
    )
    assert item.variant_sku is None


def test_a_product_without_images_gives_an_empty_image():
    item = OrderItemResponse.model_validate(_line(product=_product([])))
    assert item.product_image == ""


def test_a_variant_line_carries_the_variant_sku():
    variant = ProductVariant(id=5, product_id=11, sku="W-RED-M")
    line = _line(variant_id=5, variant=variant, product=_product([]))

    assert OrderItemResponse.model_validate(line).variant_sku == "W-RED-M"


def test_a_line_whose_product_is_not_loaded_falls_back_to_the_model_properties():
    # An OrderItem built with only product_id (its `product` relationship
    # not set, as before a flush/refresh) is handed to pydantic as is: the
    # read-only properties on the model then answer product_name and
    # product_image with empty strings instead of failing validation.
    line = _line()
    assert line.product is None

    item = OrderItemResponse.model_validate(line)

    assert (item.product_name, item.product_image, item.variant_sku) == ("", "", None)


def test_the_model_properties_read_through_the_product_relationship():
    line = _line(product=_product(["a.png"]))
    assert (line.product_name, line.product_image) == ("Widget", "a.png")
    line.product.images = []
    assert line.product_image == ""


def test_an_already_shaped_dict_is_validated_as_given():
    item = OrderItemResponse.model_validate({
        "id": 1, "order_id": 2, "product_id": 3, "quantity": 1, "price_at_time": "1.5",
        "created_at": CREATED, "product_name": "Given", "product_image": "given.png",
    })
    assert (item.product_name, item.price_at_time) == ("Given", Decimal("1.50"))


# -- RefundCreate / PaymentCreate / CheckoutSessionCreate ----------------------

def test_a_refund_without_an_amount_is_a_full_refund():
    assert RefundCreate().amount is None
    # An explicit JSON null reaches the validator, which passes it through.
    assert RefundCreate.model_validate({"amount": None}).amount is None


@pytest.mark.parametrize("amount", ["0", "-0.01", "-5"])
def test_a_refund_amount_must_be_positive(amount):
    with pytest.raises(ValidationError):
        RefundCreate(amount=amount)


def test_amounts_are_normalised_to_cents():
    assert RefundCreate(amount="5").amount == Decimal("5.00")
    assert PaymentCreate(order_id=1, payment_method="stripe", amount=5).amount == Decimal("5.00")


def test_checkout_redirects_may_be_left_out_or_null():
    assert CheckoutSessionCreate().success_url is None
    body = CheckoutSessionCreate.model_validate({"success_url": None, "cancel_url": None})
    assert (body.success_url, body.cancel_url) == (None, None)


# An amount too large for the validator's quantize (a 28-digit Decimal
# context) raised decimal.InvalidOperation out of the validator. That is
# not a ValueError, so pydantic did not turn it into a validation error:
# POST /orders/{id}/pay or /refund with {"amount": 1e30} answered 500.
# Order totals are Numeric(10, 2), so no amount past that is ever payable.
@pytest.mark.parametrize("amount", ["1e30", "1E+400", "123456789012345678901234567.89",
                                    "10000000000", "123456789.123"])
def test_an_amount_beyond_any_order_total_is_a_validation_error(amount):
    with pytest.raises(ValidationError):
        PaymentCreate(order_id=1, payment_method="stripe", amount=amount)
    with pytest.raises(ValidationError):
        RefundCreate(amount=amount)


# A fraction of a cent was rounded away, silently, after the gt=0 check had
# passed: a refund of 0.001 became a refund of 0.00, and 99.995 was taken
# as 100.00 (half-even). Money with more than two decimals is refused.
@pytest.mark.parametrize("amount", ["0.001", "0.004", "0.005", "99.995", "10.001"])
def test_a_fraction_of_a_cent_is_refused_not_rounded(amount):
    with pytest.raises(ValidationError):
        RefundCreate(amount=amount)
    with pytest.raises(ValidationError):
        PaymentCreate(order_id=1, payment_method="stripe", amount=amount)


@pytest.mark.parametrize("amount,cents", [("0.01", "0.01"), ("10.5", "10.50"),
                                          ("10.500", "10.50"), ("7", "7.00")])
def test_whole_cents_are_accepted_however_they_are_written(amount, cents):
    assert RefundCreate(amount=amount).amount == Decimal(cents)
    assert PaymentCreate(order_id=1, payment_method="stripe",
                         amount=amount).amount == Decimal(cents)


def test_the_largest_order_total_is_still_accepted():
    assert RefundCreate(amount="99999999.99").amount == Decimal("99999999.99")
    assert PaymentCreate(order_id=1, payment_method="stripe",
                         amount="99999999.99").amount == Decimal("99999999.99")
