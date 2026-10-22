"""
Tests written from mutation testing of the payment layer (mutmut): each one
pins a behaviour of the webhook helpers in app/api/v1/orders.py that a mutant
changed without any existing test noticing - the exact answer the webhook
route sends for a refund or dispute event (a flag set or not, a key renamed),
how an event's odd fields (a non-integer amount, a reason that is not text) are
stored, what the manual-review log lines say (they are what an operator is
alerted on), and which attempts still hold back an event that matched nothing.
"""
import logging
import uuid
from datetime import datetime, timedelta

import pytest
from payment_support import (
    Shop,
    order_state,
    payment_rows,
    reset_provider,
    use_production_sessions,
)

from app.api.v1.orders import _attempt_still_storing_its_id, _object_id
from app.core.config import settings
from app.models.payment import Payment
from app.models.payment_ledger import PaymentDispute, PaymentRefund

ORDERS_LOGGER = "app.api.v1.orders"
PROCESSED = {"status": "processed", "applied": True}
PROCESSED_FLAGGED = {"status": "processed", "applied": True, "requires_manual_review": True}


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


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


def _refund_object(payment, refund_id, amount, status, failure_reason=None):
    return {"id": refund_id, "object": "refund", "amount": amount,
            "currency": payment.currency.lower(), "payment_intent": payment.payment_intent_id,
            "charge": f"ch_{uuid.uuid4().hex[:24]}", "status": status,
            "failure_reason": failure_reason, "metadata": {}}


def _dispute_object(payment, status, **overrides):
    obj = {"id": "du_mut_1", "object": "dispute", "amount": payment.amount,
           "currency": payment.currency.lower(), "charge": f"ch_{uuid.uuid4().hex[:24]}",
           "payment_intent": payment.payment_intent_id, "status": status,
           "reason": "fraudulent"}
    obj.update(overrides)
    return obj


def _ledger(shop, payment):
    shop.db.expire_all()
    return [(r.refund_id[:3], r.amount, r.status, r.source, r.requires_manual_review)
            for r in shop.db.query(PaymentRefund).filter(PaymentRefund.payment_id == payment.id)
            .order_by(PaymentRefund.id)]


def _messages(caplog):
    return [r.getMessage() for r in caplog.records if r.name == ORDERS_LOGGER]


# -- Refund events: what the webhook answers ---------------------------------------

def test_a_refund_completing_is_answered_processed_and_not_flagged(shop, paid, monkeypatch):
    from app.services.payment.stripe_provider import StripePaymentProvider
    from app.services.payment.types import RefundStatus

    real_refund = StripePaymentProvider.create_refund

    def pending_refund(self, **kwargs):
        refund = real_refund(self, **kwargs)
        refund.status = RefundStatus.PENDING
        return refund

    monkeypatch.setattr(StripePaymentProvider, "create_refund", pending_refund)
    refund_id = shop.refund(paid["admin"], paid["order"]["id"], amount="5.00").json()["data"]["id"]

    response = shop.webhook(
        "refund.updated", _refund_object(paid["payment"], refund_id, 500, "succeeded"))

    assert response.status_code == 200, response.text
    assert response.json()["data"] == PROCESSED


def test_a_refund_failing_after_it_was_recorded_is_answered_flagged(shop, paid):
    refund_id = shop.refund(paid["admin"], paid["order"]["id"]).json()["data"]["id"]

    response = shop.webhook("refund.failed", _refund_object(
        paid["payment"], refund_id, 1999, "failed", failure_reason="expired_or_canceled_card"))

    assert response.json()["data"] == PROCESSED_FLAGGED


def test_a_failed_refund_is_final_and_a_later_event_for_it_is_not_flagged(shop, paid):
    refund_id = shop.refund(paid["admin"], paid["order"]["id"]).json()["data"]["id"]
    shop.webhook("refund.failed", _refund_object(paid["payment"], refund_id, 1999, "failed"))

    response = shop.webhook(
        "refund.updated", _refund_object(paid["payment"], refund_id, 1999, "succeeded"))

    assert response.json()["data"] == PROCESSED
    assert _ledger(shop, paid["payment"]) == [("re_", 1999, "failed", "admin", True)]


def test_a_refund_event_without_a_status_is_not_flagged(shop, paid):
    refund_id = shop.refund(paid["admin"], paid["order"]["id"]).json()["data"]["id"]

    response = shop.webhook(
        "refund.updated", _refund_object(paid["payment"], refund_id, 1999, None))

    assert response.json()["data"] == PROCESSED


