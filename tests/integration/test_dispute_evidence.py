"""
What Stripe reports about a dispute's evidence (Dispute.evidence_details),
stored with the dispute by the charge.dispute.* handler
(app/services/stripe_events.py, _apply_dispute_event) so the admin can see
whether evidence was submitted and by when it is due.
"""
import uuid
from datetime import UTC, datetime

import pytest
from payment_support import Shop, reset_provider

from app.models.payment_ledger import PaymentDispute

DUE_BY = 1_795_000_000  # 2026-11-18 11:06:40 UTC


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def shop(client, db_session, monkeypatch):
    return Shop(client, db_session)


@pytest.fixture
def payment(shop):
    product = shop.product(price="19.99", stock=4)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    assert shop.pay(buyer, order["id"]).status_code == 200
    return shop.payments(order["id"])[0]


def _dispute(payment, status, evidence_details=None):
    obj = {"id": "du_evidence_1", "object": "dispute", "amount": payment.amount,
           "currency": payment.currency.lower(), "charge": f"ch_{uuid.uuid4().hex[:24]}",
           "payment_intent": payment.payment_intent_id, "status": status,
           "reason": "fraudulent"}
    if evidence_details is not None:
        obj["evidence_details"] = evidence_details
    return obj


def _evidence(shop):
    shop.db.expire_all()
    d = shop.db.query(PaymentDispute).one()
    return (d.evidence_due_by, d.evidence_has_evidence, d.evidence_past_due,
            d.evidence_submission_count, d.evidence_submission_method)


def test_a_new_dispute_stores_its_evidence_deadline_and_state(shop, payment):
    shop.webhook("charge.dispute.created", _dispute(payment, "needs_response", {
        "due_by": DUE_BY, "has_evidence": False, "past_due": False,
        "submission_count": 0, "submission_method": "not_submitted"}))

    assert _evidence(shop) == (
        datetime.fromtimestamp(DUE_BY, UTC), False, False, 0, "not_submitted")


def test_submitted_evidence_updates_the_stored_state(shop, payment):
    shop.webhook("charge.dispute.created", _dispute(payment, "needs_response", {
        "due_by": DUE_BY, "has_evidence": False, "past_due": False,
        "submission_count": 0, "submission_method": "not_submitted"}))

    shop.webhook("charge.dispute.updated", _dispute(payment, "under_review", {
        "due_by": DUE_BY, "has_evidence": True, "past_due": False,
        "submission_count": 1, "submission_method": "manual"}))

    assert _evidence(shop) == (
        datetime.fromtimestamp(DUE_BY, UTC), True, False, 1, "manual")


def test_an_event_without_evidence_details_keeps_what_was_stored(shop, payment):
    shop.webhook("charge.dispute.created", _dispute(payment, "needs_response", {
        "due_by": DUE_BY, "has_evidence": True, "past_due": False,
        "submission_count": 1, "submission_method": "manual"}))

    shop.webhook("charge.dispute.updated", _dispute(payment, "under_review"))

    assert _evidence(shop) == (
        datetime.fromtimestamp(DUE_BY, UTC), True, False, 1, "manual")


def test_a_dispute_never_reported_with_evidence_details_has_none(shop, payment):
    shop.webhook("charge.dispute.created", _dispute(payment, "needs_response"))

    assert _evidence(shop) == (None, None, None, None, None)


@pytest.mark.parametrize("details", [
    {"due_by": None, "has_evidence": None, "past_due": None,
     "submission_count": None, "submission_method": None},
    {"due_by": True, "has_evidence": 1, "past_due": "no",
     "submission_count": False, "submission_method": 3},
    {"due_by": "1795000000", "has_evidence": "true", "past_due": 0,
     "submission_count": 1.0, "submission_method": ["manual"]},
    {},
], ids=["nulls", "bools-and-ints-swapped", "strings-and-floats", "empty"])
def test_a_field_of_the_wrong_type_is_stored_as_not_reported(shop, payment, details):
    shop.webhook("charge.dispute.created", _dispute(payment, "needs_response", details))

    assert _evidence(shop) == (None, None, None, None, None)


def test_evidence_details_that_are_not_an_object_change_nothing(shop, payment):
    shop.webhook("charge.dispute.created", _dispute(payment, "needs_response", {
        "due_by": DUE_BY, "has_evidence": False, "past_due": False,
        "submission_count": 0, "submission_method": "not_submitted"}))

    shop.webhook("charge.dispute.updated", _dispute(payment, "needs_response", "garbage"))

    assert _evidence(shop) == (
        datetime.fromtimestamp(DUE_BY, UTC), False, False, 0, "not_submitted")


def test_a_closed_dispute_keeps_its_last_evidence_state(shop, payment):
    shop.webhook("charge.dispute.created", _dispute(payment, "needs_response", {
        "due_by": DUE_BY, "has_evidence": True, "past_due": False,
        "submission_count": 1, "submission_method": "manual"}))
    shop.webhook("charge.dispute.closed", _dispute(payment, "won", {
        "due_by": DUE_BY, "has_evidence": True, "past_due": False,
        "submission_count": 1, "submission_method": "manual"}))

    # A late event for a closed dispute is ignored, its evidence included.
    shop.webhook("charge.dispute.updated", _dispute(payment, "under_review", {
        "due_by": None, "has_evidence": False, "past_due": True,
        "submission_count": 0, "submission_method": "not_submitted"}))

    assert _evidence(shop) == (
        datetime.fromtimestamp(DUE_BY, UTC), True, False, 1, "manual")


@pytest.mark.parametrize("status", ["appeal_needs_response", "appeal_under_review"])
def test_an_appealed_dispute_is_still_open_and_holds_the_order(shop, payment, status):
    # Stripe's appeal stage (under_review -> appeal_needs_response ->
    # appeal_under_review -> won or lost): the dispute is not over, so the
    # charge stays unrefundable and the appeal deadline is stored.
    shop.webhook("charge.dispute.created", _dispute(payment, "needs_response"))
    shop.webhook("charge.dispute.updated", _dispute(payment, status, {
        "due_by": DUE_BY, "has_evidence": status == "appeal_under_review", "past_due": False,
        "submission_count": 1, "submission_method": "manual"}))

    shop.db.expire_all()
    dispute = shop.db.query(PaymentDispute).one()
    assert (dispute.status, dispute.closed_at) == (status, None)
    refund = shop.refund(shop.admin(), payment.order_id)
    assert refund.status_code == 409, refund.text
    assert "disput" in refund.json()["detail"]
