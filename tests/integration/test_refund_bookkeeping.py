"""
Which charge a refund returns, and how the refunded amount is recorded on
the Payment row (app/api/v1/orders.py, refund_payment): against the charge
that paid the order, never a second charge flagged for manual review, and
added to what is recorded at the time of writing, not to what was read
before the provider was called.
"""
from datetime import timedelta

import pytest
from conftest import TestingSessionLocal
from payment_support import (
    PAY_3DS,
    Shop,
    order_state,
    payment_rows,
    reset_provider,
    use_production_sessions,
)

from app.core.config import settings
from app.models.payment import Payment
from app.services.payment.stripe_provider import StripePaymentProvider


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def shop(client, db_session, monkeypatch):
    use_production_sessions(monkeypatch)
    return Shop(client, db_session)


def _age_attempts(shop, order_id):
    """Move the order's attempts past PAYMENT_ATTEMPT_STALE_AFTER_MINUTES,
    where one still in flight no longer blocks another."""
    for payment in shop.payments(order_id):
        payment.created_at -= timedelta(minutes=settings.PAYMENT_ATTEMPT_STALE_AFTER_MINUTES + 1)
    shop.db.commit()


def test_refund_returns_the_charge_that_paid_the_order_not_a_flagged_second_one(shop):
    # Two charges for one order: a 3DS attempt left in flight long enough
    # to stop blocking, a second attempt, and the customer finishing 3DS
    # for both. The first success pays the order, the second is flagged
    # for manual review. The refund picked the newest succeeded attempt -
    # the flagged one - so the order became "refunded" while the charge
    # that paid it was never returned.
    product = shop.product(price="19.99", stock=4)
    buyer = shop.buyer()
    admin = shop.admin()
    order = shop.order_of(buyer, product)
    assert shop.pay(buyer, order["id"], token=PAY_3DS).status_code == 200
    _age_attempts(shop, order["id"])
    assert shop.pay(buyer, order["id"], token=PAY_3DS).status_code == 200
    first, second = shop.payments(order["id"])
    for payment in (first, second):  # 3D Secure completed at Stripe
        StripePaymentProvider().confirm_payment_intent(
            payment.payment_intent_id, return_url="https://shop.test/return")
    assert shop.intent_event("payment_intent.succeeded", first).status_code == 200
    assert shop.intent_event("payment_intent.succeeded", second).status_code == 200
    assert payment_rows(shop, order["id"]) == [
        ("succeeded", 1999, 0, "USD", False), ("succeeded", 1999, 0, "USD", True)]

    response = shop.refund(admin, order["id"])

    assert response.status_code == 200, response.text
    assert payment_rows(shop, order["id"]) == [
        ("succeeded", 1999, 1999, "USD", False), ("succeeded", 1999, 0, "USD", True)]
    assert order_state(shop, order["id"]) == ("confirmed", "refunded")


def test_refunds_completing_at_the_same_time_are_both_counted(shop, monkeypatch):
    # The route read the attempt, waited on the provider, and wrote back
    # what it had read plus its own refund. A refund that completed in the
    # meantime (another admin, a retried request with its own key) was
    # overwritten: two refunds taken by Stripe, one recorded.
    product = shop.product(price="19.99", stock=4)
    buyer = shop.buyer()
    admin = shop.admin()
    order = shop.order_of(buyer, product)
    assert shop.pay(buyer, order["id"]).status_code == 200
    real_refund = StripePaymentProvider.create_refund

    def _another_refund_completes_meanwhile(self, **kwargs):
        other = real_refund(self, payment_intent_id=kwargs["payment_intent_id"],
                            amount=500, idempotency_key="the-other-refund")
        db = TestingSessionLocal()
        try:
            payment = db.query(Payment).filter(Payment.order_id == order["id"]).one()
            payment.amount_refunded += other.amount
            db.commit()
        finally:
            db.close()
        return real_refund(self, **kwargs)

    monkeypatch.setattr(StripePaymentProvider, "create_refund", _another_refund_completes_meanwhile)

    response = shop.refund(admin, order["id"], amount="4.99")

    assert response.status_code == 200, response.text
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 999, "USD", False)]
    assert order_state(shop, order["id"]) == ("confirmed", "paid")


def test_concurrent_refunds_adding_up_to_the_charge_mark_the_order_refunded(shop, monkeypatch):
    product = shop.product(price="19.99", stock=4)
    buyer = shop.buyer()
    admin = shop.admin()
    order = shop.order_of(buyer, product)
    assert shop.pay(buyer, order["id"]).status_code == 200
    real_refund = StripePaymentProvider.create_refund

    def _another_refund_completes_meanwhile(self, **kwargs):
        other = real_refund(self, payment_intent_id=kwargs["payment_intent_id"],
                            amount=1000, idempotency_key="the-other-refund")
        db = TestingSessionLocal()
        try:
            payment = db.query(Payment).filter(Payment.order_id == order["id"]).one()
            payment.amount_refunded += other.amount
            db.commit()
        finally:
            db.close()
        return real_refund(self, **kwargs)

    monkeypatch.setattr(StripePaymentProvider, "create_refund", _another_refund_completes_meanwhile)

    response = shop.refund(admin, order["id"], amount="9.99")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["payment_status"] == "refunded"
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 1999, "USD", False)]
    assert order_state(shop, order["id"]) == ("confirmed", "refunded")
