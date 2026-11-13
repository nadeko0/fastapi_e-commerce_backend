"""
A payment attempt left "unknown" (the provider could not be reached or
answered with an error; the client gets 503 Retry-After: 5) is logged once,
with what is known about the cause: the error's class, Stripe's request id,
HTTP status and code, the order and attempt, and a short hash of the
idempotency key. The load audit of 2026-11 found these 503s with no log
line at all. Never the key itself, the payment method or a client secret.
"""
import hashlib
import logging

import pytest
import stripe
from payment_support import PAY_OK, Shop, reset_provider

from app.main import app
from app.services.payment import get_payment_provider
from app.services.payment.exceptions import PaymentProviderTimeoutError
from app.services.payment.live_stripe_provider import LiveStripePaymentProvider
from app.services.payment.stripe_provider import StripePaymentProvider

LOGGER = "app.api.v1.orders"


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def shop(client, db_session):
    return Shop(client, db_session)


def _unknown_attempt_lines(caplog):
    return [r for r in caplog.records if r.name == LOGGER and "left unknown" in r.getMessage()]


def _raising(error):
    def stub(*args, **kwargs):
        raise error
    return stub


def test_a_stripe_rate_limit_on_pay_is_logged_with_its_request_id(shop, monkeypatch, caplog):
    monkeypatch.setitem(app.dependency_overrides, get_payment_provider,
                        lambda: LiveStripePaymentProvider(api_key="sk_test_not_a_real_key"))
    monkeypatch.setattr(stripe.PaymentIntentService, "create", _raising(stripe.RateLimitError(
        "Too many requests", http_status=429, headers={"request-id": "req_audit_429"},
        code="rate_limit")))
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())
    key = "client-key-rate-limited-1"

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        response = shop.pay(buyer, order["id"], key=key)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    (line,) = _unknown_attempt_lines(caplog)
    (payment,) = shop.payments(order["id"])
    assert payment.status == "unknown"
    assert line.levelno == logging.WARNING
    assert (line.flow, line.order_id, line.payment_id) == ("pay", order["id"], payment.id)
    assert (line.error_class, line.stripe_request_id, line.http_status, line.provider_code) == (
        "RateLimitError", "req_audit_429", 429, "rate_limit")
    provider_key = f"pay-order-{order['id']}:{key}"
    assert line.idempotency_key_sha256 == hashlib.sha256(provider_key.encode()).hexdigest()[:16]
    assert "RateLimitError" in line.getMessage() and "req_audit_429" in line.getMessage()
    logged = line.getMessage() + repr(line.__dict__)
    assert key not in logged
    assert PAY_OK not in logged and "sk_test_not_a_real_key" not in logged


def test_a_checkout_session_timeout_is_logged(shop, monkeypatch, caplog):
    monkeypatch.setattr(StripePaymentProvider, "create_checkout_session",
                        _raising(PaymentProviderTimeoutError("Connection dropped")))
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        response = shop.checkout_session(buyer, order["id"], key="client-key-checkout-1")

    assert response.status_code == 503
    (line,) = _unknown_attempt_lines(caplog)
    assert (line.flow, line.order_id) == ("checkout_session", order["id"])
    # Not from Stripe: nothing more is known than the error itself.
    assert (line.error_class, line.stripe_request_id, line.http_status, line.provider_code) == (
        "PaymentProviderTimeoutError", None, None, None)
    assert "client-key-checkout-1" not in line.getMessage() + repr(line.__dict__)
