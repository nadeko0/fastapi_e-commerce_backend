"""
The order emails of app/services/email/messages.py (confirmation, status
update, cancellation), rendered with the real message builders and only
the email provider replaced: amounts are stated in the order's own
currency.
"""
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.order import Order
from app.services.email import messages


@pytest.fixture
def outbox(monkeypatch):
    sent = []

    class Capture:
        def send(self, to_email, subject, html_content):
            sent.append((to_email, subject, html_content))
            return True

    monkeypatch.setattr("app.services.email.get_email_provider", lambda: Capture())
    return sent


def _order(currency, status="new"):
    order = Order(id=42, status=status, payment_status="pending", user_id=1,
                  shipping_address_id=1, total_amount=Decimal("12.50"), currency=currency,
                  created_at=datetime(2026, 10, 25, 1, 30, 0, tzinfo=UTC))
    # The confirmation reads the flat OrderItemResponse fields.
    order.items = []
    return order


def _confirmation_order(currency):
    line = SimpleNamespace(product_name="Widget <b>", quantity=5,
                           price_at_time=Decimal("2.50"))
    return SimpleNamespace(id=42, status="new", total_amount=Decimal("12.50"),
                           currency=currency, created_at=datetime(2026, 10, 25, 1, 30, 0, tzinfo=UTC),
                           items=[line])


SENDERS = {
    "confirmation": lambda c: messages.send_order_confirmation_email(
        "buyer@example.com", _confirmation_order(c)),
    "status update": lambda c: messages.send_order_status_update_email(
        "buyer@example.com", _order(c, status="sent")),
    "cancellation": lambda c: messages.send_order_cancellation_email(
        "buyer@example.com", _order(c, status="cancelled")),
}


# Every order email wrote "$" before the amount whatever the store's
# currency: a EUR, GBP or PLN store told its customers they paid dollars.
@pytest.mark.parametrize("currency", ["USD", "EUR", "GBP", "PLN", "HUF", "TWD"])
@pytest.mark.parametrize("email", list(SENDERS))
def test_the_amount_is_in_the_orders_currency(outbox, email, currency):
    assert SENDERS[email](currency) is True

    [(to_email, _, body)] = outbox
    assert to_email == "buyer@example.com"
    assert f"Total Amount: 12.50 {currency}" in body
    assert "$" not in body


def test_each_line_price_is_in_the_orders_currency_too(outbox):
    SENDERS["confirmation"]("EUR")
    [(_, subject, body)] = outbox
    assert subject == "Order Confirmation #42"
    assert "<td>Widget &lt;b&gt;</td><td>5</td><td>2.50 EUR</td>" in body
