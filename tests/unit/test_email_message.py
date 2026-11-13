"""The message SmtpEmailProvider hands to the SMTP server: Date and
Message-ID (RFC 5322 3.6) and a text/plain alternative next to the HTML.
The mails are built by the real templates (app/services/email/messages.py)
and the real provider; only the SMTP connection is replaced."""

import json
from datetime import UTC, timedelta
from email.utils import parsedate_to_datetime
from types import SimpleNamespace

import pytest

from app.core.clock import utc_now
from app.core.config import settings
from app.schemas.user import GDPRExport
from app.services import email as email_module
from app.services.email import smtp_provider as smtp_provider_module
from app.services.email.smtp_provider import SmtpEmailProvider


@pytest.fixture
def sent(monkeypatch):
    """Every message the provider would have sent."""
    messages = []

    class _Server:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def starttls(self, *, context=None):
            pass

        def login(self, user, password):
            pass

        def send_message(self, message):
            messages.append(message)

    monkeypatch.setattr(smtp_provider_module.smtplib, "SMTP", _Server)
    monkeypatch.setattr(email_module, "get_email_provider", SmtpEmailProvider)
    monkeypatch.setattr(settings, "EMAILS_FROM_EMAIL", "shop@mail.example.com")
    return messages


def _parts(message):
    return {part.get_content_type(): part.get_payload(decode=True).decode(part.get_content_charset())
            for part in message.get_payload()}


def test_a_mail_has_a_date_and_a_message_id_in_the_senders_domain(sent):
    before = utc_now().replace(microsecond=0)
    assert email_module.send_welcome_email("new.user@example.com", "New User", "tok123")

    (message,) = sent
    # "-0000": the time is UTC and says nothing of the host's zone (RFC 5322
    # 3.3); Python reads it as a naive datetime.
    assert message["Date"].endswith(" -0000")
    assert before <= parsedate_to_datetime(message["Date"]).replace(tzinfo=UTC) <= utc_now()
    assert message["Message-ID"].startswith("<")
    assert message["Message-ID"].endswith("@mail.example.com>")


def test_message_ids_are_unique(sent):
    email_module.send_welcome_email("a@example.com", "A", "tok1")
    email_module.send_welcome_email("b@example.com", "B", "tok2")

    assert sent[0]["Message-ID"] != sent[1]["Message-ID"]


def test_the_plain_text_alternative_comes_first_and_says_what_the_html_says(sent):
    email_module.send_welcome_email("new.user@example.com", "<b>New</b> & User", "tok123")

    (message,) = sent
    assert message.get_content_type() == "multipart/alternative"
    assert [part.get_content_type() for part in message.get_payload()] == ["text/plain", "text/html"]
    text = _parts(message)["text/plain"]
    assert "<" not in text.replace("<b>New</b>", "")  # no markup but the user's own, unescaped
    assert "Dear <b>New</b> & User," in text
    assert f"Verify Email Address ({settings.FRONTEND_URL}" in text  # the link keeps its address
    assert "tok123" in text
    assert "- Verify your email address\n- Browse our product catalog" in text
    assert "\n\n\n" not in text


def test_table_rows_become_lines(sent):
    order = SimpleNamespace(
        id=1,
        status=SimpleNamespace(value="new"),
        total_amount="19.98",
        currency="USD",
        created_at=utc_now(),
        items=[SimpleNamespace(product_name="Widget", quantity=2, price_at_time="9.99")],
    )
    email_module.send_order_confirmation_email("buyer@example.com", order)

    text = _parts(sent[0])["text/plain"]
    assert "Product | Quantity | Price\n" in text
    assert "Widget | 2 | 9.99 USD\n" in text


def test_head_style_and_script_are_left_out_of_the_text(sent):
    html = (
        "<html><head><title>T</title><style>p { color: red }</style></head>"
        "<body><script>track()</script><p>Hello</p></body></html>"
    )
    assert SmtpEmailProvider().send("a@example.com", "s", html)

    assert _parts(sent[0])["text/plain"] == "Hello\n"


def test_preformatted_json_keeps_its_layout(sent):
    data = {"user": {"full_name": 'Ann "Q" <Lee>'}, "orders": [1, 2]}
    export = GDPRExport(
        request_id="export_1_123",
        request_date=utc_now(),
        expires_at=utc_now() + timedelta(hours=24),
        status="completed",
        data=data,
    )
    email_module.send_gdpr_export_email("user@example.com", export)

    text = _parts(sent[0])["text/plain"]
    assert json.dumps(data, indent=2) in text  # indentation kept, entities decoded
