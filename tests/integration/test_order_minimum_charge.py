"""An order below Stripe's minimum charge for its currency is not placed.

It used to be: it held its stock until it expired, and every /pay was refused
by Stripe (https://docs.stripe.com/currencies, minimum charge amounts)."""
import pytest
from payment_support import Shop, reset_provider

from app.core.config import settings


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.mark.parametrize("price,quantity,placed", [
    ("0.49", 1, False), ("0.00", 1, False), ("0.25", 2, True), ("0.50", 1, True),
])
def test_an_order_below_the_minimum_charge_is_refused_and_takes_no_stock(
    client, db_session, price, quantity, placed
):
    shop = Shop(client, db_session)
    product = shop.product(price=price, stock=5)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product, quantity).status_code == 200

    response = shop.place_order(buyer)

    assert response.status_code == (201 if placed else 422), response.text
    assert shop.stock(product) == (5 - quantity if placed else 5)
    if not placed:
        assert "minimum charge of 0.50 USD" in response.json()["detail"]


def test_a_currency_without_a_published_minimum_is_left_to_the_provider(
    client, db_session, monkeypatch
):
    # Its minimum depends on the conversion to the account's settlement
    # currency, which the application does not know.
    monkeypatch.setattr(settings, "STORE_CURRENCY", "KES")
    shop = Shop(client, db_session)
    product = shop.product(price="0.10", stock=5)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product).status_code == 200

    assert shop.place_order(buyer).status_code == 201
