"""
Which charge a refund returns, and how the refunded amount is recorded on
the Payment row (app/api/v1/orders.py, refund_payment): against the charge
that paid the order, never a second charge flagged for manual review, and
added to what is recorded at the time of writing, not to what was read
before the provider was called.
"""
from datetime import timedelta

import pytest
from payment_support import (
    PAY_3DS,
    Shop,
    order_state,
    payment_rows,
    reset_provider,
    use_production_sessions,
)

from app.core.config import settings
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
