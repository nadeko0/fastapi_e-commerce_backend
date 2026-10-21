"""Every currency Stripe lists, not the four the payment journey tests use:
which ones STORE_CURRENCY accepts (app/core/config.py) and that the lists
behind that decision match Stripe's own data (docs.stripe.com/currencies,
read in October 2026)."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.currencies import (
    STRIPE_PRESENTMENT_CURRENCIES,
    THREE_DECIMAL_CURRENCIES,
    WHOLE_UNITS_ONLY_CURRENCIES,
    ZERO_DECIMAL_CURRENCIES,
)

# The zero-decimal list of https://docs.stripe.com/currencies, copied from
# the page in October 2026 (UGX is on it too, as a special case).
STRIPE_ZERO_DECIMAL = frozenset({
    "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA",
    "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
})

_NOT_TWO_DECIMAL = ZERO_DECIMAL_CURRENCIES | THREE_DECIMAL_CURRENCIES | WHOLE_UNITS_ONLY_CURRENCIES


def _settings(**values):
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("currency", sorted(STRIPE_PRESENTMENT_CURRENCIES))
def test_a_store_currency_is_accepted_iff_stripe_charges_it_in_hundredths(currency):
    if currency in _NOT_TWO_DECIMAL:
        with pytest.raises(ValidationError, match="STORE_CURRENCY"):
            _settings(STORE_CURRENCY=currency)
    else:
        assert _settings(STORE_CURRENCY=currency).STORE_CURRENCY == currency


@pytest.mark.parametrize("currency", ["ABC", "XXX", "EUX", "USX", "ZZZ"])
def test_a_three_letter_code_stripe_does_not_list_is_refused_at_startup(currency):
    # Shaped like a currency, but Stripe refuses every payment in it.
    with pytest.raises(ValidationError, match="STORE_CURRENCY") as refused:
        _settings(STORE_CURRENCY=currency)
    assert "Stripe" in str(refused.value)


def test_the_zero_decimal_list_is_the_one_stripe_publishes():
    assert ZERO_DECIMAL_CURRENCIES == STRIPE_ZERO_DECIMAL


def test_every_three_decimal_and_whole_unit_currency_is_one_stripe_lists():
    assert THREE_DECIMAL_CURRENCIES <= STRIPE_PRESENTMENT_CURRENCIES
    assert WHOLE_UNITS_ONLY_CURRENCIES <= STRIPE_PRESENTMENT_CURRENCIES
    assert ZERO_DECIMAL_CURRENCIES <= STRIPE_PRESENTMENT_CURRENCIES


def test_the_presentment_list_is_the_139_codes_of_stripes_page():
    assert len(STRIPE_PRESENTMENT_CURRENCIES) == 139
    assert all(c.isupper() and len(c) == 3 for c in STRIPE_PRESENTMENT_CURRENCIES)