def test_a_refund_made_outside_the_app_is_recorded_even_for_one_cent(shop, paid):
    response = shop.webhook(
        "refund.created", _refund_object(paid["payment"], "re_dashboard_1", 1, "succeeded"))

    assert response.json()["data"] == PROCESSED
    assert _ledger(shop, paid["payment"]) == [("re_", 1, "succeeded", "provider", False)]
    assert payment_rows(shop, paid["order"]["id"]) == [("succeeded", 1999, 1, "USD", False)]


def test_a_refund_of_nothing_made_outside_the_app_is_not_recorded(shop, paid):
    for amount in (0, True, "5", None):
        response = shop.webhook(
            "refund.created", _refund_object(paid["payment"], "re_dashboard_2", amount, "succeeded"))
        assert response.json()["data"] == PROCESSED
    assert _ledger(shop, paid["payment"]) == []


def test_a_failure_reason_that_is_not_text_is_not_stored(shop, paid):
    refund_id = shop.refund(paid["admin"], paid["order"]["id"]).json()["data"]["id"]

    shop.webhook("refund.failed", _refund_object(
        paid["payment"], refund_id, 1999, "failed", failure_reason=123))

    shop.db.expire_all()
    assert shop.db.query(PaymentRefund).one().failure_reason is None


def test_a_charge_refunded_event_applies_the_refunds_it_lists(shop, paid):
    charge = {"id": "ch_1", "object": "charge", "payment_intent": paid["payment"].payment_intent_id,
              "refunds": {"data": ["not a refund", _refund_object(
                  paid["payment"], "re_listed_1", 700, "succeeded")]}}

    response = shop.webhook("charge.refunded", charge)

    assert response.json()["data"] == PROCESSED
    assert _ledger(shop, paid["payment"]) == [("re_", 700, "succeeded", "provider", False)]


@pytest.mark.parametrize("refunds", [None, "refunds", {}, {"data": None}, {"data": []},
                                     {"data": ["not a refund"]}])
def test_a_charge_refunded_event_listing_no_refund_applies_nothing(shop, paid, refunds):
    charge = {"id": "ch_1", "object": "charge", "payment_intent": paid["payment"].payment_intent_id}
    if refunds is not None:
        charge["refunds"] = refunds

    response = shop.webhook("charge.refunded", charge)

    assert response.json()["data"] == {"status": "processed", "applied": False}
    assert _ledger(shop, paid["payment"]) == []


def test_a_refund_event_log_line_names_the_refund_the_payment_and_the_order(shop, paid, caplog):
    refund_id = shop.refund(paid["admin"], paid["order"]["id"]).json()["data"]["id"]

    with caplog.at_level(logging.WARNING, logger=ORDERS_LOGGER):
        shop.webhook("refund.failed", _refund_object(
            paid["payment"], refund_id, 1999, "failed", failure_reason="lost_or_stolen_card"))

    assert _messages(caplog) == [
        f"Refund {refund_id} of payment {paid['payment'].id} (order {paid['order']['id']}) is "
        "failed after it was recorded (lost_or_stolen_card): the customer was not refunded, "
        "needs manual review"
    ]


# -- Dispute events -----------------------------------------------------------------

def _stored_dispute(shop):
    shop.db.expire_all()
    dispute = shop.db.query(PaymentDispute).one()
    return dispute.amount, dispute.reason, dispute.status


def test_a_dispute_is_stored_with_its_amount_and_reason(shop, paid):
    shop.webhook("charge.dispute.created", _dispute_object(paid["payment"], "needs_response",
                                                           amount=1500))

    assert _stored_dispute(shop) == (1500, "fraudulent", "needs_response")


@pytest.mark.parametrize("amount", ["1500", True, 15.5, None])
def test_a_dispute_amount_that_is_not_an_integer_is_stored_as_zero(shop, paid, amount):
    shop.webhook("charge.dispute.created", _dispute_object(paid["payment"], "needs_response",
                                                           amount=amount))

    assert _stored_dispute(shop)[0] == 0


def test_a_dispute_without_an_amount_is_stored_as_zero(shop, paid):
    obj = _dispute_object(paid["payment"], "needs_response")
    del obj["amount"]

    shop.webhook("charge.dispute.created", obj)

    assert _stored_dispute(shop)[0] == 0


@pytest.mark.parametrize("reason", [123, None])
def test_a_dispute_reason_that_is_not_text_is_not_stored(shop, paid, reason):
    shop.webhook("charge.dispute.created", _dispute_object(paid["payment"], "needs_response",
                                                           reason=reason))

    assert _stored_dispute(shop)[1] is None


