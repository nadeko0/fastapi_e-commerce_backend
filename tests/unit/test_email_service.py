from datetime import datetime, timedelta
from types import SimpleNamespace

from app.schemas.user import GDPRExport
from app.services import email as email_module


def _no_real_smtp(monkeypatch):
    # None of these tests should touch the network - _send_email is the
    # single choke point that calls smtplib.SMTP, so short-circuit it
    # and record what it was called with.
    calls = []
    monkeypatch.setattr(
        email_module.EmailService,
        "_send_email",
        lambda self, to_email, subject, html_content: calls.append(
            (to_email, subject, html_content)
        )
        or True,
    )
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


def test_send_email_returns_false_and_does_not_raise_on_smtp_failure(monkeypatch):
    class _BoomSMTP:
        def __init__(self, *a, **k):
            raise OSError("connection refused")

    monkeypatch.setattr(email_module.smtplib, "SMTP", _BoomSMTP)

    service = email_module.EmailService()
    result = service._send_email("user@example.com", "subject", "<p>body</p>")

    assert result is False
