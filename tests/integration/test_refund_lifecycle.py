"""
A refund after it was requested (app/api/v1/orders.py: refund_payment and
the refund.* / charge.refunded webhook events). Stripe documents that card
refunds can complete asynchronously (https://docs.stripe.com/testing,
"Refunds"): pm_card_pendingRefund starts pending and later succeeds
(refund.updated), pm_card_refundFail starts succeeded and later fails
(refund.failed). Refunds can also be made outside the app (the Stripe
Dashboard), which Stripe reports with refund.created.

- A pending refund is not reported as completed: the order stays paid
  until the refunds that have succeeded cover the charge.
- A refund that fails or is canceled after it was recorded is taken back
  out of amount_refunded, the order is no longer refunded, and the refund
  is flagged for manual review: the customer did not get that money.
- A refund made outside the app is recorded like one made through it; a
  refund reported twice (by /refund and by its webhook, in either order)
  is counted once.
"""
import uuid

import pytest
from payment_support import (
    Outbox,
    Shop,
    order_state,
    payment_rows,
    reset_provider,
    use_production_sessions,
)

from app.models.payment_ledger import PaymentRefund
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.payment.types import RefundStatus


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def outbox(monkeypatch):
    return Outbox(monkeypatch)


@pytest.fixture
def shop(client, db_session, monkeypatch):
    use_production_sessions(monkeypatch)
    return Shop(client, db_session)


@pytest.fixture
def paid(shop):
    product = shop.product(price="19.99", stock=4)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    assert shop.pay(buyer, order["id"]).status_code == 200
    return {"order": order, "admin": shop.admin(), "payment": shop.payments(order["id"])[0]}


def _refunds_start(monkeypatch, status):
    """The provider answers every refund with `status`, as Stripe does for
    pm_card_pendingRefund (pending)."""
    real_refund = StripePaymentProvider.create_refund

    def _refund(self, **kwargs):
        refund = real_refund(self, **kwargs)
        refund.status = status
        return refund

    monkeypatch.setattr(StripePaymentProvider, "create_refund", _refund)


def _refund_object(payment, refund_id, amount, status, failure_reason=None):
    return {"id": refund_id, "object": "refund", "amount": amount,
            "currency": payment.currency.lower(), "payment_intent": payment.payment_intent_id,
            "charge": f"ch_{uuid.uuid4().hex[:24]}", "status": status,
            "failure_reason": failure_reason, "metadata": {}}


def _ledger(shop, order_id):
    shop.db.expire_all()
    payment_ids = [p.id for p in shop.payments(order_id)]
    return [(r.refund_id[:3], r.amount, r.status, r.source, r.requires_manual_review)
            for r in shop.db.query(PaymentRefund)
            .filter(PaymentRefund.payment_id.in_(payment_ids)).order_by(PaymentRefund.id)]


# -- Pending, then succeeded -----------------------------------------------------

def test_a_pending_refund_is_not_reported_as_completed(shop, paid, monkeypatch):
    # The order used to be marked refunded as soon as the refund was
    # created, whatever its status: a pending refund that later failed
    # left a "refunded" order whose customer never got the money.
    _refunds_start(monkeypatch, RefundStatus.PENDING)
    order_id = paid["order"]["id"]

    response = shop.refund(paid["admin"], order_id)

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert (body["status"], body["payment_status"], body["amount"]) == ("pending", "paid", "19.99")
    assert order_state(shop, order_id) == ("confirmed", "paid")
    # Reserved: Stripe will not refund the same money twice, nor will we.
    assert payment_rows(shop, order_id) == [("succeeded", 1999, 1999, "USD", False)]
    assert shop.refund(paid["admin"], order_id, amount="0.01").status_code == 400

    refund_id = body["id"]
    done = shop.webhook("refund.updated",
                        _refund_object(paid["payment"], refund_id, 1999, "succeeded"))

    assert done.status_code == 200, done.text
    assert order_state(shop, order_id) == ("confirmed", "refunded")
    assert payment_rows(shop, order_id) == [("succeeded", 1999, 1999, "USD", False)]
    assert _ledger(shop, order_id) == [("re_", 1999, "succeeded", "admin", False)]


