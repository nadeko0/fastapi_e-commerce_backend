"""What Settings accepts, refuses and derives (app/core/config.py), built
in-process with explicit values. Secrets have their own file
(test_config_secrets.py); parsing of list settings from raw environment
strings is in test_config.py, which needs a subprocess for it."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.rate_limit import parse_rate

_RATE_SETTINGS = [
    "RATE_LIMIT_DEFAULT", "RATE_LIMIT_SEARCH", "RATE_LIMIT_AUTH",
    "RATE_LIMIT_CHECKOUT", "RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS",
    "RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL", "RATE_LIMIT_GUEST_CART",
]


def _settings(**values):
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("name", _RATE_SETTINGS)
@pytest.mark.parametrize(
    "value,count,seconds",
    [
        ("300/minute", 300, 60), ("5/second", 5, 1), ("10/15 minutes", 10, 900),
        ("1/day", 1, 86400), ("2/hours", 2, 3600), ("7/3hour", 7, 10800),
    ],
)
def test_every_limit_accepts_the_documented_forms(name, value, count, seconds):
    configured = getattr(_settings(**{name: value}), name)
    assert parse_rate(configured) == (count, seconds)


@pytest.mark.parametrize("name", _RATE_SETTINGS)
@pytest.mark.parametrize(
    "value", ["0/minute", "-1/minute", "10/fortnight", "10 per minute", "10/0 minutes", "", "/minute", "10/"]
)
def test_every_limit_refuses_anything_else_at_startup(name, value):
    with pytest.raises(ValidationError):
        _settings(**{name: value})


# Not limits: the switch, and the old login setting that is only declared so
# that setting it fails startup (see its validator in app/core/config.py).
_NOT_LIMITS = {"RATE_LIMIT_ENABLED", "RATE_LIMIT_LOGIN_FAILURES"}


def test_every_limit_setting_is_covered_here():
    assert sorted(n for n in Settings.model_fields if n.startswith("RATE_LIMIT_") and n not in _NOT_LIMITS) == sorted(_RATE_SETTINGS)


@pytest.mark.parametrize(
    "name,value", [
        ("REDIS_GUEST_CART_TTL_DAYS", 0), ("CART_MAX_LINES", 0), ("BCRYPT_MAX_CONCURRENCY", 0),
        ("PASSWORD_RESET_TOKEN_EXPIRE_MINUTES", 0),
    ]
)
def test_lower_bounds(name, value):
    with pytest.raises(ValidationError):
        _settings(**{name: value})
    assert getattr(_settings(**{name: value + 1}), name) == value + 1


# Stripe's zero-decimal currencies, and the three-decimal ones it charges
# in, as Stripe lists them: https://docs.stripe.com/currencies (zero-decimal)
# and https://support.stripe.com/questions/which-payments-methods-and-products-are-available-in-the-uae
# (three-decimal).
@pytest.mark.parametrize(
    "currency",
    [
        "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND",
        "VUV", "XAF", "XOF", "XPF",
        "BHD", "JOD", "KWD", "OMR", "TND",
    ],
)
def test_a_store_currency_without_two_decimals_is_refused_at_startup(currency):
    # Amounts are sent to the provider multiplied by 100 and prices are
    # stored with two decimals: in JPY a 1000 yen order would be charged
    # 100,000 yen, in KWD a tenth of its price.
    with pytest.raises(ValidationError, match="STORE_CURRENCY") as refused:
        _settings(STORE_CURRENCY=currency)
    assert "two decimal" in str(refused.value)


def test_a_store_currency_stripe_charges_in_whole_units_only_is_refused():
    # Stripe's special case (https://docs.stripe.com/currencies, "Special
    # cases"): ISK is sent with two decimals that must be 00, "You can't
    # charge fractions of ISK". Prices here have two decimals, so an order
    # of 10.50 ISK would be placed, its stock taken, and its payment then
    # refused by Stripe.
    with pytest.raises(ValidationError, match="STORE_CURRENCY") as refused:
        _settings(STORE_CURRENCY="ISK")
    assert "whole units" in str(refused.value)


# HUF and TWD are special cases only for payouts (manual payouts in
# multiples of 100), which this application does not make; their charges
# take two decimals.
@pytest.mark.parametrize("currency", ["USD", "EUR", "GBP", "HUF", "TWD"])
def test_two_decimal_store_currencies_are_accepted(currency):
    assert _settings(STORE_CURRENCY=currency).STORE_CURRENCY == currency


def test_a_password_reset_link_lives_an_hour_by_default():
    assert _settings().PASSWORD_RESET_TOKEN_EXPIRE_MINUTES == 60


def test_development_forces_one_worker_debug_and_reload():
    development = _settings(ENVIRONMENT="development", WORKERS=8, DEBUG=False, RELOAD=False)
    assert (development.WORKERS, development.DEBUG, development.RELOAD) == (1, True, True)


def test_elsewhere_workers_are_kept_and_debug_and_reload_are_off():
    staging = _settings(
        ENVIRONMENT="test", WORKERS=8, DEBUG=True, RELOAD=True,
    )
    assert (staging.WORKERS, staging.DEBUG, staging.RELOAD) == (8, False, False)
    assert staging.SQL_DEBUG is False


@pytest.mark.parametrize("environment,size,overflow", [("development", 5, 5), ("test", 10, 5)])
def test_pool_sizes_are_halved_in_development_with_a_floor_of_five(environment, size, overflow):
    configured = _settings(ENVIRONMENT=environment, DB_POOL_SIZE="10", DB_MAX_OVERFLOW=5)
    assert (configured.DB_POOL_SIZE, configured.DB_MAX_OVERFLOW) == (size, overflow)


def test_database_uri_is_assembled_unless_given():
    assembled = _settings(POSTGRES_USER="u", POSTGRES_PASSWORD="p", POSTGRES_SERVER="h", POSTGRES_DB="d")
    assert assembled.DATABASE_URI == "postgresql+psycopg://u:p@h/d"
    given = _settings(DATABASE_URI="postgresql+psycopg://other@elsewhere/db")
    assert given.DATABASE_URI == "postgresql+psycopg://other@elsewhere/db"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("", []),
        ("  ", []),
        ("10.0.0.1, 10.0.0.2,,", ["10.0.0.1", "10.0.0.2"]),
        ('["10.0.0.1", "fd00::/8"]', ["10.0.0.1", "fd00::/8"]),
        (["10.0.0.1"], ["10.0.0.1"]),
    ],
)
def test_trusted_proxies_forms(value, expected):
    assert _settings(TRUSTED_PROXIES=value).TRUSTED_PROXIES == expected


def test_cors_origins_must_be_urls():
    assert _settings(BACKEND_CORS_ORIGINS="http://a.example,https://b.example").BACKEND_CORS_ORIGINS == [
        "http://a.example", "https://b.example",
    ]
    with pytest.raises(ValidationError):
        _settings(BACKEND_CORS_ORIGINS="not a url")
    with pytest.raises(ValidationError):
        _settings(BACKEND_CORS_ORIGINS="ftp://a.example")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://a.example/", ["https://a.example"]),
        ("HTTPS://A.Example", ["https://a.example"]),
        ("https://a.example:443", ["https://a.example"]),
        ("http://localhost:3000/", ["http://localhost:3000"]),
        ('["http://localhost:3000/", "https://b.example:8443"]',
         ["http://localhost:3000", "https://b.example:8443"]),
    ],
)
def test_cors_origins_are_normalized_to_the_origin_form(value, expected):
    assert _settings(BACKEND_CORS_ORIGINS=value).BACKEND_CORS_ORIGINS == expected


@pytest.mark.parametrize(
    "value",
    ["https://a.example/app", "https://a.example?x=1", "https://a.example/#f"],
)
def test_cors_origin_with_a_path_query_or_fragment_is_refused(value):
    with pytest.raises(ValidationError, match="origin"):
        _settings(BACKEND_CORS_ORIGINS=value)


def test_version_comes_from_the_package_not_the_environment():
    from app import __version__

    assert _settings().VERSION == __version__
    assert "VERSION" not in Settings.model_fields
