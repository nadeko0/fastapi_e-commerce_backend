"""Links in emails point at PUBLIC_BASE_URL, which must be https anywhere
but on a developer's machine. They used to be http://localhost:{PORT}:
broken in production, and one search-and-replace away from sending
verification and reset tokens over plain http."""

import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.services import email as email_module

SHOP = "https://shop.example.com"


def _sent(monkeypatch):
    calls = []

    class _FakeProvider:
        def send(self, to_email, subject, html_content):
            calls.append(html_content)
            return True

    monkeypatch.setattr(email_module, "get_email_provider", lambda: _FakeProvider())
    return calls


@pytest.mark.parametrize(
    "send,path",
    [
        (lambda: email_module.send_welcome_email("a@example.com", "A", "tok"), "/users/verify-email/tok"),
        (lambda: email_module.send_email_verification("a@example.com", "tok"), "/users/verify-email/tok"),
        (lambda: email_module.send_password_reset_email("a@example.com", "tok"), "/users/password/reset/tok"),
    ],
    ids=["welcome", "verification", "reset"],
)
def test_email_links_use_the_public_base_url(monkeypatch, send, path):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", SHOP, raising=False)
    sent = _sent(monkeypatch)

    send()

    assert f'href="{SHOP}{settings.API_V1_STR}{path}"' in sent[0]
    assert "localhost" not in sent[0]


@pytest.mark.parametrize("environment", ["production", "staging"])
@pytest.mark.parametrize("url", ["http://shop.example.com", "shop.example.com", "ftp://shop.example.com"])
def test_outside_development_the_public_base_url_must_be_https(environment, url):
    with pytest.raises(ValidationError, match="PUBLIC_BASE_URL"):
        type(settings)(ENVIRONMENT=environment, PUBLIC_BASE_URL=url)


def test_outside_development_there_is_no_default_to_fall_back_on():
    # The development default (http://localhost) must not quietly become
    # the production link.
    with pytest.raises(ValidationError, match="PUBLIC_BASE_URL"):
        type(settings)(ENVIRONMENT="production")


@pytest.mark.parametrize("environment", ["development", "test"])
def test_https_is_accepted_and_a_trailing_slash_dropped(environment):
    # A deployment accepting it is covered with the other deployment
    # settings (tests/unit/test_config_secrets.py).
    configured = type(settings)(ENVIRONMENT=environment, PUBLIC_BASE_URL=f"{SHOP}/")
    assert configured.PUBLIC_BASE_URL == SHOP


def test_where_nothing_is_deployed_links_default_to_this_machine():
    assert type(settings)(ENVIRONMENT="test").PUBLIC_BASE_URL.startswith("http://localhost:")


def test_development_may_use_plain_http():
    configured = type(settings)(ENVIRONMENT="development", PUBLIC_BASE_URL="http://localhost:8000")
    assert configured.PUBLIC_BASE_URL == "http://localhost:8000"