def test_a_partial_refund_completing_does_not_refund_the_order(shop, paid, monkeypatch):
    _refunds_start(monkeypatch, RefundStatus.PENDING)
    order_id = paid["order"]["id"]
    refund_id = shop.refund(paid["admin"], order_id, amount="5.00").json()["data"]["id"]

    shop.webhook("refund.updated", _refund_object(paid["payment"], refund_id, 500, "succeeded"))

    assert order_state(shop, order_id) == ("confirmed", "paid")
    assert payment_rows(shop, order_id) == [("succeeded", 1999, 500, "USD", False)]


@pytest.mark.parametrize("refund_id,status", [("re_x", None), (None, "succeeded")])
def test_a_refund_event_missing_its_id_or_status_changes_nothing(shop, paid, monkeypatch,
                                                                  refund_id, status):
    # Refund.status is nullable in Stripe's API reference.
    _refunds_start(monkeypatch, RefundStatus.PENDING)
    order_id = paid["order"]["id"]
    recorded = shop.refund(paid["admin"], order_id).json()["data"]["id"]
    obj = _refund_object(paid["payment"], refund_id and recorded, 1999, status)

    response = shop.webhook("refund.updated", obj)

    assert response.status_code == 200
    assert order_state(shop, order_id) == ("confirmed", "paid")
    assert _ledger(shop, order_id) == [("re_", 1999, "pending", "admin", False)]


# -- Refunds made outside the app -------------------------------------------------

def test_our_own_refund_reported_by_its_webhook_is_counted_once(shop, paid):
    order_id = paid["order"]["id"]
    refund = shop.refund(paid["admin"], order_id, amount="5.00").json()["data"]

    for event_type in ("refund.created", "refund.updated", "charge.refunded"):
        obj = _refund_object(paid["payment"], refund["id"], 500, "succeeded")
        if event_type == "charge.refunded":
            obj = {"id": "ch_x", "object": "charge", "payment_intent": paid["payment"].payment_intent_id,
                   "amount": 1999, "amount_refunded": 500, "refunded": False, "currency": "usd",
                   "refunds": {"object": "list", "data": [obj]}}
        assert shop.webhook(event_type, obj).status_code == 200

    assert payment_rows(shop, order_id) == [("succeeded", 1999, 500, "USD", False)]
    assert _ledger(shop, order_id) == [("re_", 500, "succeeded", "admin", False)]


def test_charge_refunded_without_its_refund_list_changes_nothing(shop, paid):
    # Current API versions do not include Charge.refunds in the event
    # (Stripe: "Listen to refund.created for information about the
    # refund"); the cumulative amount alone says nothing about which refund
    # it is or whether it will succeed.
    order_id = paid["order"]["id"]
    obj = {"id": "ch_x", "object": "charge", "payment_intent": paid["payment"].payment_intent_id,
           "amount": 1999, "amount_refunded": 500, "refunded": False, "currency": "usd"}

    response = shop.webhook("charge.refunded", obj)

    assert response.status_code == 200
    assert payment_rows(shop, order_id) == [("succeeded", 1999, 0, "USD", False)]


def test_refund_events_for_a_charge_this_app_did_not_make_are_acknowledged(shop, paid):
    obj = {"id": "re_elsewhere", "object": "refund", "amount": 100, "currency": "usd",
           "payment_intent": "pi_not_ours", "status": "succeeded"}

    response = shop.webhook("refund.created", obj)

    assert response.status_code == 200
    assert response.json()["data"] == {"status": "processed", "applied": False}
    assert payment_rows(shop, paid["order"]["id"]) == [("succeeded", 1999, 0, "USD", False)]
