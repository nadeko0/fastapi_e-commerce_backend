"""Links in the welcome, verification and password-reset emails open the
frontend (settings.FRONTEND_URL), which then calls the POST endpoints. The
API no longer answers a GET on those tokens, so a link to the API would
only ever be a 404 in the customer's mail client."""

import re

import pytest

from app.core.config import settings
from app.services import email as email_module
from app.services.email import messages

SHOP = "https://shop.example.com"


def _sent(monkeypatch):
    calls = []

    class _FakeProvider:
        def send(self, to_email, subject, html_content):
            calls.append(html_content)
            return True

    monkeypatch.setattr(email_module, "get_email_provider", lambda: _FakeProvider())
    return calls


def _href(html_content):
    return re.search(r'href="([^"]+)"', html_content).group(1)


SENDERS = pytest.mark.parametrize(
    "send,path",
    [
        (lambda tok: email_module.send_welcome_email("a@example.com", "A", tok), "/verify-email"),
        (lambda tok: email_module.send_email_verification("a@example.com", tok), "/verify-email"),
        (lambda tok: email_module.send_password_reset_email("a@example.com", tok), "/reset-password"),
    ],
    ids=["welcome", "verification", "reset"],
)


@SENDERS
def test_email_links_open_the_frontend_with_the_token_in_the_query(monkeypatch, send, path):
    monkeypatch.setattr(settings, "FRONTEND_URL", SHOP)
    sent = _sent(monkeypatch)

    send("tok")

    assert _href(sent[0]) == f"{SHOP}{path}?token=tok"


@SENDERS
def test_email_links_never_point_at_the_api(monkeypatch, send, path):
    monkeypatch.setattr(settings, "FRONTEND_URL", SHOP)
    sent = _sent(monkeypatch)

    send("tok")

    assert settings.API_V1_STR not in _href(sent[0])
    assert "/users/" not in _href(sent[0])


@SENDERS
def test_the_token_is_url_encoded(monkeypatch, send, path):
    monkeypatch.setattr(settings, "FRONTEND_URL", SHOP)
    sent = _sent(monkeypatch)

    send("a b&c=d/e#f?g")

    link = _href(sent[0])
    assert link == f"{SHOP}{path}?token=a%20b%26c%3Dd%2Fe%23f%3Fg"
    assert "#" not in link


def test_the_page_paths_are_the_documented_contract():
    assert messages.VERIFY_EMAIL_PAGE_PATH == "/verify-email"
    assert messages.RESET_PASSWORD_PAGE_PATH == "/reset-password"
