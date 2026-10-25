"""Tests for Stripe 429 (rate limit) error mapping across provider and API layers.

When Stripe's API returns a 429 (RateLimitError), LiveStripePaymentProvider maps it
via _provider_unavailable into PaymentProviderTimeoutError.
The orders API (/pay, /checkout-session, /refund) in app/api/v1/orders.py catches
PaymentProviderTimeoutError and returns 503 Service Unavailable with a Retry-After
header telling the client when to retry.
"""

from unittest.mock import MagicMock

import pytest
import stripe

from app.core.security import create_access_token
from app.main import app
from app.models.order import Order
from app.models.payment import Payment
from app.schemas.order import OrderStatus, PaymentStatus
from app.services.payment import get_payment_provider
from app.services.payment.exceptions import PaymentProviderTimeoutError
from app.services.payment.live_stripe_provider import LiveStripePaymentProvider
from tests.integration.test_tasks import (
    _make_category_and_product,
    _make_order_with_item,
    _make_verified_user,
)


@pytest.fixture
def live_provider():
    return LiveStripePaymentProvider(api_key="unit-test-key-not-a-real-stripe-key")


def test_live_stripe_provider_maps_rate_limit_error_to_timeout(live_provider, monkeypatch):
    """When Stripe SDK raises stripe.RateLimitError, LiveStripePaymentProvider maps it
    to PaymentProviderTimeoutError across operations."""
    rate_limit_err = stripe.RateLimitError("Rate limit exceeded")

    # create_payment_intent
    monkeypatch.setattr(
        stripe.PaymentIntentService,
        "create",
        MagicMock(side_effect=rate_limit_err),
    )
    with pytest.raises(PaymentProviderTimeoutError):
        live_provider.create_payment_intent(
            amount=1000, currency="usd", idempotency_key="test-key-create"
        )

    # confirm_payment_intent
    monkeypatch.setattr(
        stripe.PaymentIntentService,
        "confirm",
        MagicMock(side_effect=rate_limit_err),
    )
    with pytest.raises(PaymentProviderTimeoutError):
        live_provider.confirm_payment_intent("pi_test", return_url="https://example.com/return")

    # create_checkout_session
    monkeypatch.setattr(
        stripe.checkout.SessionService,
        "create",
        MagicMock(side_effect=rate_limit_err),
    )
    with pytest.raises(PaymentProviderTimeoutError):
        live_provider.create_checkout_session(
            amount=1000,
            currency="usd",
            idempotency_key="test-key-cs",
            success_url="https://shop.example/success",
            cancel_url="https://shop.example/cancel",
        )

    # create_refund
    monkeypatch.setattr(
        stripe.RefundService,
        "create",
        MagicMock(side_effect=rate_limit_err),
    )
    with pytest.raises(PaymentProviderTimeoutError):
        live_provider.create_refund(payment_intent_id="pi_test")


def test_orders_pay_endpoint_maps_rate_limit_to_503(client, db_session):
    """When the payment provider raises PaymentProviderTimeoutError during /pay,
    the API responds with 503 Service Unavailable and a Retry-After header."""
    user = _make_verified_user(db_session, email="buyer_ratelimit@example.com")
    _, product = _make_category_and_product(db_session)
    order = _make_order_with_item(
        db_session, user, product, status="new", payment_status="pending"
    )

    token = create_access_token(user.id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "test-idempotency-key-pay-1",
    }

    fake_provider = MagicMock()
    fake_provider.create_payment_intent.side_effect = PaymentProviderTimeoutError(
        "Rate limit exceeded, retry later"
    )

    app.dependency_overrides[get_payment_provider] = lambda: fake_provider
    try:
        response = client.post(
            f"/api/v1/orders/{order.id}/pay",
            json={
                "order_id": order.id,
                "payment_method": "stripe",
                "amount": str(order.total_amount),
            },
            headers=headers,
        )
        assert response.status_code == 503
        assert response.headers.get("retry-after") == "5"
        assert response.json()["detail"] == "Payment provider unavailable, please retry"

        # Verify the attempt was flagged as unknown status
        db_session.expire_all()
        reloaded_order = db_session.get(Order, order.id)
        assert reloaded_order.status == OrderStatus.NEW
        assert reloaded_order.payment_status == PaymentStatus.PENDING
    finally:
        app.dependency_overrides.pop(get_payment_provider, None)


def test_orders_checkout_session_endpoint_maps_rate_limit_to_503(client, db_session):
    """When the payment provider raises PaymentProviderTimeoutError during /checkout-session,
    the API responds with 503 Service Unavailable and a Retry-After header."""
    user = _make_verified_user(db_session, email="buyer_cs_ratelimit@example.com")
    _, product = _make_category_and_product(db_session)
    order = _make_order_with_item(
        db_session, user, product, status="new", payment_status="pending"
    )

    token = create_access_token(user.id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "test-idempotency-key-cs-1",
    }

    fake_provider = MagicMock()
    fake_provider.create_checkout_session.side_effect = PaymentProviderTimeoutError(
        "Rate limit exceeded, retry later"
    )

    app.dependency_overrides[get_payment_provider] = lambda: fake_provider
    try:
        response = client.post(
            f"/api/v1/orders/{order.id}/checkout-session",
            json={},
            headers=headers,
        )
        assert response.status_code == 503
        assert response.headers.get("retry-after") == "5"
        assert response.json()["detail"] == "Payment provider unavailable, please retry"
    finally:
        app.dependency_overrides.pop(get_payment_provider, None)


def test_orders_refund_endpoint_maps_rate_limit_to_503(client, db_session):
    """When the payment provider raises PaymentProviderTimeoutError during /refund,
    the API responds with 503 Service Unavailable and a Retry-After header."""
    admin_user = _make_verified_user(db_session, email="admin_refund@example.com")
    admin_user.role = "admin"
    db_session.commit()

    buyer = _make_verified_user(db_session, email="buyer_refund@example.com")
    _, product = _make_category_and_product(db_session)
    order = _make_order_with_item(
        db_session, buyer, product, status="confirmed", payment_status="paid"
    )

    payment = Payment(
        order_id=order.id,
        payment_intent_id="pi_test_refund_paid",
        amount=int(order.total_amount * 100),
        currency="USD",
        status="succeeded",
    )
    db_session.add(payment)
    db_session.commit()

    token = create_access_token(admin_user.id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "test-idempotency-key-refund-1",
    }

    fake_provider = MagicMock()
    fake_provider.create_refund.side_effect = PaymentProviderTimeoutError(
        "Rate limit exceeded, retry later"
    )

    app.dependency_overrides[get_payment_provider] = lambda: fake_provider
    try:
        response = client.post(
            f"/api/v1/orders/{order.id}/refund",
            json={"amount": str(order.total_amount)},
            headers=headers,
        )
        assert response.status_code == 503
        assert response.headers.get("retry-after") == "5"
        assert response.json()["detail"] == "Payment provider unavailable, please retry"
    finally:
        app.dependency_overrides.pop(get_payment_provider, None)
