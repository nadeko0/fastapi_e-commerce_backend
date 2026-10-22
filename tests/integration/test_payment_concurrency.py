"""
Concurrent payment attempts for one order: the order row is locked while
its payment state is read and changed, and a second success for an order
that is already paid is flagged for manual review instead of applied again.

What SQLite can and cannot show. SQLite has no row locks and SQLAlchemy
leaves FOR UPDATE out of the SQL it sends there, so the lock itself is only
real on Postgres. These tests check two things instead: that every read of
the order on the payment paths is a locking read on Postgres (each ORM
statement is compiled with the Postgres dialect, which is exactly what
Postgres would be sent), and the state guard the lock protects - what
happens when the order's payment state changes between the guard and the
provider's answer, simulated by changing it from another session while the
provider call is in flight.

What remains possible on Postgres. The lock is released before the call to
the provider (no transaction stays open across an HTTP call). One attempt
retried is a replay (the Idempotency-Key is required), and a second attempt
with its own key is refused while the first is in flight
(test_payment_rules.py). Once the first has been in flight for longer than
PAYMENT_ATTEMPT_STALE_AFTER_MINUTES, a second one may start, and both can
then be charged: the second success is recorded and flagged for manual
review (a refund), never applied to the order a second time.
"""
import uuid

import pytest
from conftest import TestingSessionLocal, engine
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker
from test_checkout_session import CHECKOUT_SESSION_URL, _checkout_webhook_body
from test_payment import (
    ORDER_AMOUNT_CENTS,
    ORDER_CURRENCY_REPORTED,
    ORDERS_PREFIX,
    _age_attempts,
    _create_admin,
    _create_verified_buyer_with_order,
    _get_order,
    _get_payments,
    _pay_requiring_action,
    _payment_payload,
    _post_webhook,
    _sign,
    _webhook_body,
    _with_idempotency_key,
)

from app.api.deps import get_db as deps_get_db
from app.core.database import get_db as database_get_db
from app.main import app
from app.models.order import Order
from app.services.payment.stripe_provider import StripePaymentProvider


@pytest.fixture(autouse=True)
def _reset_stripe_mock_state():
    StripePaymentProvider.reset_mock_state()
    yield
    StripePaymentProvider.reset_mock_state()


@pytest.fixture(autouse=True)
def _stub_order_confirmation_email(monkeypatch):
    monkeypatch.setattr("app.api.v1.orders.send_order_confirmation_email", lambda *a, **k: True)
    yield


_ProductionLikeSession = sessionmaker(
    autocommit=False, autoflush=False, expire_on_commit=False, bind=engine
)


def _production_like_get_db():
    db = _ProductionLikeSession()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _sessions_configured_as_in_production(monkeypatch):
    # The app's sessions do not expire objects on commit
    # (app/core/database.py); the suite's default ones do, which reloads
    # the order after every commit and would hide both a stale read and an
    # unlocked one. Here the routes get sessions configured like the app's.
    monkeypatch.setitem(app.dependency_overrides, database_get_db, _production_like_get_db)
    monkeypatch.setitem(app.dependency_overrides, deps_get_db, _production_like_get_db)


@pytest.fixture
def status_emails(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.api.v1.orders.send_order_status_update_email",
        lambda email, order: sent.append(order.payment_status) or True,
    )
    return sent


@pytest.fixture
def postgres_sql():
    """Every ORM statement any session executes while the fixture is active,
    as Postgres would receive it."""
    rendered = []

    def _record(state):
        rendered.append(str(state.statement.compile(dialect=postgresql.dialect())))

    event.listen(Session, "do_orm_execute", _record)
    yield rendered
    event.remove(Session, "do_orm_execute", _record)


def _order_reads(statements):
    return [s for s in statements if s.lstrip().startswith("SELECT") and "FROM orders" in s]


def _mark_paid_from_another_session(order_id):
    # What a concurrent attempt (a webhook for another PaymentIntent or a
    # Checkout Session) commits while this request waits on the provider.
    db = TestingSessionLocal()
    try:
        order = db.get(Order, order_id)
        order.payment_status = "paid"
        order.status = "confirmed"
        db.commit()
    finally:
        db.close()


