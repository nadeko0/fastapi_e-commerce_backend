"""
Money at the payment boundary, property-based: every amount the request
schemas accept converts to integer minor units and back without loss, in
every two-decimal currency a store may be configured with, and the mock
provider charges exactly that many minor units in that currency.

Refund arithmetic (partials summing to the capture, never above it) is
property-tested against both providers in test_payment_provider_contract.py.
"""
import uuid
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from app.api.v1.orders import _decimal_to_cents
from app.core.config import (
    THREE_DECIMAL_CURRENCIES,
    WHOLE_UNITS_ONLY_CURRENCIES,
    ZERO_DECIMAL_CURRENCIES,
)
from app.schemas.order import PaymentCreate
from app.services.payment.stripe_provider import StripePaymentProvider

# Two-decimal currencies a store may run in (STORE_CURRENCY): the common
# ones, plus HUF and TWD, which Stripe charges in hundredths although it
# pays them out in whole units (docs.stripe.com/currencies, "Special cases").
STORE_CURRENCIES = ["USD", "EUR", "GBP", "PLN", "HUF", "TWD"]

LARGEST_ORDER_TOTAL = Decimal("99999999.99")  # Order.total_amount is Numeric(10, 2)

# Every amount the request schemas accept for a payment: whole cents from
# one cent up to the largest order total.
order_amounts = st.decimals(min_value=Decimal("0.01"), max_value=LARGEST_ORDER_TOTAL, places=2)

_PROVIDER_STATE = [HealthCheck.function_scoped_fixture]


@pytest.fixture(autouse=True)
def _fresh_mock():
    StripePaymentProvider.reset_mock_state()
    yield
    StripePaymentProvider.reset_mock_state()


def _pay(amount):
    return PaymentCreate(order_id=1, payment_method="stripe", amount=amount).amount


# -- currencies ------------------------------------------------------------------

@pytest.mark.parametrize("currency", STORE_CURRENCIES)
def test_each_store_currency_is_one_the_configuration_accepts(currency):
    assert currency not in ZERO_DECIMAL_CURRENCIES | THREE_DECIMAL_CURRENCIES
    assert currency not in WHOLE_UNITS_ONLY_CURRENCIES


@pytest.mark.parametrize("currency", STORE_CURRENCIES)
@given(amount=order_amounts)
@hyp_settings(max_examples=25, suppress_health_check=_PROVIDER_STATE)
def test_the_mock_charges_exactly_the_minor_units_in_the_orders_currency(currency, amount):
    cents = _decimal_to_cents(_pay(amount))

    intent = StripePaymentProvider().create_payment_intent(
        amount=cents, currency=currency, idempotency_key=f"money-{uuid.uuid4().hex}",
    )

    assert (intent.amount, intent.currency) == (cents, currency)
