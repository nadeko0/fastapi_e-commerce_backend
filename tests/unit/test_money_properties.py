"""
Money at the payment boundary, property-based: every amount the request
schemas accept converts to integer minor units and back without loss, in
every two-decimal currency a store may be configured with, and the mock
provider charges exactly that many minor units in that currency.

Refund arithmetic (partials summing to the capture, never above it) is
property-tested against both providers in test_payment_provider_contract.py.
"""
import re
import uuid
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from pydantic import ValidationError

from app.api.v1.orders import _cents_to_decimal, _decimal_to_cents
from app.core.config import (
    THREE_DECIMAL_CURRENCIES,
    WHOLE_UNITS_ONLY_CURRENCIES,
    ZERO_DECIMAL_CURRENCIES,
)
from app.schemas.order import PaymentCreate, RefundCreate
from app.services.payment.exceptions import InvalidRequestError
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


# -- conversion ------------------------------------------------------------------

@given(amount=order_amounts)
def test_an_accepted_amount_converts_to_minor_units_and_back_exactly(amount):
    accepted = _pay(amount)
    cents = _decimal_to_cents(accepted)

    assert type(cents) is int
    assert cents == int(amount * 100)
    assert _cents_to_decimal(cents) == amount == accepted
    assert _cents_to_decimal(cents).as_tuple().exponent == -2


@given(cents=st.integers(min_value=0, max_value=int(LARGEST_ORDER_TOTAL * 100)))
def test_every_minor_unit_count_is_an_amount_the_schemas_accept(cents):
    amount = _cents_to_decimal(cents)
    assert _decimal_to_cents(_pay(amount)) == cents
    if cents:
        assert _decimal_to_cents(RefundCreate(amount=amount).amount) == cents


@pytest.mark.parametrize("amount,cents", [
    ("0", 0), ("0.00", 0), ("0.01", 1), ("0.10", 10), ("0.99", 99), ("1", 100),
    ("19.99", 1999), ("99999999.99", 9999999999),
])
def test_the_boundaries_convert_exactly(amount, cents):
    assert _decimal_to_cents(_pay(amount)) == cents
    assert _cents_to_decimal(cents) == Decimal(amount)


@given(amount=st.floats(min_value=0.01, max_value=99999999.99, allow_nan=False))
def test_an_amount_is_a_decimal_string_never_a_json_number(amount):
    # A JSON number arrives as a float (0.1 + 0.2), so no number is accepted
    # whatever it is. The same value written as a string is that many cents,
    # never the float's binary neighbour, if its shortest form has at most
    # two decimals; any other is refused (422), not rounded.
    with pytest.raises(ValidationError):
        _pay(amount)
    text = repr(amount)
    try:
        accepted = _pay(text)
    except ValidationError:
        assert Decimal(text).normalize().as_tuple().exponent < -2
    else:
        assert accepted == Decimal(text)


def test_a_zero_refund_is_refused_and_a_cent_is_the_smallest():
    with pytest.raises(ValidationError):
        RefundCreate(amount="0.00")
    assert RefundCreate(amount="0.01").amount == Decimal("0.01")


@pytest.mark.parametrize("currency", STORE_CURRENCIES)
def test_a_zero_order_total_is_never_sent_as_a_charge(currency):
    # PaymentCreate accepts 0.00 (a free order has that total); the
    # provider is what refuses to charge nothing.
    with pytest.raises(InvalidRequestError):
        StripePaymentProvider().create_payment_intent(
            amount=_decimal_to_cents(_pay("0.00")), currency=currency, idempotency_key="zero",
        )


# -- currencies ------------------------------------------------------------------

@given(code=st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=0x24F),
                    max_size=5))
def test_a_payment_currency_must_be_three_capital_letters(code):
    if re.fullmatch(r"[A-Z]{3}", code):
        assert PaymentCreate(order_id=1, payment_method="stripe", amount="1",
                             currency=code).currency == code
    else:
        with pytest.raises(ValidationError):
            PaymentCreate(order_id=1, payment_method="stripe", amount="1", currency=code)


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


@pytest.mark.parametrize("currency", STORE_CURRENCIES)
def test_the_mock_reports_currencies_in_upper_case_however_they_were_sent(currency):
    # As the live provider does with Stripe's lower-case answers.
    provider = StripePaymentProvider()
    intent = provider.create_payment_intent(
        amount=5000, currency=currency.lower(), idempotency_key=f"lower-{currency}",
        payment_method="pm_card_visa",
    )
    provider.confirm_payment_intent(intent.id, return_url="http://localhost:3000/r")
    session = provider.create_checkout_session(
        amount=5000, currency=currency.lower(), idempotency_key=f"lower-cs-{currency}",
        success_url="http://localhost:3000/s", cancel_url="http://localhost:3000/c",
    )

    assert intent.currency == currency
    assert session.currency == currency
    assert provider.create_refund(payment_intent_id=intent.id).currency == currency
    # The same request again, in the other case, is the same request.
    assert provider.create_payment_intent(
        amount=5000, currency=currency, idempotency_key=f"lower-{currency}",
    ).id == intent.id
