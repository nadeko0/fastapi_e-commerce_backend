"""The payment layer in every currency the configuration accepts: the mock
provider takes it and the cents conversion round-trips at the amount
boundaries. The journey tests use four currencies; this is what lets them
stand for the rest."""

from decimal import Decimal

import pytest

from app.api.v1.orders import _cents_to_decimal, _decimal_to_cents
from app.core.currencies import (
    STRIPE_PRESENTMENT_CURRENCIES,
    THREE_DECIMAL_CURRENCIES,
    WHOLE_UNITS_ONLY_CURRENCIES,
    ZERO_DECIMAL_CURRENCIES,
)
from app.schemas.order import ORDER_AMOUNT_DECIMAL_PLACES, ORDER_AMOUNT_MAX_DIGITS
from app.services.payment.exceptions import InvalidRequestError
from app.services.payment.stripe_provider import (
    TOKEN_SUCCESS_DEFAULT,
    StripePaymentProvider,
)

# The currencies Settings accepts as STORE_CURRENCY.
ACCEPTED = sorted(
    STRIPE_PRESENTMENT_CURRENCIES
    - ZERO_DECIMAL_CURRENCIES
    - THREE_DECIMAL_CURRENCIES
    - WHOLE_UNITS_ONLY_CURRENCIES
)

SCHEMA_MAXIMUM = Decimal(10 ** (ORDER_AMOUNT_MAX_DIGITS - ORDER_AMOUNT_DECIMAL_PLACES)) - Decimal("0.01")


@pytest.fixture(autouse=True)
def _fresh_mock():
    StripePaymentProvider.reset_mock_state()
    yield
    StripePaymentProvider.reset_mock_state()


@pytest.mark.parametrize("currency", ACCEPTED)
def test_the_mock_charges_and_refunds_every_accepted_store_currency(currency):
    provider = StripePaymentProvider()
    intent = provider.create_payment_intent(
        amount=5000, currency=currency, idempotency_key=f"pi-{currency}",
        payment_method=TOKEN_SUCCESS_DEFAULT,
    )
    provider.confirm_payment_intent(intent.id, return_url="http://localhost:3000/r")

    refund = provider.create_refund(
        payment_intent_id=intent.id, amount=1000, idempotency_key=f"re-{currency}"
    )

    assert refund.amount == 1000


@pytest.mark.parametrize("currency", ["ABC", "XXX", "EUX", "usx1", "dollars", "", None, 840])
def test_the_mock_still_refuses_a_code_stripe_does_not_list(currency):
    with pytest.raises(InvalidRequestError):
        StripePaymentProvider().create_payment_intent(
            amount=5000, currency=currency, idempotency_key="junk"
        )


def test_the_mock_takes_a_lowercase_listed_code_as_stripe_does():
    StripePaymentProvider().create_payment_intent(
        amount=5000, currency="kes", idempotency_key="lower"
    )


def test_the_schema_maximum_is_what_the_boundary_test_assumes():
    assert SCHEMA_MAXIMUM == Decimal("99999999.99")


@pytest.mark.parametrize("currency", ACCEPTED)
@pytest.mark.parametrize(
    "amount", [Decimal("0.01"), Decimal("1.00"), Decimal("999999.99"), SCHEMA_MAXIMUM]
)
def test_cents_round_trip_in_every_accepted_currency(currency, amount):
    cents = _decimal_to_cents(amount)

    assert cents == int(amount * 100)
    assert _cents_to_decimal(cents) == amount
    # And the provider takes that amount in that currency.
    StripePaymentProvider().create_payment_intent(
        amount=cents, currency=currency, idempotency_key=f"rt-{currency}-{cents}"
    )