# -- The order row is locked on every payment path ---------------------------

def test_pay_reads_the_order_only_with_a_row_lock(client, db_session, postgres_sql):
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]
    postgres_sql.clear()

    response = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay", json=_payment_payload(order), headers=_with_idempotency_key(setup["headers"])
    )

    assert response.status_code == 200, response.text
    reads = _order_reads(postgres_sql)
    assert reads, "no read of the order was recorded"
    assert all("FOR UPDATE" in s for s in reads), reads


def test_checkout_session_reads_the_order_only_with_a_row_lock(client, db_session, postgres_sql):
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]
    postgres_sql.clear()

    response = client.post(CHECKOUT_SESSION_URL(order["id"]), headers=_with_idempotency_key(setup["headers"]))

    assert response.status_code == 200, response.text
    reads = _order_reads(postgres_sql)
    assert reads, "no read of the order was recorded"
    assert all("FOR UPDATE" in s for s in reads), reads


@pytest.mark.parametrize("event_type", ["payment_intent.succeeded", "payment_intent.payment_failed"])
def test_webhook_reads_the_order_only_with_a_row_lock(
    client, db_session, postgres_sql, event_type
):
    setup = _create_verified_buyer_with_order(client, db_session)
    intent_id = _pay_requiring_action(client, setup)
    postgres_sql.clear()

    body = _webhook_body(
        event_type, intent_id, amount=ORDER_AMOUNT_CENTS, currency=ORDER_CURRENCY_REPORTED
    )
    response = _post_webhook(client, body, _sign(body))

    assert response.status_code == 200
    reads = _order_reads(postgres_sql)
    assert reads, "no read of the order was recorded"
    assert all("FOR UPDATE" in s for s in reads), reads


@pytest.mark.parametrize("by", ["customer", "admin"])
def test_cancellation_reads_the_order_only_with_a_row_lock(client, db_session, postgres_sql, by):
    # Cancelling returns stock; two cancellations deciding on the same
    # "new" status would return it twice. The admin route joins the
    # shipping address (an outer join), so only the orders row is locked:
    # Postgres refuses FOR UPDATE on the nullable side of an outer join.
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]
    headers = setup["headers"] if by == "customer" else _create_admin(db_session)["headers"]
    postgres_sql.clear()

    if by == "customer":
        response = client.post(f"{ORDERS_PREFIX}/{order['id']}/cancel", headers=headers)
    else:
        response = client.put(
            f"{ORDERS_PREFIX}/{order['id']}/status", params={"status": "cancelled"}, headers=headers
        )

    assert response.status_code == 200, response.text
    reads = _order_reads(postgres_sql)
    assert reads, "no read of the order was recorded"
    assert all("FOR UPDATE OF orders" in s for s in reads), reads


def test_expiry_locks_each_order_and_skips_those_already_locked(
    client, db_session, postgres_sql, tasks_db
):
    from datetime import timedelta

    from app import tasks
    from app.core.clock import utc_now
    from app.core.config import settings

    setup = _create_verified_buyer_with_order(client, db_session)
    db_order = db_session.get(Order, setup["order"]["id"])
    db_order.created_at = utc_now() - timedelta(
        minutes=settings.UNPAID_ORDER_EXPIRE_MINUTES + 1
    )
    db_session.commit()
    postgres_sql.clear()

    assert tasks.cancel_expired_unpaid_orders() == 1

    # The whole-row query per candidate, not the id-only candidate list
    # (nothing to lock yet) or the reload of attributes the task's session
    # (tasks_db: the suite's sessions, which expire on commit) does after
    # each commit to address the email.
    full_rows = [s for s in _order_reads(postgres_sql) if "orders.status AS" in s]
    assert full_rows and all("FOR UPDATE SKIP LOCKED" in s for s in full_rows), full_rows


# -- A second success for a paid order is flagged, not applied again ---------

