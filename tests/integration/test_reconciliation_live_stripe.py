"""
The payment reconciliation against the REAL Stripe test-mode API: pay and
refund in Stripe, record the payment as the app does, check that
reconciliation finds nothing, then make our record diverge from Stripe's
(what a lost webhook or a Dashboard refund does) and check it is found, and
that it is resolved once the record is right again.

Like test_payment_live_stripe.py it is skipped unless a real Stripe test-mode
STRIPE_SECRET_KEY is in the environment or in .env.test (gitignored), and it
makes real requests: run it by hand now and then, not on every commit.
"""
import os
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from payment_support import Shop

from app.core.clock import utc_now
from app.models.payment import Payment
from app.models.payment_discrepancy import (
    KIND_AMOUNT,
    KIND_REFUND_NOT_RECORDED,
    KIND_REFUNDED_TOTAL,
    KIND_STATUS,
    PaymentDiscrepancy,
)
from app.models.payment_ledger import PaymentRefund
from app.services.payment.live_stripe_provider import LiveStripePaymentProvider
from app.services.reconciliation import reconcile_payments

_dotenv_test = Path(__file__).resolve().parents[2] / ".env.test"
if _dotenv_test.exists():
    from dotenv import load_dotenv

    load_dotenv(_dotenv_test, override=False)

_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
_HAS_REAL_KEY = _SECRET_KEY.startswith("sk_test_") and _SECRET_KEY != "sk_test_mock_placeholder"

pytestmark = pytest.mark.skipif(
    not _HAS_REAL_KEY,
    reason=(
        "No real Stripe test-mode STRIPE_SECRET_KEY in the environment "
        "(set STRIPE_SECRET_KEY or provide .env.test) - skipping the live "
        "reconciliation test."
    ),
)

RETURN_URL = "https://example.com/checkout/return"


@pytest.fixture
def provider():
    return LiveStripePaymentProvider(api_key=_SECRET_KEY)


def _open(db):
    db.expire_all()
    return sorted((d.kind, d.subject, d.ours, d.theirs)
                  for d in db.query(PaymentDiscrepancy)
                  .filter(PaymentDiscrepancy.resolved_at.is_(None)))


def test_a_payment_and_a_refund_in_stripe_reconcile_and_a_divergence_is_found(
        client, db_session, provider):
    shop = Shop(client, db_session)
    order = shop.order_of(shop.buyer(), shop.product())

    # Pay and refund in Stripe's test mode.
    intent = provider.create_payment_intent(
        amount=1099, currency="usd", idempotency_key=str(uuid.uuid4()),
        payment_method="pm_card_visa")
    provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)
    refund = provider.create_refund(
        payment_intent_id=intent.id, amount=300, idempotency_key=str(uuid.uuid4()))

    # Our record of it, as the payment flow and the webhooks leave it.
    payment = Payment(
        order_id=order["id"], provider="stripe", payment_intent_id=intent.id,
        amount=1099, amount_refunded=300, currency="USD", status="succeeded",
        created_at=utc_now() - timedelta(hours=2))
    db_session.add(payment)
    db_session.flush()
    db_session.add(PaymentRefund(
        payment_id=payment.id, refund_id=refund.id, amount=300, currency="USD",
        status=refund.provider_status, source="admin"))
    db_session.commit()

    # Nothing differs.
    result = reconcile_payments(db_session, provider, order_id=order["id"])
    assert (result.checked, result.errors, result.opened) == (1, 0, 0)
    assert _open(db_session) == []

    # A divergence: we lost track of the refund.
    payment.amount_refunded = 0
    db_session.query(PaymentRefund).delete()
    db_session.commit()
    result = reconcile_payments(db_session, provider, order_id=order["id"])
    assert result.opened == 2
    assert _open(db_session) == [
        (KIND_REFUND_NOT_RECORDED, refund.id, None, "succeeded 300"),
        (KIND_REFUNDED_TOTAL, "", "0", "300"),
    ]

    # And one of the amount and the status.
    payment.amount_refunded = 300
    payment.amount = 1200
    payment.status = "processing"
    db_session.commit()
    reconcile_payments(db_session, provider, order_id=order["id"])
    assert {k for k, *_ in _open(db_session)} == {
        KIND_AMOUNT, KIND_STATUS, KIND_REFUND_NOT_RECORDED}

    # Put right, everything resolves.
    payment.amount = 1099
    payment.status = "succeeded"
    db_session.add(PaymentRefund(
        payment_id=payment.id, refund_id=refund.id, amount=300, currency="USD",
        status="succeeded", source="admin"))
    db_session.commit()
    result = reconcile_payments(db_session, provider, order_id=order["id"])
    assert result.resolved == 3
    assert _open(db_session) == []
