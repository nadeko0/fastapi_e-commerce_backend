import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.schemas.user import GDPRExport
from app.services import email as email_module
from app.services.email import smtp_provider as smtp_provider_module
from app.services.email.logging_provider import LoggingEmailProvider
from app.services.email.smtp_provider import SmtpEmailProvider


def _no_real_smtp(monkeypatch):
    # None of these tests should touch the network - get_email_provider() is
    # the single choke point every send_*_email function goes through,
    # so short-circuit it with a fake provider and record what it was
    # called with.
    calls = []

    class _FakeProvider:
        def send(self, to_email, subject, html_content):
            calls.append((to_email, subject, html_content))
            return True

    monkeypatch.setattr(email_module, "get_email_provider", lambda: _FakeProvider())
    return calls


def _order_item(product_name="Widget", quantity=2, price_at_time="9.99"):
    return SimpleNamespace(
        product_name=product_name, quantity=quantity, price_at_time=price_at_time
    )


def _order(status="new", with_items=True):
    return SimpleNamespace(
        id=1,
        status=SimpleNamespace(value=status),
        total_amount="19.98",
        created_at=datetime.utcnow(),
        items=[_order_item()] if with_items else [],
    )


def test_send_order_confirmation_email(monkeypatch):
    calls = _no_real_smtp(monkeypatch)

    result = email_module.send_order_confirmation_email("buyer@example.com", _order())

    assert result is True
    assert calls[0][0] == "buyer@example.com"
    assert "Order Confirmation" in calls[0][1]
    assert "Widget" in calls[0][2]


def test_send_order_status_update_email(monkeypatch):
    calls = _no_real_smtp(monkeypatch)

    result = email_module.send_order_status_update_email(
        "buyer@example.com", _order(status="confirmed")
    )

    assert result is True
    assert "Order Status Update" in calls[0][1]
    assert "confirmed" in calls[0][2]


def test_send_order_cancellation_email(monkeypatch):
    calls = _no_real_smtp(monkeypatch)

    result = email_module.send_order_cancellation_email("buyer@example.com", _order())

    assert result is True
    assert "Order Cancellation" in calls[0][1]


def test_send_low_stock_alert_email(monkeypatch):
    calls = _no_real_smtp(monkeypatch)
    products = [SimpleNamespace(name="Widget", stock_quantity=2)]

    result = email_module.send_low_stock_alert_email("admin@example.com", products)

    assert result is True
    assert "Low Stock Alert" in calls[0][1]
    assert "Widget" in calls[0][2]


def test_send_welcome_email(monkeypatch):
    calls = _no_real_smtp(monkeypatch)

    result = email_module.send_welcome_email("new.user@example.com", "New User", "tok123")

    assert result is True
    assert "Welcome" in calls[0][1]
    assert "tok123" in calls[0][2]


def test_send_email_verification(monkeypatch):
    calls = _no_real_smtp(monkeypatch)

    result = email_module.send_email_verification("new.user@example.com", "tok123")

    assert result is True
    assert "Verify" in calls[0][1]


def test_send_password_reset_email(monkeypatch):
    calls = _no_real_smtp(monkeypatch)

    result = email_module.send_password_reset_email("user@example.com", "reset-tok")

    assert result is True
    assert "Password Reset" in calls[0][1]
    assert "reset-tok" in calls[0][2]


def test_send_gdpr_export_email(monkeypatch):
    calls = _no_real_smtp(monkeypatch)
    export_data = GDPRExport(
        request_id="export_1_123",
        request_date=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=24),
        status="completed",
    )

    result = email_module.send_gdpr_export_email("user@example.com", export_data)

    assert result is True
    assert "Data Export" in calls[0][1]
    assert "export_1_123" in calls[0][2]


def test_send_gdpr_deletion_confirmation(monkeypatch):
    calls = _no_real_smtp(monkeypatch)

    result = email_module.send_gdpr_deletion_confirmation("user@example.com", "deletion_1_123")

    assert result is True
    assert "Deletion Confirmation" in calls[0][1]
    assert "deletion_1_123" in calls[0][2]


