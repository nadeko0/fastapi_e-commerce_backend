"""Secrets in Settings outside development and test (F-08).

Settings is built in-process here, with every secret passed explicitly so
the test does not depend on what conftest put in the environment. _env_file
None keeps a developer's own .env out of it.
"""

import os
import re

import pytest
from pydantic import ValidationError

from app.core.config import Settings

_GOOD_SECRETS = {
    "JWT_SECRET_KEY": "k" * 64,
    "POSTGRES_PASSWORD": "p" * 24,
    "REDIS_PASSWORD": "r" * 24,
    "SMTP_PASSWORD": "an-smtp-app-password",
    "STRIPE_WEBHOOK_SECRET": "whsec_" + "w" * 32,
}

_ENV_EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env.example"
)


def _settings(environment="production", **overrides):
    return Settings(_env_file=None, ENVIRONMENT=environment, **{**_GOOD_SECRETS, **overrides})


def _env_example_values():
    values = {}
    with open(_ENV_EXAMPLE, encoding="utf-8") as env_file:
        for line in env_file:
            match = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
            if match:
                values[match.group(1)] = match.group(2)
    return values


def test_real_looking_secrets_are_accepted_in_production():
    settings = _settings()
    assert settings.JWT_SECRET_KEY == "k" * 64


@pytest.mark.parametrize("name", sorted(_GOOD_SECRETS))
def test_the_env_example_placeholder_is_rejected_in_production(name):
    # Whatever .env.example ships for a secret is public: a deployment that
    # copied the file and never replaced the value must not start.
    placeholder = _env_example_values()[name]
    with pytest.raises(ValidationError) as error:
        _settings(**{name: placeholder})
    assert name in str(error.value)


@pytest.mark.parametrize("name", sorted(_GOOD_SECRETS))
@pytest.mark.parametrize("marker", ["change-me", "CHANGEME", "my_placeholder_value"])
def test_any_placeholder_looking_value_is_rejected(name, marker):
    with pytest.raises(ValidationError):
        _settings(**{name: marker + "x" * 64})


def test_the_stripe_webhook_secret_default_is_rejected_in_production():
    # Not passed at all: the built-in default is the published mock value,
    # and anyone who knows it can sign a webhook.
    secrets = {k: v for k, v in _GOOD_SECRETS.items() if k != "STRIPE_WEBHOOK_SECRET"}
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, ENVIRONMENT="production", **secrets)
    assert "STRIPE_WEBHOOK_SECRET" in str(error.value)


@pytest.mark.parametrize(
    "algorithm,minimum",
    [("HS256", 32), ("HS384", 48), ("HS512", 64)],
)
def test_jwt_secret_must_be_at_least_as_long_as_the_hash(algorithm, minimum):
    # RFC 7518 3.2: an HMAC key at least as long as the hash output.
    assert _settings(JWT_ALGORITHM=algorithm, JWT_SECRET_KEY="a" * minimum)
    with pytest.raises(ValidationError) as error:
        _settings(JWT_ALGORITHM=algorithm, JWT_SECRET_KEY="a" * (minimum - 1))
    assert "JWT_SECRET_KEY" in str(error.value)


def test_jwt_secret_length_is_counted_in_bytes():
    # "é" is two bytes in UTF-8, which is what the HMAC is keyed with.
    assert _settings(JWT_SECRET_KEY="é" * 16)
    with pytest.raises(ValidationError):
        _settings(JWT_SECRET_KEY="é" * 15 + "a")


@pytest.mark.parametrize("name", ["POSTGRES_PASSWORD", "REDIS_PASSWORD", "STRIPE_WEBHOOK_SECRET"])
def test_short_secrets_are_rejected_in_production(name):
    with pytest.raises(ValidationError) as error:
        _settings(**{name: "short"})
    assert name in str(error.value)


@pytest.mark.parametrize("value", [None, ""])
def test_redis_password_is_required_in_production(value):
    with pytest.raises(ValidationError) as error:
        _settings(REDIS_PASSWORD=value)
    assert "REDIS_PASSWORD" in str(error.value)


def test_smtp_password_has_no_length_rule():
    # Issued by the mail provider, not chosen here: only placeholders count.
    assert _settings(SMTP_PASSWORD="short")


@pytest.mark.parametrize("environment", ["development", "test"])
def test_placeholders_are_allowed_where_nothing_is_deployed(environment):
    placeholders = {name: _env_example_values()[name] for name in _GOOD_SECRETS}
    placeholders["JWT_SECRET_KEY"] = "short"
    assert _settings(environment, **placeholders)


@pytest.mark.parametrize("environment", ["staging", "Production", "prod"])
def test_every_other_environment_is_checked(environment):
    # Fail closed: only the two names that mean "not deployed" relax it.
    with pytest.raises(ValidationError):
        _settings(environment, JWT_SECRET_KEY="short")


def test_the_error_names_every_problem_and_echoes_no_secret():
    with pytest.raises(ValidationError) as error:
        _settings(JWT_SECRET_KEY="change-me-jwt", POSTGRES_PASSWORD="tiny-pg-pass")
    message = str(error.value)
    assert "JWT_SECRET_KEY" in message and "POSTGRES_PASSWORD" in message
    assert "change-me-jwt" not in message
    assert "tiny-pg-pass" not in message


def test_the_error_does_not_show_the_settings_input():
    # pydantic appends a truncated repr of the whole input to the error,
    # whose tail is whatever setting came last: here the webhook secret.
    with pytest.raises(ValidationError) as error:
        _settings(STRIPE_WEBHOOK_SECRET="placeholder-webhook-secret-value")
    assert "secret-value" not in str(error.value)
