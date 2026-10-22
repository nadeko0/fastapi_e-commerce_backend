"""
The reconciliation through the live adapter, on the in-process stand-in for
api.stripe.com: the same story as tests/integration/test_reconciliation_live_stripe.py
(pay, refund, find nothing, diverge, find it, put it right, resolved), so that
the wiring of the adapter (amount_received, the refund list, the statuses as
Stripe words them) is checked on every run and not only when someone has a
Stripe test key.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from test_reconciliation_provider import API_KEY, RETURN_URL, ListingFakeStripe

from app.core.clock import utc_now
from app.models.address import Address
from app.models.order import Order
from app.models.payment import Payment
from app.models.payment_discrepancy import PaymentDiscrepancy
from app.models.payment_ledger import PaymentRefund
from app.models.user import User
from app.services.payment.live_stripe_provider import LiveStripePaymentProvider
from app.services.reconciliation import reconcile_payments


@pytest.fixture
def provider(monkeypatch):
    ListingFakeStripe(API_KEY).install(monkeypatch)
    return LiveStripePaymentProvider(api_key=API_KEY)


def _order_id(db):
    """The id of a real order: PostgreSQL enforces payments.order_id."""
    user = User(email=f"buyer-{uuid.uuid4().hex[:10]}@example.com", hashed_password="x",
                full_name="Buyer One", role="client", is_active=True)
    db.add(user)
    db.commit()
    address = Address(user_id=user.id, street="1 Main Street", city="Metropolis",
                      state="NY", postal_code="10001", country="US")
    db.add(address)
    db.commit()
    order = Order(user_id=user.id, status="new", payment_status="pending",
                  shipping_address_id=address.id, total_amount=Decimal("10.99"))
    db.add(order)
    db.commit()
    return order.id


def _open(db):
    db.expire_all()
    return sorted((d.kind, d.subject, d.ours, d.theirs)
                  for d in db.query(PaymentDiscrepancy)
                  .filter(PaymentDiscrepancy.resolved_at.is_(None)))


def test_reconciling_through_the_live_adapter(db_session, provider):
    db = db_session
    intent = provider.create_payment_intent(
        amount=1099, currency="usd", idempotency_key="adapter-1", payment_method="pm_card_visa")
    provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)
    refund = provider.create_refund(payment_intent_id=intent.id, amount=300)
    payment = Payment(
        order_id=_order_id(db), provider="stripe", payment_intent_id=intent.id, amount=1099,
        amount_refunded=300, currency="USD", status="succeeded",
        created_at=utc_now() - timedelta(days=1))
    db.add(payment)
    db.flush()
    db.add(PaymentRefund(payment_id=payment.id, refund_id=refund.id, amount=300,
                         currency="USD", status="succeeded", source="admin"))
    db.commit()

    assert reconcile_payments(db, provider).opened == 0
    assert _open(db) == []

    payment.amount_refunded = 0
    db.query(PaymentRefund).delete()
    payment.status = "processing"
    db.commit()
    assert reconcile_payments(db, provider).opened == 3
    assert _open(db) == [
        ("refund_not_recorded", refund.id, None, "succeeded 300"),
        ("refunded_total", "", "0", "300"),
        ("status", "", "processing", "succeeded"),
    ]

    payment.amount_refunded = 300
    payment.status = "succeeded"
    db.add(PaymentRefund(payment_id=payment.id, refund_id=refund.id, amount=300,
                         currency="USD", status="succeeded", source="admin"))
    db.commit()
    assert reconcile_payments(db, provider).resolved == 3
    assert _open(db) == []


def test_a_payment_stripe_does_not_have_is_reported_through_the_adapter(db_session, provider):
    db_session.add(Payment(
        order_id=_order_id(db_session), provider="stripe", payment_intent_id="pi_nowhere", amount=100,
        currency="USD", status="succeeded", created_at=utc_now() - timedelta(days=1)))
    db_session.commit()

    result = reconcile_payments(db_session, provider)

    assert (result.checked, result.errors) == (1, 0)
    assert _open(db_session) == [("payment_missing", "", "pi_nowhere", None)]
