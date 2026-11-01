"""
Integration tests covering payment failure paths, checkout session reconciliation
differences, and unpaid order expiry edge cases.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from conftest import TestingSessionLocal
from payment_support import PAY_3DS, Shop, reset_provider

from app import tasks
from app.core.clock import utc_now
from app.core.config import settings
from app.models.enums import OrderStatus, PaymentStatus
from app.models.order import Order
from app.models.payment_discrepancy import (
    KIND_PAYMENT_MISSING,
    KIND_STATUS,
    PaymentDiscrepancy,
)
from app.models.payment_ledger import PaymentRefund
from app.services.payment.exceptions import CardError, PaymentProviderError
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.payment.types import CheckoutSessionStatus
from app.services.reconciliation import (
    Difference,
    _compare_session,
    reconcile_payments,
)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    reset_provider()
    monkeypatch.setattr("app.api.v1.orders.send_order_confirmation_email", lambda *a, **k: True)
    monkeypatch.setattr("app.api.v1.orders.send_order_status_update_email", lambda *a, **k: True)
    monkeypatch.setattr("app.tasks.send_order_cancellation_email", lambda *a, **k: True)
    yield
    reset_provider()


@pytest.fixture
def shop(client, db_session):
    return Shop(client, db_session)


@pytest.fixture
def provider():
    return StripePaymentProvider()


# ---------------------------------------------------------------------------
# 1. POST /orders/{id}/refund failure path
# ---------------------------------------------------------------------------

def test_refund_payment_succeeds_when_list_refunds_raises_payment_provider_error(
    shop, monkeypatch
):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product(price="20.00", stock=5))
    pay_resp = shop.pay(buyer, order["id"])
    assert pay_resp.status_code == 200, pay_resp.text
    admin = shop.admin()

    def _failing_list_refunds(self, payment_intent_id):
        raise PaymentProviderError("Stripe list_refunds unavailable")

    monkeypatch.setattr(StripePaymentProvider, "list_refunds", _failing_list_refunds)

    response = shop.refund(admin, order["id"], amount="10.00")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["order_id"] == order["id"]
    assert Decimal(str(body["amount"])) == Decimal("10.00")
    assert body["payment_status"] == "paid"
    assert body["status"] == "succeeded"

    payments = shop.payments(order["id"])
    assert len(payments) == 1
    assert payments[0].amount_refunded == 1000

    refunds = (
        shop.db.query(PaymentRefund)
        .filter(PaymentRefund.payment_id == payments[0].id)
        .all()
    )
    assert len(refunds) == 1
    assert refunds[0].amount == 1000
    assert refunds[0].refund_id == body["id"]
    assert refunds[0].status == "succeeded"

    assert shop.order(order["id"]).payment_status == "paid"


# ---------------------------------------------------------------------------
# 2. _compare_session failure and discrepancy paths
# ---------------------------------------------------------------------------

def test_compare_session_resource_missing_opens_payment_missing_discrepancy(shop, provider):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())
    session_resp = shop.checkout_session(buyer, order["id"])
    assert session_resp.status_code == 200, session_resp.text
    (payment,) = shop.payments(order["id"])
    session_id = payment.checkout_session_id

    # The session is missing at the provider
    StripePaymentProvider._sessions_by_id.pop(session_id, None)

    diffs = _compare_session(provider, session_id, payment.status)
    assert diffs == [Difference(KIND_PAYMENT_MISSING, "", session_id, None)]

    result = reconcile_payments(shop.db, provider, order_id=order["id"])
    assert result.opened == 1
    assert result.checked == 1
    assert result.errors == 0

    discrepancy = (
        shop.db.query(PaymentDiscrepancy)
        .filter(PaymentDiscrepancy.payment_id == payment.id)
        .one()
    )
    assert discrepancy.kind == KIND_PAYMENT_MISSING
    assert discrepancy.ours == session_id
    assert discrepancy.theirs is None
    assert discrepancy.resolved_at is None


def test_compare_session_other_card_error_counts_as_error_and_run_continues(
    shop, provider, monkeypatch
):
    buyer = shop.buyer()
    # First payment: agreed paid order (lower id)
    order2 = shop.order_of(buyer, shop.product(price="25.00"))
    assert shop.pay(buyer, order2["id"]).status_code == 200

    # Second payment: checkout session attempt (higher id, checked first by reconciliation)
    order1 = shop.order_of(buyer, shop.product(price="15.00"))
    assert shop.checkout_session(buyer, order1["id"]).status_code == 200
    (payment1,) = shop.payments(order1["id"])

    def _card_error_retrieve(self, session_id):
        raise CardError("Card error not missing", code="api_error")

    monkeypatch.setattr(StripePaymentProvider, "retrieve_checkout_session", _card_error_retrieve)

    with pytest.raises(CardError) as exc_info:
        _compare_session(provider, payment1.checkout_session_id, payment1.status)
    assert exc_info.value.code == "api_error"

    result = reconcile_payments(
        shop.db, provider, now=utc_now() + timedelta(hours=2)
    )

    assert result.errors == 1
    assert result.error_payment_ids == [payment1.id]
    assert result.checked >= 1
    assert result.aborted is False

    assert (
        shop.db.query(PaymentDiscrepancy)
        .filter(PaymentDiscrepancy.payment_id == payment1.id)
        .all()
        == []
    )


@pytest.mark.parametrize(
    ("session_status", "payment_status", "expected_theirs"),
    [
        (CheckoutSessionStatus.COMPLETE, "unpaid", "session complete/unpaid"),
        (CheckoutSessionStatus.OPEN, "paid", "session open/paid"),
    ],
)
def test_compare_session_complete_or_paid_opens_status_discrepancy(
    shop, provider, session_status, payment_status, expected_theirs
):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())
    assert shop.checkout_session(buyer, order["id"]).status_code == 200
    (payment,) = shop.payments(order["id"])

    session = StripePaymentProvider._sessions_by_id[payment.checkout_session_id]
    session.status = session_status
    session.payment_status = payment_status

    diffs = _compare_session(provider, session.id, payment.status)
    assert diffs == [Difference(KIND_STATUS, "", payment.status, expected_theirs)]

    result = reconcile_payments(shop.db, provider, order_id=order["id"])
    assert result.opened == 1
    assert result.checked == 1
    assert result.errors == 0

    discrepancy = (
        shop.db.query(PaymentDiscrepancy)
        .filter(PaymentDiscrepancy.payment_id == payment.id)
        .one()
    )
    assert discrepancy.kind == KIND_STATUS
    assert discrepancy.ours == payment.status
    assert discrepancy.theirs == expected_theirs


def test_compare_session_open_unpaid_session_opens_no_discrepancy(shop, provider):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())
    assert shop.checkout_session(buyer, order["id"]).status_code == 200
    (payment,) = shop.payments(order["id"])

    session = StripePaymentProvider._sessions_by_id[payment.checkout_session_id]
    assert session.status == CheckoutSessionStatus.OPEN
    assert session.payment_status == "unpaid"

    assert _compare_session(provider, session.id, payment.status) == []

    result = reconcile_payments(shop.db, provider, order_id=order["id"])
    assert result.checked == 1
    assert result.opened == 0
    assert result.errors == 0

    assert (
        shop.db.query(PaymentDiscrepancy)
        .filter(PaymentDiscrepancy.payment_id == payment.id)
        .all()
        == []
    )


# ---------------------------------------------------------------------------
# 3. SoftTimeLimitExceeded during comparison
# ---------------------------------------------------------------------------

def test_reconcile_payments_soft_time_limit_stops_run_cleanly_with_aborted(
    shop, provider, monkeypatch
):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())
    assert shop.pay(buyer, order["id"]).status_code == 200

    def _timeout_retrieve(self, payment_intent_id):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(StripePaymentProvider, "retrieve_payment_intent", _timeout_retrieve)

    result = reconcile_payments(shop.db, provider, order_id=order["id"])

    assert result.aborted is True
    assert result.checked == 0
    assert result.errors == 0
    assert result.opened == 0
    assert shop.db.query(PaymentDiscrepancy).all() == []


def test_reconcile_payments_soft_time_limit_during_session_compare_aborts_cleanly(
    shop, provider, monkeypatch
):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())
    assert shop.checkout_session(buyer, order["id"]).status_code == 200

    def _timeout_session(self, session_id):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(StripePaymentProvider, "retrieve_checkout_session", _timeout_session)

    result = reconcile_payments(shop.db, provider, order_id=order["id"])

    assert result.aborted is True
    assert result.checked == 0
    assert result.errors == 0
    assert result.opened == 0
    assert shop.db.query(PaymentDiscrepancy).all() == []


# ---------------------------------------------------------------------------
# 4. Expiry sweep failure paths
# ---------------------------------------------------------------------------

def test_expiry_sweep_attempt_older_than_window_provider_unreachable_leaves_order_new(
    client, db_session, tasks_db, monkeypatch
):
    shop = Shop(client, db_session)
    product = shop.product(price="10.00", stock=10)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product, quantity=3)
    pay_resp = shop.pay(buyer, order["id"], token=PAY_3DS)
    assert pay_resp.status_code == 200

    assert shop.stock(product) == 7
    assert shop.order(order["id"]).status == "new"
    assert shop.order(order["id"]).payment_status == "pending"

    age = utc_now() - timedelta(minutes=settings.CHECKOUT_SESSION_EXPIRE_MINUTES + 1)
    # Committed in between: shop.order expires the session, which would discard it.
    payment = shop.payments(order["id"])[0]
    payment.created_at = age
    shop.db.commit()
    db_order = shop.order(order["id"])
    db_order.created_at = age
    shop.db.commit()

    def _unreachable_cancel(self, payment_intent_id):
        raise PaymentProviderError("Stripe provider is unreachable")

    with monkeypatch.context() as m:
        m.setattr(StripePaymentProvider, "cancel_payment_intent", _unreachable_cancel)
        cancelled = tasks.cancel_expired_unpaid_orders()
        assert cancelled == 0

        assert shop.order(order["id"]).status == "new"
        assert shop.order(order["id"]).payment_status == "pending"
        assert shop.stock(product) == 7

    cancelled_later = tasks.cancel_expired_unpaid_orders()
    assert cancelled_later == 1
    assert shop.order(order["id"]).status == "cancelled"
    assert shop.order(order["id"]).payment_status == "pending"
    assert shop.stock(product) == 10


def test_expiry_sweep_checkout_session_attempt_provider_unreachable_leaves_order_new(
    client, db_session, tasks_db, monkeypatch
):
    shop = Shop(client, db_session)
    product = shop.product(price="10.00", stock=10)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product, quantity=2)
    assert shop.checkout_session(buyer, order["id"]).status_code == 200

    assert shop.stock(product) == 8
    assert shop.order(order["id"]).status == "new"

    age = utc_now() - timedelta(minutes=settings.CHECKOUT_SESSION_EXPIRE_MINUTES + 1)
    # Committed in between: shop.order expires the session, which would discard it.
    payment = shop.payments(order["id"])[0]
    payment.created_at = age
    shop.db.commit()
    db_order = shop.order(order["id"])
    db_order.created_at = age
    shop.db.commit()

    def _unreachable_expire(self, session_id):
        raise PaymentProviderError("Stripe provider unreachable")

    with monkeypatch.context() as m:
        m.setattr(StripePaymentProvider, "expire_checkout_session", _unreachable_expire)
        cancelled = tasks.cancel_expired_unpaid_orders()
        assert cancelled == 0
        assert shop.order(order["id"]).status == "new"
        assert shop.stock(product) == 8

    cancelled_later = tasks.cancel_expired_unpaid_orders()
    assert cancelled_later == 1
    assert shop.order(order["id"]).status == "cancelled"
    assert shop.stock(product) == 10


def test_expiry_sweep_attempt_older_than_window_paid_during_cancel_leaves_order_alone(
    client, db_session, tasks_db, monkeypatch
):
    shop = Shop(client, db_session)
    product = shop.product(price="10.00", stock=10)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product, quantity=3)
    pay_resp = shop.pay(buyer, order["id"], token=PAY_3DS)
    assert pay_resp.status_code == 200

    assert shop.stock(product) == 7
    assert shop.order(order["id"]).status == "new"
    assert shop.order(order["id"]).payment_status == "pending"

    age = utc_now() - timedelta(minutes=settings.CHECKOUT_SESSION_EXPIRE_MINUTES + 1)
    # Committed in between: shop.order expires the session, which would discard it.
    payment = shop.payments(order["id"])[0]
    payment.created_at = age
    shop.db.commit()
    db_order = shop.order(order["id"])
    db_order.created_at = age
    shop.db.commit()

    real_cancel = StripePaymentProvider.cancel_payment_intent

    def _cancel_and_mark_paid(self, payment_intent_id, *args, **kwargs):
        other_session = TestingSessionLocal()
        try:
            o = other_session.get(Order, order["id"])
            o.status = OrderStatus.CONFIRMED
            o.payment_status = PaymentStatus.PAID
            other_session.commit()
        finally:
            other_session.close()
        return real_cancel(self, payment_intent_id, *args, **kwargs)

    monkeypatch.setattr(StripePaymentProvider, "cancel_payment_intent", _cancel_and_mark_paid)

    cancelled = tasks.cancel_expired_unpaid_orders()
    assert cancelled == 0

    refreshed_order = shop.order(order["id"])
    assert refreshed_order.status == "confirmed"
    assert refreshed_order.payment_status == "paid"
    assert shop.stock(product) == 7
