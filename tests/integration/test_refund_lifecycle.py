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
)

from app.models.payment import Payment
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
    body = response.json()
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
    refund_id = shop.refund(paid["admin"], order_id, amount="5.00").json()["id"]

    shop.webhook("refund.updated", _refund_object(paid["payment"], refund_id, 500, "succeeded"))

    assert order_state(shop, order_id) == ("confirmed", "paid")
    assert payment_rows(shop, order_id) == [("succeeded", 1999, 500, "USD", False)]


@pytest.mark.parametrize("refund_id,status", [("re_x", None), (None, "succeeded")])
def test_a_refund_event_missing_its_id_or_status_changes_nothing(shop, paid, monkeypatch,
                                                                  refund_id, status):
    # Refund.status is nullable in Stripe's API reference.
    _refunds_start(monkeypatch, RefundStatus.PENDING)
    order_id = paid["order"]["id"]
    recorded = shop.refund(paid["admin"], order_id).json()["id"]
    obj = _refund_object(paid["payment"], refund_id and recorded, 1999, status)

    response = shop.webhook("refund.updated", obj)

    assert response.status_code == 200
    assert order_state(shop, order_id) == ("confirmed", "paid")
    assert _ledger(shop, order_id) == [("re_", 1999, "pending", "admin", False)]


# -- Succeeded, then failed --------------------------------------------------------

@pytest.mark.parametrize("event_type,status", [
    ("refund.failed", "failed"),
    ("refund.updated", "failed"),
    ("refund.updated", "canceled"),
])
def test_a_refund_that_fails_after_it_was_recorded_is_undone_and_flagged(
    shop, paid, outbox, event_type, status
):
    # pm_card_refundFail: the refund is created succeeded and fails later.
    # The order stayed "refunded" while the customer got nothing back.
    order_id = paid["order"]["id"]
    refund = shop.refund(paid["admin"], order_id).json()
    assert order_state(shop, order_id) == ("confirmed", "refunded")
    outbox.take()

    response = shop.webhook(event_type, _refund_object(
        paid["payment"], refund["id"], 1999, status, failure_reason="expired_or_canceled_card"))

    assert response.status_code == 200, response.text
    assert response.json()["requires_manual_review"] is True
    assert order_state(shop, order_id) == ("confirmed", "paid")
    assert payment_rows(shop, order_id) == [("succeeded", 1999, 0, "USD", False)]
    assert _ledger(shop, order_id) == [("re_", 1999, status, "admin", True)]
    # The charge can be refunded again. At Stripe a failed refund's amount
    # goes back to the balance and is refundable again; the mock provider
    # does not model refund failures, so its record is corrected here.
    StripePaymentProvider._intents_by_id[paid["payment"].payment_intent_id].amount_refunded = 0
    assert shop.refund(paid["admin"], order_id, amount="1.00").status_code == 200
    assert payment_rows(shop, order_id) == [("succeeded", 1999, 100, "USD", False)]


def test_a_failed_refund_stays_failed_whatever_arrives_after_it(shop, paid):
    order_id = paid["order"]["id"]
    refund = shop.refund(paid["admin"], order_id, amount="5.00").json()
    failed = _refund_object(paid["payment"], refund["id"], 500, "failed")
    assert shop.webhook("refund.failed", failed).status_code == 200

    for late in ("refund.updated", "refund.created"):
        response = shop.webhook(late, _refund_object(
            paid["payment"], refund["id"], 500, "succeeded"))
        assert response.status_code == 200

    assert payment_rows(shop, order_id) == [("succeeded", 1999, 0, "USD", False)]
    assert _ledger(shop, order_id) == [("re_", 500, "failed", "admin", True)]
    assert shop.webhook("refund.failed", failed).status_code == 200
    assert payment_rows(shop, order_id) == [("succeeded", 1999, 0, "USD", False)]


# -- Refunds made outside the app -------------------------------------------------

def test_a_refund_made_in_the_dashboard_is_recorded(shop, paid):
    # It was never seen: amount_refunded stayed 0 and the order "paid"
    # for money already returned.
    order_id = paid["order"]["id"]

    partial = shop.webhook("refund.created", _refund_object(
        paid["payment"], "re_dashboard_1", 700, "succeeded"))
    assert partial.status_code == 200, partial.text
    assert payment_rows(shop, order_id) == [("succeeded", 1999, 700, "USD", False)]
    assert order_state(shop, order_id) == ("confirmed", "paid")

    rest = shop.webhook("refund.created", _refund_object(
        paid["payment"], "re_dashboard_2", 1299, "succeeded"))
    assert rest.status_code == 200
    assert payment_rows(shop, order_id) == [("succeeded", 1999, 1999, "USD", False)]
    assert order_state(shop, order_id) == ("confirmed", "refunded")
    assert _ledger(shop, order_id) == [
        ("re_", 700, "succeeded", "provider", False),
        ("re_", 1299, "succeeded", "provider", False),
    ]


