"""Currency facts the store currency and the mock payment provider share.

Nothing here is read from memory: each list names where it was copied from.
"""
from decimal import Decimal

# Stripe's minimum charge per currency, https://docs.stripe.com/currencies
# ("Minimum charge amount by currency", read through Stripe's documentation
# search in November 2026). It holds for a charge in the currency the account
# settles in; a charge converted to another settlement currency must meet that
# currency's minimum after conversion, which depends on the rate and is not
# checked here. JPY and KRW are left out: STORE_CURRENCY refuses them.
MINIMUM_CHARGE = {
    currency: Decimal(amount) for currency, amount in (
        ("USD", "0.50"), ("AED", "2.00"), ("ARS", "0.50"), ("AUD", "0.50"),
        ("BRL", "0.50"), ("CAD", "0.50"), ("CHF", "0.50"), ("COP", "0.50"),
        ("CZK", "15.00"), ("DKK", "2.50"), ("EUR", "0.50"), ("GBP", "0.30"),
        ("HKD", "4.00"), ("HUF", "175.00"), ("IDR", "0.50"), ("ILS", "0.50"),
        ("INR", "0.50"), ("MXN", "10.00"), ("MYR", "2.00"), ("NOK", "3.00"),
        ("NZD", "0.50"), ("PHP", "0.50"), ("PLN", "2.00"), ("RON", "2.00"),
        ("RUB", "0.50"), ("SEK", "3.00"), ("SGD", "0.50"), ("THB", "10.00"),
        ("ZAR", "0.50"),
    )
}

# Currencies whose smallest unit is not a hundredth, which STORE_CURRENCY
# refuses. Copied from Stripe's own lists, not from memory:
# - zero-decimal: https://docs.stripe.com/currencies#zero-decimal
#   ("Zero-decimal currencies"; UGX is also among its special cases, sent
#   as two decimals that must be 00, so no fraction can be charged either);
# - three-decimal: https://support.stripe.com/questions/which-payments-methods-and-products-are-available-in-the-uae
#   ("We also support charges in the following three-decimal currencies").
#   docs.stripe.com/currencies mentions three-decimal support but, as
#   fetched in October 2026, lists no three-decimal currencies itself.
ZERO_DECIMAL_CURRENCIES = frozenset({
    "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA",
    "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
})
THREE_DECIMAL_CURRENCIES = frozenset({"BHD", "JOD", "KWD", "OMR", "TND"})
# Stripe's "Special cases" (https://docs.stripe.com/currencies#special-cases,
# read from the page's HTML in October 2026; it lists ISK, HUF, TWD, UGX):
# - ISK is sent with two decimals that must be 00 ("You can't charge
#   fractions of ISK"). Prices here have two decimals, so an order could be
#   placed for an amount Stripe then refuses to charge: refused.
# - UGX: the same rule; already refused as zero-decimal above.
# - HUF and TWD: only payouts are restricted (manual payouts in multiples
#   of 100); "you can charge two-decimal amounts". This application makes
#   no payouts, so both stay accepted.
WHOLE_UNITS_ONLY_CURRENCIES = frozenset({"ISK"})

# Every presentment currency https://docs.stripe.com/currencies lists for
# any of the 45 account countries it names (the page's embedded
# SupportedPresentmentCurrencies data, fetched in October 2026). The union
# is used because the store's Stripe account country is not known here. The
# five three-decimal currencies appear only for some countries.
STRIPE_PRESENTMENT_CURRENCIES = frozenset({
    "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD",
    "AWG", "AZN", "BAM", "BBD", "BDT", "BHD", "BIF", "BMD",
    "BND", "BOB", "BRL", "BSD", "BWP", "BYN", "BZD", "CAD",
    "CDF", "CHF", "CLP", "CNY", "COP", "CRC", "CVE", "CZK",
    "DJF", "DKK", "DOP", "DZD", "EGP", "ETB", "EUR", "FJD",
    "FKP", "GBP", "GEL", "GIP", "GMD", "GNF", "GTQ", "GYD",
    "HKD", "HNL", "HTG", "HUF", "IDR", "ILS", "INR", "ISK",
    "JMD", "JOD", "JPY", "KES", "KGS", "KHR", "KMF", "KRW",
    "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD", "LSL",
    "MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MUR",
    "MVR", "MWK", "MXN", "MYR", "MZN", "NAD", "NGN", "NIO",
    "NOK", "NPR", "NZD", "OMR", "PAB", "PEN", "PGK", "PHP",
    "PKR", "PLN", "PYG", "QAR", "RON", "RSD", "RUB", "RWF",
    "SAR", "SBD", "SCR", "SEK", "SGD", "SHP", "SLE", "SOS",
    "SRD", "STD", "SZL", "THB", "TJS", "TND", "TOP", "TRY",
    "TTD", "TWD", "TZS", "UAH", "UGX", "USD", "UYU", "UZS",
    "VND", "VUV", "WST", "XAF", "XCD", "XCG", "XOF", "XPF",
    "YER", "ZAR", "ZMW",
})