def test_an_open_dispute_log_line_says_the_order_is_on_hold(shop, paid, caplog):
    with caplog.at_level(logging.WARNING, logger=ORDERS_LOGGER):
        shop.webhook("charge.dispute.created", _dispute_object(paid["payment"], "needs_response"))

    assert _messages(caplog) == [
        f"Dispute du_mut_1 of payment {paid['payment'].id} (order {paid['order']['id']}) is "
        "needs_response: order on hold, needs review"
    ]


def test_a_closed_dispute_log_line_does_not(shop, paid, caplog):
    with caplog.at_level(logging.WARNING, logger=ORDERS_LOGGER):
        shop.webhook("charge.dispute.closed", _dispute_object(paid["payment"], "won"))

    assert _messages(caplog) == [
        f"Dispute du_mut_1 of payment {paid['payment'].id} (order {paid['order']['id']}) is won"
    ]
    assert order_state(shop, paid["order"]["id"]) == ("confirmed", "paid")


# -- _object_id ---------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("pi_1", "pi_1"),
    ({"id": "pi_1"}, "pi_1"),
    ("", None),
    ({"id": ""}, None),
    (123, None),
    ({"id": 123}, None),
    ({}, None),
    (None, None),
    (["pi_1"], None),
])
def test_object_id_is_a_non_empty_string_or_the_id_of_an_object(value, expected):
    assert _object_id(value) == expected


# -- An event that matched no payment: is its attempt still storing its id? -----------

def _attempt(db, order_id, **overrides):
    values = dict(order_id=order_id, payment_intent_id=f"pending-{uuid.uuid4().hex[:12]}",
                  amount=1999, currency="USD", status="processing", checkout_session_id=None)
    values.update(overrides)
    payment = Payment(**values)
    db.add(payment)
    db.commit()
    return payment


def _event(order_id):
    return {"id": "pi_1", "metadata": {"order_id": str(order_id)}}


def test_an_attempt_in_flight_holds_back_an_event_naming_its_order(db_session):
    _attempt(db_session, 41)

    assert _attempt_still_storing_its_id(db_session, _event(41)) is True


def test_an_attempt_of_another_order_does_not(db_session):
    _attempt(db_session, 41)

    assert _attempt_still_storing_its_id(db_session, _event(42)) is False


@pytest.mark.parametrize("overrides", [
    {"status": "failed"},
    {"status": "succeeded"},
    {"checkout_session_id": "cs_1"},
    {"payment_intent_id": "pi_real_one"},
], ids=["failed", "succeeded", "has-session-id", "has-intent-id"])
def test_an_attempt_that_is_not_waiting_for_its_ids_does_not(db_session, overrides):
    _attempt(db_session, 41, **overrides)

    assert _attempt_still_storing_its_id(db_session, _event(41)) is False


def test_an_attempt_in_flight_for_longer_than_the_stale_limit_does_not(db_session):
    stale = datetime.utcnow() - timedelta(minutes=settings.PAYMENT_ATTEMPT_STALE_AFTER_MINUTES + 1)
    _attempt(db_session, 41, created_at=stale)

    assert _attempt_still_storing_its_id(db_session, _event(41)) is False


def test_an_attempt_exactly_at_the_stale_limit_does_not(db_session, monkeypatch):
    now = datetime(2030, 1, 1, 12, 0, 0)

    class Frozen(datetime):
        @classmethod
        def utcnow(cls):
            return now

    monkeypatch.setattr("app.api.v1.orders.datetime", Frozen)
    limit = now - timedelta(minutes=settings.PAYMENT_ATTEMPT_STALE_AFTER_MINUTES)
    _attempt(db_session, 41, created_at=limit)
    assert _attempt_still_storing_its_id(db_session, _event(41)) is False

    _attempt(db_session, 42, created_at=limit + timedelta(seconds=1))
    assert _attempt_still_storing_its_id(db_session, _event(42)) is True


@pytest.mark.parametrize("order_id,expected", [
    (2 ** 31 - 1, True),
    (2 ** 31, False),
    (2 ** 32, False),
    (100, True),
])
def test_only_order_ids_a_database_integer_can_hold_are_looked_up(db_session, order_id, expected):
    _attempt(db_session, order_id)

    assert _attempt_still_storing_its_id(db_session, _event(order_id)) is expected


@pytest.mark.parametrize("metadata", [None, "order", [], {}, {"order_id": None},
                                      {"order_id": "seven"}, {"order_id": "0"},
                                      {"order_id": "-1"}])
def test_an_event_that_does_not_name_an_order_is_not_held_back(db_session, metadata):
    _attempt(db_session, 41)
    event = {"id": "pi_1"}
    if metadata is not None:
        event["metadata"] = metadata

    assert _attempt_still_storing_its_id(db_session, event) is False