def test_our_own_refund_reported_by_its_webhook_is_counted_once(shop, paid):
    order_id = paid["order"]["id"]
    refund = shop.refund(paid["admin"], order_id, amount="5.00").json()

    for event_type in ("refund.created", "refund.updated", "charge.refunded"):
        obj = _refund_object(paid["payment"], refund["id"], 500, "succeeded")
        if event_type == "charge.refunded":
            obj = {"id": "ch_x", "object": "charge", "payment_intent": paid["payment"].payment_intent_id,
                   "amount": 1999, "amount_refunded": 500, "refunded": False, "currency": "usd",
                   "refunds": {"object": "list", "data": [obj]}}
        assert shop.webhook(event_type, obj).status_code == 200

    assert payment_rows(shop, order_id) == [("succeeded", 1999, 500, "USD", False)]
    assert _ledger(shop, order_id) == [("re_", 500, "succeeded", "admin", False)]


def test_a_refund_webhook_that_overtakes_the_refund_request_is_counted_once(
    shop, paid, monkeypatch
):
    # refund.created arrives while /refund is still waiting for the
    # provider's answer: the webhook records the refund first, and /refund
    # then finds it recorded instead of adding it again.
    order_id = paid["order"]["id"]
    real_refund = StripePaymentProvider.create_refund
    early = []

    def _webhook_first(self, **kwargs):
        refund = real_refund(self, **kwargs)
        early.append(shop.webhook("refund.created", _refund_object(
            paid["payment"], refund.id, refund.amount, "succeeded")))
        return refund

    monkeypatch.setattr(StripePaymentProvider, "create_refund", _webhook_first)

    response = shop.refund(paid["admin"], order_id)

    assert early[0].status_code == 200, early[0].text
    assert response.status_code == 200, response.text
    assert response.json()["payment_status"] == "refunded"
    assert payment_rows(shop, order_id) == [("succeeded", 1999, 1999, "USD", False)]
    assert _ledger(shop, order_id) == [("re_", 1999, "succeeded", "provider", False)]
    assert order_state(shop, order_id) == ("confirmed", "refunded")


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
    assert response.json() == {"status": "processed", "applied": False}
    assert payment_rows(shop, paid["order"]["id"]) == [("succeeded", 1999, 0, "USD", False)]


def test_a_refund_of_a_flagged_second_charge_leaves_the_order_alone(shop, paid):
    # A charge flagged for manual review (here: a success reported for an
    # attempt after another one paid the order) is refunded by whoever
    # reconciles it, typically in the Dashboard. Its refund is recorded on
    # that charge; the order, paid by the other one, stays paid.
    order_id = paid["order"]["id"]
    second = shop.payments(order_id)[0]

    flagged = Payment(order_id=order_id, provider="stripe", payment_intent_id="pi_second_charge",
                      idempotency_key=uuid.uuid4().hex, amount=1999, currency="USD",
                      status="succeeded", requires_manual_review=True)
    shop.db.add(flagged)
    shop.db.commit()

    response = shop.webhook("refund.created", _refund_object(flagged, "re_second", 1999, "succeeded"))

    assert response.status_code == 200
    assert payment_rows(shop, order_id) == [
        ("succeeded", 1999, 0, "USD", False), ("succeeded", 1999, 1999, "USD", True)]
    assert order_state(shop, order_id) == ("confirmed", "paid")
    assert second.amount_refunded == 0


# -- Odd reports ---------------------------------------------------------------------

def test_a_refund_first_seen_as_failed_is_recorded_but_not_counted(shop, paid):
    # refund.failed for a Dashboard refund whose refund.created never came
    # (or came later): kept for the record, no money counted as returned.
    order_id = paid["order"]["id"]

    response = shop.webhook("refund.failed", _refund_object(
        paid["payment"], "re_never_seen", 500, "failed", failure_reason="declined"))

    assert response.status_code == 200
    assert payment_rows(shop, order_id) == [("succeeded", 1999, 0, "USD", False)]
    assert _ledger(shop, order_id) == [("re_", 500, "failed", "provider", False)]
    assert order_state(shop, order_id) == ("confirmed", "paid")


def test_a_refund_event_with_an_expanded_payment_intent_is_matched(shop, paid):
    obj = _refund_object(paid["payment"], "re_expanded", 300, "succeeded")
    obj["payment_intent"] = {"id": paid["payment"].payment_intent_id, "object": "payment_intent"}

    shop.webhook("refund.created", obj)

    assert payment_rows(shop, paid["order"]["id"]) == [("succeeded", 1999, 300, "USD", False)]


@pytest.mark.parametrize("amount", [None, 0, -5, "500", True])
def test_an_unknown_refund_without_a_usable_amount_is_not_recorded(shop, paid, amount):
    obj = _refund_object(paid["payment"], "re_no_amount", amount, "succeeded")

    response = shop.webhook("refund.created", obj)

    assert response.status_code == 200
    assert payment_rows(shop, paid["order"]["id"]) == [("succeeded", 1999, 0, "USD", False)]
    assert _ledger(shop, paid["order"]["id"]) == []