def test_webhook_success_for_an_order_another_attempt_paid_is_flagged(
    client, db_session, status_emails
):
    # The first attempt waited for 3D Secure; the customer paid with another
    # card instead; then the first one completed too. Two charges for one
    # order. It used to be applied as a fresh success (order "paid" again,
    # a second confirmation email) and nothing pointed at the double charge.
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]
    first_intent_id = _pay_requiring_action(client, setup)
    _age_attempts(db_session, order["id"])  # long enough not to block another
    paid = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay", json=_payment_payload(order), headers=_with_idempotency_key(setup["headers"])
    )
    assert paid.status_code == 200, paid.text
    assert status_emails == ["paid"]

    body = _webhook_body(
        "payment_intent.succeeded", first_intent_id,
        amount=ORDER_AMOUNT_CENTS, currency=ORDER_CURRENCY_REPORTED,
    )
    response = _post_webhook(client, body, _sign(body))

    assert response.status_code == 200
    assert response.json()["data"]["applied"] is False
    assert response.json()["data"]["requires_manual_review"] is True
    assert _get_order(db_session, order["id"]).payment_status == "paid"
    first = next(p for p in _get_payments(db_session, order["id"])
                 if p.payment_intent_id == first_intent_id)
    assert first.status == "succeeded"
    assert first.requires_manual_review is True
    assert status_emails == ["paid"]


def test_checkout_session_completed_after_pay_is_flagged(client, db_session, status_emails):
    # The F-04 scenario the other way round: the abandoned Checkout Session
    # is completed after /pay already took the money.
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]
    session = client.post(CHECKOUT_SESSION_URL(order["id"]), headers=_with_idempotency_key(setup["headers"])).json()["data"]
    _age_attempts(db_session, order["id"])  # long enough not to block /pay
    paid = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay", json=_payment_payload(order), headers=_with_idempotency_key(setup["headers"])
    )
    assert paid.status_code == 200, paid.text

    body = _checkout_webhook_body(
        "checkout.session.completed", session["checkout_session_id"],
        payment_intent_id=f"pi_{uuid.uuid4().hex[:24]}",
    )
    response = _post_webhook(client, body, _sign(body))

    assert response.json()["data"]["requires_manual_review"] is True
    assert _get_order(db_session, order["id"]).payment_status == "paid"
    assert status_emails == ["paid"]


def test_pay_whose_order_is_paid_while_the_provider_call_is_in_flight_is_flagged(
    client, db_session, monkeypatch, status_emails
):
    # The guard saw an unpaid order; by the time the provider confirmed this
    # charge, another attempt had paid it. This attempt is recorded as the
    # success it is and flagged; the order is not applied a second time.
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]
    real_confirm = StripePaymentProvider.confirm_payment_intent

    def _confirm_after_another_attempt_paid(self, payment_intent_id, **kwargs):
        _mark_paid_from_another_session(order["id"])
        return real_confirm(self, payment_intent_id, **kwargs)

    monkeypatch.setattr(
        StripePaymentProvider, "confirm_payment_intent", _confirm_after_another_attempt_paid
    )

    response = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay", json=_payment_payload(order), headers=_with_idempotency_key(setup["headers"])
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "paid"
    payments = _get_payments(db_session, order["id"])
    assert [(p.status, p.requires_manual_review) for p in payments] == [("succeeded", True)]
    assert status_emails == []


def test_decline_whose_order_is_paid_while_the_provider_call_is_in_flight_keeps_it_paid(
    client, db_session, monkeypatch
):
    from app.services.payment.exceptions import CardError

    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]

    def _decline_after_another_attempt_paid(self, payment_intent_id, **kwargs):
        _mark_paid_from_another_session(order["id"])
        raise CardError("Your card was declined.", code="card_declined")

    monkeypatch.setattr(
        StripePaymentProvider, "confirm_payment_intent", _decline_after_another_attempt_paid
    )

    response = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay", json=_payment_payload(order), headers=_with_idempotency_key(setup["headers"])
    )

    assert response.status_code == 402
    assert _get_order(db_session, order["id"]).payment_status == "paid"
    assert [p.status for p in _get_payments(db_session, order["id"])] == ["failed"]
