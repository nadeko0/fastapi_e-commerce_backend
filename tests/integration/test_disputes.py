"""
Disputes (chargebacks) of a charge, reported by Stripe as charge.dispute.*
events (app/api/v1/orders.py). Follows WooCommerce's Stripe gateway
semantics, mapped onto this app's order model:

- charge.dispute.created puts the order on hold for review: its status and
  payment_status are left as they are, but it cannot be moved on
  (fulfilled) and its charge cannot be refunded while the dispute is open
  - refunding a disputed charge returns the money twice (Stripe: the
  dispute's amount is already withdrawn from the balance).
- charge.dispute.closed records the outcome: lost means the money went
  back to the customer through their bank, so the order becomes refunded;
  won and warning_closed (an inquiry that never became a chargeback) lift
  the hold. A closed dispute is final: an event for it that arrives late,
  out of order, does not reopen it.
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

from app.models.payment_ledger import PaymentDispute


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
    return {"order": order, "buyer": buyer, "admin": shop.admin(), "product": product,
            "payment": shop.payments(order["id"])[0]}


def _dispute(payment, status, dispute_id="du_test_1", amount=None, reason="fraudulent"):
    return {"id": dispute_id, "object": "dispute",
            "amount": payment.amount if amount is None else amount,
            "currency": payment.currency.lower(), "charge": f"ch_{uuid.uuid4().hex[:24]}",
            "payment_intent": payment.payment_intent_id, "status": status, "reason": reason}


def _disputes(shop):
    shop.db.expire_all()
    return [(d.dispute_id, d.status, d.closed_at is not None)
            for d in shop.db.query(PaymentDispute).order_by(PaymentDispute.id)]


def test_a_dispute_puts_the_order_on_hold_without_changing_it(shop, paid, outbox):
    order_id = paid["order"]["id"]
    outbox.take()

    response = shop.webhook("charge.dispute.created", _dispute(paid["payment"], "needs_response"))

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "status": "processed", "applied": True, "requires_manual_review": True}
    assert order_state(shop, order_id) == ("confirmed", "paid")
    assert _disputes(shop) == [("du_test_1", "needs_response", False)]
    # Not refunded: the dispute already took the money back from us.
    refund = shop.refund(paid["admin"], order_id)
    assert refund.status_code == 409, refund.text
    assert "disput" in refund.json()["detail"]
    assert payment_rows(shop, order_id) == [("succeeded", 1999, 0, "USD", False)]
    # Not fulfilled while on hold; cancelling (and restocking) is allowed.
    moved = shop.set_status(paid["admin"], order_id, "processing")
    assert moved.status_code == 409, moved.text
    assert order_state(shop, order_id) == ("confirmed", "paid")
    assert outbox.take() == []


@pytest.mark.parametrize("outcome", ["won", "warning_closed"])
def test_a_dispute_closed_in_our_favour_lifts_the_hold(shop, paid, outcome):
    order_id = paid["order"]["id"]
    shop.webhook("charge.dispute.created", _dispute(paid["payment"], "warning_needs_response"))
    shop.webhook("charge.dispute.updated", _dispute(paid["payment"], "under_review"))
    assert _disputes(shop) == [("du_test_1", "under_review", False)]

    response = shop.webhook("charge.dispute.closed", _dispute(paid["payment"], outcome))

    assert response.status_code == 200
    assert response.json()["data"] == {"status": "processed", "applied": True}
    assert order_state(shop, order_id) == ("confirmed", "paid")
    assert _disputes(shop) == [("du_test_1", outcome, True)]
    assert shop.set_status(paid["admin"], order_id, "processing").status_code == 200
    assert shop.refund(paid["admin"], order_id, amount="1.00").status_code == 200


def test_a_lost_dispute_refunds_the_order(shop, paid, outbox):
    order_id = paid["order"]["id"]
    shop.webhook("charge.dispute.created", _dispute(paid["payment"], "needs_response"))
    outbox.take()

    response = shop.webhook("charge.dispute.closed", _dispute(paid["payment"], "lost"))

    assert response.status_code == 200
    assert order_state(shop, order_id) == ("confirmed", "refunded")
    # Not a refund of ours: amount_refunded counts refunds only.
    assert payment_rows(shop, order_id) == [("succeeded", 1999, 0, "USD", False)]
    assert _disputes(shop) == [("du_test_1", "lost", True)]
    # Nothing more to refund, and the order cannot be paid again.
    assert shop.refund(paid["admin"], order_id).status_code == 400
    assert shop.pay(paid["buyer"], order_id).status_code == 400
    # Stock is not returned: the goods may well have been shipped.
    assert shop.stock(paid["product"]) == 3


def test_a_closed_dispute_is_not_reopened_by_a_late_event(shop, paid):
    order_id = paid["order"]["id"]
    # closed overtakes created: Stripe does not deliver events in order.
    shop.webhook("charge.dispute.closed", _dispute(paid["payment"], "won"))

    for event_type, status in (("charge.dispute.created", "needs_response"),
                               ("charge.dispute.updated", "under_review"),
                               ("charge.dispute.funds_withdrawn", "needs_response")):
        response = shop.webhook(event_type, _dispute(paid["payment"], status))
        assert response.status_code == 200
        assert response.json()["data"] == {"status": "processed", "applied": False}

    assert _disputes(shop) == [("du_test_1", "won", True)]
    assert order_state(shop, order_id) == ("confirmed", "paid")
    assert shop.set_status(paid["admin"], order_id, "processing").status_code == 200


def test_a_lost_dispute_is_not_undone_by_a_late_event(shop, paid):
    order_id = paid["order"]["id"]
    shop.webhook("charge.dispute.closed", _dispute(paid["payment"], "lost"))
    shop.webhook("charge.dispute.funds_reinstated", _dispute(paid["payment"], "won"))

    assert order_state(shop, order_id) == ("confirmed", "refunded")
    assert _disputes(shop) == [("du_test_1", "lost", True)]


def test_a_dispute_on_a_partially_refunded_charge_still_holds_the_rest(shop, paid):
    order_id = paid["order"]["id"]
    assert shop.refund(paid["admin"], order_id, amount="5.00").status_code == 200
    shop.webhook("charge.dispute.created", _dispute(paid["payment"], "needs_response", amount=1499))

    assert shop.refund(paid["admin"], order_id, amount="1.00").status_code == 409
    shop.webhook("charge.dispute.closed", _dispute(paid["payment"], "lost", amount=1499))
    assert order_state(shop, order_id) == ("confirmed", "refunded")
    assert payment_rows(shop, order_id) == [("succeeded", 1999, 500, "USD", False)]


def test_an_admin_may_cancel_an_order_on_hold(shop, paid):
    order_id = paid["order"]["id"]
    shop.webhook("charge.dispute.created", _dispute(paid["payment"], "needs_response"))

    response = shop.set_status(paid["admin"], order_id, "cancelled")

    assert response.status_code == 200, response.text
    assert order_state(shop, order_id) == ("cancelled", "paid")
    assert shop.stock(paid["product"]) == 4


@pytest.mark.parametrize("obj", [
    {"id": "du_x", "object": "dispute", "amount": 100, "currency": "usd",
     "payment_intent": "pi_not_ours", "status": "needs_response"},
    {"id": "du_x", "object": "dispute", "amount": 100, "currency": "usd",
     "payment_intent": None, "status": "needs_response"},
])
def test_a_dispute_of_a_charge_this_app_did_not_make_is_acknowledged(shop, paid, obj):
    response = shop.webhook("charge.dispute.created", obj)

    assert response.status_code == 200
    assert response.json()["data"] == {"status": "processed", "applied": False}
    assert _disputes(shop) == []


def test_a_dispute_event_without_an_id_or_status_changes_nothing(shop, paid):
    obj = _dispute(paid["payment"], None)

    response = shop.webhook("charge.dispute.created", obj)

    assert response.json()["data"] == {"status": "processed", "applied": False}
    assert _disputes(shop) == []
    assert shop.refund(paid["admin"], paid["order"]["id"], amount="1.00").status_code == 200
