"""
Which payment provider the app uses (PAYMENT_PROVIDER) and the startup
check that keeps the mock, or the repository's placeholder Stripe secrets,
out of production.

get_payment_provider() used to return the mock unconditionally: deployed
as is, every order would be "paid" without any money being taken (the mock
confirms with a successful test card when no token is sent), and webhooks
would be verified with a secret anyone can read in .env.example.
"""
import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.services.payment import get_payment_provider
from app.services.payment.live_stripe_provider import LiveStripePaymentProvider
from app.services.payment.stripe_provider import StripePaymentProvider

# Not the repository's placeholders. Deliberately not in Stripe's key format:
# GitHub push protection refuses anything that looks like a live key.
REAL_LOOKING_SECRET_KEY = "stripe-secret-key-for-tests-not-a-real-key"
REAL_LOOKING_WEBHOOK_SECRET = "whsec_9f8e7d6c5b4a39281706f5e4d3c2b1a0"


def _settings(**overrides):
    # _env_file=None: only the test environment (tests/conftest.py) and the
    # overrides here, never a developer's own .env.
    return Settings(_env_file=None, **overrides)


def _production(**overrides):
    values = {
        "ENVIRONMENT": "production",
        "PAYMENT_PROVIDER": "stripe",
        "STRIPE_SECRET_KEY": REAL_LOOKING_SECRET_KEY,
        "STRIPE_WEBHOOK_SECRET": REAL_LOOKING_WEBHOOK_SECRET,
        # The other secrets a deployment needs (Settings.check_deployment_settings),
        # so that only the value under test can be what is refused.
        "JWT_SECRET_KEY": "k" * 64,
        "POSTGRES_PASSWORD": "p" * 24,
        "REDIS_PASSWORD": "r" * 24,
        "SMTP_PASSWORD": "an-smtp-app-password",
    }
    values.update(overrides)
    return _settings(**values)


def test_the_test_suite_runs_on_the_mock():
    assert settings.PAYMENT_PROVIDER == "mock"
    assert isinstance(get_payment_provider(), StripePaymentProvider)


def test_stripe_setting_selects_the_live_provider_with_the_configured_key(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(settings, "PAYMENT_PROVIDER", "stripe")
        m.setattr(settings, "STRIPE_SECRET_KEY", REAL_LOOKING_SECRET_KEY)
        provider = get_payment_provider()

    assert isinstance(provider, LiveStripePaymentProvider)
    assert provider._api_key == REAL_LOOKING_SECRET_KEY


def test_unknown_provider_name_is_refused():
    with pytest.raises(ValidationError, match="PAYMENT_PROVIDER"):
        _settings(PAYMENT_PROVIDER="paypal")


def test_production_refuses_the_mock_provider():
    with pytest.raises(ValidationError, match="PAYMENT_PROVIDER"):
        _production(PAYMENT_PROVIDER="mock")


@pytest.mark.parametrize(
    "field,placeholder",
    [
        ("STRIPE_SECRET_KEY", "sk_test_mock_placeholder"),
        ("STRIPE_SECRET_KEY", "sk_test_mock_anything_else"),
        ("STRIPE_SECRET_KEY", ""),
        ("STRIPE_WEBHOOK_SECRET", "whsec_mock_placeholder"),
        ("STRIPE_WEBHOOK_SECRET", ""),
    ],
)
def test_production_refuses_placeholder_stripe_secrets(field, placeholder):
    with pytest.raises(ValidationError, match=field):
        _production(**{field: placeholder})


def test_production_with_stripe_and_real_secrets_starts():
    configured = _production()
    assert configured.PAYMENT_PROVIDER == "stripe"


def test_development_may_use_the_mock_with_placeholders():
    configured = _settings(ENVIRONMENT="development", PAYMENT_PROVIDER="mock")
    assert configured.PAYMENT_PROVIDER == "mock"
    assert configured.STRIPE_WEBHOOK_SECRET == "whsec_mock_placeholder"
