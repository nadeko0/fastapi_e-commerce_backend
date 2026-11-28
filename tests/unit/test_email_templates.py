from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import UndefinedError

from app.core.clock import utc_now
from app.services import email as email_module
from app.services.email.templates import render

XSS_PAYLOAD = "<script>alert(1)</script>"
ESCAPED_XSS_PAYLOAD = "&lt;script&gt;alert(1)&lt;/script&gt;"
TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "app" / "templates" / "email"

TEMPLATE_CONTEXTS = {
    "order_confirmation.html": {
        "order_id": 1,
        "items": [SimpleNamespace(product_name=XSS_PAYLOAD, quantity=1, price_at_time="10.00")],
        "total_amount": "10.00",
        "currency": "USD",
        "status": "new",
        "order_date": "2026-01-01 00:00:00 UTC",
    },
    "order_status_update.html": {
        "order_id": 1,
        "status": XSS_PAYLOAD,
        "order_date": "2026-01-01 00:00:00 UTC",
        "total_amount": "10.00",
        "currency": "USD",
    },
    "order_cancellation.html": {
        "order_id": XSS_PAYLOAD,
        "order_date": "2026-01-01 00:00:00 UTC",
        "total_amount": "10.00",
        "currency": "USD",
    },
    "low_stock_alert.html": {
        "products": [SimpleNamespace(name=XSS_PAYLOAD, stock_quantity=2)],
        "generated_at": "2026-01-01 00:00:00 UTC",
    },
    "welcome.html": {
        "project_name": "Shop",
        "full_name": XSS_PAYLOAD,
        "verification_link": "https://shop.example.com/verify-email?token=tok",
        "expire_hours": 24,
    },
    "account_exists.html": {
        "project_name": XSS_PAYLOAD,
    },
    "email_verification.html": {
        "verification_link": "https://shop.example.com/verify-email?token=tok",
        "expire_hours": 24,
        "project_name": XSS_PAYLOAD,
    },
    "password_reset.html": {
        "reset_link": "https://shop.example.com/reset-password?token=tok",
        "expire_minutes": 30,
        "project_name": XSS_PAYLOAD,
    },
    "gdpr_deletion_confirmation.html": {
        "request_id": XSS_PAYLOAD,
        "scheduled_at": "2026-01-01 00:00:00 UTC",
        "dpo_email": "dpo@example.com",
        "project_name": "Shop",
        "dpo_name": "DPO",
        "company_address": "123 St",
    },
    "gdpr_deletion_completed.html": {
        "completed_at": "2026-01-01 00:00:00 UTC",
        "retained_records": [XSS_PAYLOAD],
        "dpo_email": "dpo@example.com",
        "project_name": "Shop",
        "dpo_name": "DPO",
        "company_address": "123 St",
    },
    "gdpr_request_received.html": {
        "gdpr_article": "15",
        "request_type_text": "export",
        "request_id": XSS_PAYLOAD,
        "received_at": "2026-01-01 00:00:00 UTC",
        "dpo_email": "dpo@example.com",
        "project_name": "Shop",
        "dpo_name": "DPO",
        "company_address": "123 St",
    },
    "gdpr_export_ready.html": {
        "download_url": f"https://shop.example.com/account/data-export/{XSS_PAYLOAD}",
        "expiry_text": "2026-01-02 00:00:00 UTC",
        "dpo_email": "dpo@example.com",
        "project_name": "Shop",
        "dpo_name": "DPO",
        "company_address": "123 St",
    },
}


def test_all_email_templates_are_covered():
    on_disk = {path.name for path in TEMPLATES_DIR.glob("*.html")}
    assert on_disk == set(TEMPLATE_CONTEXTS)


@pytest.mark.parametrize("template_name", sorted(TEMPLATE_CONTEXTS))
def test_email_template_escapes_html_injection(template_name):
    rendered = render(template_name, **TEMPLATE_CONTEXTS[template_name])

    assert XSS_PAYLOAD not in rendered
    assert ESCAPED_XSS_PAYLOAD in rendered


def test_email_template_raises_on_missing_context_variable():
    with pytest.raises(UndefinedError):
        render("welcome.html", project_name="Shop")


def test_send_order_confirmation_email_escapes_html_in_product_name(monkeypatch):
    calls = []

    class _FakeProvider:
        def send(self, to_email, subject, html_content):
            calls.append(html_content)
            return True

    monkeypatch.setattr(email_module, "get_email_provider", _FakeProvider)
    order = SimpleNamespace(
        id=1,
        status="new",
        total_amount="10.00",
        currency="USD",
        created_at=utc_now(),
        items=[SimpleNamespace(product_name=XSS_PAYLOAD, quantity=1, price_at_time="10.00")],
    )

    assert email_module.send_order_confirmation_email("buyer@example.com", order) is True
    assert XSS_PAYLOAD not in calls[0]
    assert ESCAPED_XSS_PAYLOAD in calls[0]


def test_send_gdpr_deletion_completed_escapes_html_in_retained_records(monkeypatch):
    calls = []

    class _FakeProvider:
        def send(self, to_email, subject, html_content):
            calls.append(html_content)
            return True

    monkeypatch.setattr(email_module, "get_email_provider", _FakeProvider)

    assert email_module.send_gdpr_deletion_completed(
        "user@example.com", retained_records=[XSS_PAYLOAD]
    ) is True
    assert XSS_PAYLOAD not in calls[0]
    assert ESCAPED_XSS_PAYLOAD in calls[0]