def test_send_gdpr_request_received_export(monkeypatch):
    calls = _no_real_smtp(monkeypatch)

    result = email_module.send_gdpr_request_received("user@example.com", "export", "req_1")

    assert result is True
    assert "Article 15" in calls[0][2]


def test_send_gdpr_request_received_deletion(monkeypatch):
    calls = _no_real_smtp(monkeypatch)

    result = email_module.send_gdpr_request_received("user@example.com", "deletion", "req_2")

    assert result is True
    assert "Article 17" in calls[0][2]


# -- SmtpEmailProvider --------------------------------------------------


def test_smtp_provider_send_success(monkeypatch):
    sent = {}

    class _FakeServer:
        def __init__(self, *a, **k):
            sent["init_args"] = a
            sent["init_kwargs"] = k

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent["starttls"] = True

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, message):
            sent["message"] = message

    monkeypatch.setattr(smtp_provider_module.smtplib, "SMTP", _FakeServer)

    provider = SmtpEmailProvider()
    result = provider.send("user@example.com", "subject", "<p>body</p>")

    assert result is True
    assert sent["starttls"] is True
    assert sent["message"]["To"] == "user@example.com"
    assert sent["message"]["Subject"] == "subject"
    # Regression: smtplib.SMTP(host, port) with no timeout blocks on the OS
    # default socket timeout, which can hang effectively forever against a
    # stalled/unresponsive server - reproduced live against a real SMTP
    # server during manual verification. Must always pass an explicit
    # bounded timeout.
    assert sent["init_kwargs"].get("timeout") == provider.smtp_timeout
    assert sent["init_kwargs"]["timeout"] is not None


def test_smtp_provider_send_returns_false_and_does_not_raise_on_connection_refused(monkeypatch):
    class _BoomSMTP:
        def __init__(self, *a, **k):
            raise OSError("connection refused")

    monkeypatch.setattr(smtp_provider_module.smtplib, "SMTP", _BoomSMTP)

    provider = SmtpEmailProvider()
    result = provider.send("user@example.com", "subject", "<p>body</p>")

    assert result is False


def test_smtp_provider_send_returns_false_and_does_not_raise_on_auth_failure(monkeypatch):
    import smtplib as real_smtplib

    class _AuthFailsServer:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            raise real_smtplib.SMTPAuthenticationError(535, b"Authentication failed")

        def send_message(self, message):
            raise AssertionError("send_message should not be reached after a failed login")

    monkeypatch.setattr(smtp_provider_module.smtplib, "SMTP", _AuthFailsServer)

    provider = SmtpEmailProvider()
    result = provider.send("user@example.com", "subject", "<p>body</p>")

    assert result is False


def test_smtp_provider_send_failure_is_logged_not_printed(monkeypatch, caplog):
    class _BoomSMTP:
        def __init__(self, *a, **k):
            raise OSError("connection refused")

    monkeypatch.setattr(smtp_provider_module.smtplib, "SMTP", _BoomSMTP)

    provider = SmtpEmailProvider()
    with caplog.at_level(logging.ERROR, logger="app.services.email.smtp_provider"):
        result = provider.send("user@example.com", "subject", "<p>body</p>")

    assert result is False
    assert any("Failed to send email" in record.message for record in caplog.records)


# -- LoggingEmailProvider -------------------------------------------------


def test_logging_provider_is_a_noop_and_returns_true(caplog):
    provider = LoggingEmailProvider()

    with caplog.at_level(logging.INFO, logger="app.services.email.logging_provider"):
        result = provider.send("user@example.com", "subject", "<p>body</p>")

    assert result is True
    assert any("Email suppressed" in record.message for record in caplog.records)


# -- get_email_provider() factory -----------------------------------------


def test_get_email_provider_returns_smtp_provider_when_configured(monkeypatch):
    monkeypatch.setattr(email_module.settings, "EMAIL_PROVIDER", "smtp")

    assert isinstance(email_module.get_email_provider(), SmtpEmailProvider)


def test_get_email_provider_returns_logging_provider_by_default(monkeypatch):
    monkeypatch.setattr(email_module.settings, "EMAIL_PROVIDER", "logging")

    assert isinstance(email_module.get_email_provider(), LoggingEmailProvider)
