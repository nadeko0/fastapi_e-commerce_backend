"""
Stripe events are stored by the webhook route and applied afterwards by the
process_stripe_event Celery task (app/services/stripe_events.py, app/tasks.py).

What these tests pin down:
- the route answers 200 once the event is stored and its task published,
  before anything is applied; a duplicate delivery changes nothing; a bad
  signature stores nothing; a store that fails answers 5xx (Stripe retries),
  a broker that is down does not (the sweeper finds the event);
- the task applies an event once, in the same transaction as the state it
  changes, whatever the order events arrive in and however many workers
  get the same message;
- an event that overtook its payment attempt, or whose processing fails, is
  retried with a backoff, and is "failed" when the retries run out.

The suite has no broker: stripe_event_support records what would be
published and runs the task body when a test asks for it.
"""
import json
import logging
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from payment_support import (
    PAY_3DS,
    Outbox,
    Shop,
    order_state,
    payment_rows,
    reset_provider,
)
from sqlalchemy import event as sa_event
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from stripe_event_support import published_ids, retries, run_event

import app.services.stripe_events as stripe_events
from app import tasks
from app.core.clock import utc_now
from app.core.config import settings
from app.main import app
from app.models.payment import WebhookEvent
from app.services.payment.stripe_provider import StripePaymentProvider


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


class _Countdowns:
    """The countdowns the task asked for since the test began; compares as a
    list."""

    def __init__(self):
        self.start = len(retries)

    def _asked(self):
        return retries[self.start:]

    def __eq__(self, other):
        return self._asked() == other

    def __repr__(self):
        return repr(self._asked())

    def __iter__(self):
        return iter(self._asked())


@pytest.fixture
def retries_seen():
    return _Countdowns()


def _awaiting_3ds(shop):
    product = shop.product(price="19.99", stock=5)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    assert shop.pay(buyer, order["id"], token=PAY_3DS).status_code == 200
    return buyer, order, shop.payments(order["id"])[0]


def _succeeded_object(payment):
    return {"id": payment.payment_intent_id, "object": "payment_intent",
            "amount": payment.amount, "amount_received": payment.amount,
            "currency": payment.currency.lower(),
            "metadata": {"order_id": str(payment.order_id)}}


def _failed_object(payment):
    return {"id": payment.payment_intent_id, "object": "payment_intent",
            "amount": payment.amount, "currency": payment.currency.lower(),
            "metadata": {"order_id": str(payment.order_id)}}


def _event_row(shop, event_id):
    shop.db.expire_all()
    return shop.db.query(WebhookEvent).filter(WebhookEvent.event_id == event_id).one_or_none()


def _new_id():
    return f"evt_{uuid.uuid4().hex[:24]}"


def _backdate(shop, event_id, minutes):
    row = _event_row(shop, event_id)
    row.updated_at = utc_now() - timedelta(minutes=minutes)
    shop.db.commit()


# -- The route: store, publish, answer ------------------------------------------

def test_the_route_stores_the_event_and_answers_before_applying_it(shop, outbox):
    buyer, order, payment = _awaiting_3ds(shop)
    outbox.take()
    event_id = _new_id()

    response = shop.webhook_raw("payment_intent.succeeded", _succeeded_object(payment),
                                event_id=event_id)

    assert response.status_code == 200
    assert response.json() == {"status": "received"}
    # Stored, with what the worker needs, and handed to the broker...
    row = _event_row(shop, event_id)
    assert (row.status, row.attempts, row.last_error, row.processed_at) == (
        "received", 0, None, None)
    assert row.event_type == "payment_intent.succeeded"
    stored = json.loads(row.payload)
    assert stored["id"] == event_id and stored["type"] == "payment_intent.succeeded"
    assert stored["data"]["object"]["id"] == payment.payment_intent_id
    assert published_ids().count(event_id) == 1
    # ...and not applied yet.
    assert order_state(shop, order["id"]) == ("new", "pending")
    assert outbox.take() == []


def test_the_task_applies_the_stored_event_and_marks_it_processed(shop, outbox):
    buyer, order, payment = _awaiting_3ds(shop)
    outbox.take()
    event_id = _new_id()
    shop.webhook_raw("payment_intent.succeeded", _succeeded_object(payment), event_id=event_id)

    result = run_event(event_id)

    assert result == {"status": "processed", "applied": True}
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    row = _event_row(shop, event_id)
    assert row.status == "processed" and row.attempts == 0 and row.last_error is None
    assert row.processed_at is not None
    assert [(e[0], e[4]) for e in outbox.take()] == [("status_update", "paid")]


def test_a_duplicate_delivery_is_answered_and_changes_nothing(shop, outbox):
    buyer, order, payment = _awaiting_3ds(shop)
    outbox.take()
    event_id = _new_id()
    obj = _succeeded_object(payment)

    first = shop.webhook_raw("payment_intent.succeeded", obj, event_id=event_id)
    second = shop.webhook_raw("payment_intent.succeeded", obj, event_id=event_id)

    assert first.json() == {"status": "received"}
    assert second.status_code == 200
    assert second.json() == {"status": "already_processed"}
    assert shop.db.query(WebhookEvent).filter(WebhookEvent.event_id == event_id).count() == 1
    assert published_ids().count(event_id) == 1

    run_event(event_id)
    third = shop.webhook_raw("payment_intent.succeeded", obj, event_id=event_id)

    assert third.json() == {"status": "already_processed"}
    assert published_ids().count(event_id) == 1
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert [(e[0], e[4]) for e in outbox.take()] == [("status_update", "paid")]


def test_a_bad_signature_stores_and_publishes_nothing(shop):
    buyer, order, payment = _awaiting_3ds(shop)
    event_id = _new_id()
    body = json.dumps({"id": event_id, "type": "payment_intent.succeeded",
                       "data": {"object": _succeeded_object(payment)}}).encode()

    response = shop.client.post(
        "/api/v1/orders/webhooks/stripe", content=body,
        headers={"Stripe-Signature": "t=1,v1=00", "Content-Type": "application/json"})

    assert response.status_code == 400
    assert _event_row(shop, event_id) is None
    assert event_id not in published_ids()


def test_a_signed_body_that_is_not_json_is_refused_and_stores_nothing(shop):
    body = b"this is not json"
    signature = StripePaymentProvider.sign_payload(body, settings.STRIPE_WEBHOOK_SECRET.get_secret_value())
    before = shop.db.query(WebhookEvent).count()

    response = shop.client.post(
        "/api/v1/orders/webhooks/stripe", content=body,
        headers={"Stripe-Signature": signature, "Content-Type": "application/json"})

    assert response.status_code == 400
    assert shop.db.query(WebhookEvent).count() == before


def test_an_event_that_cannot_be_stored_is_answered_5xx_so_stripe_retries(shop, monkeypatch):
    buyer, order, payment = _awaiting_3ds(shop)
    event_id = _new_id()

    def _database_down(*args, **kwargs):
        raise OperationalError("INSERT INTO webhook_events", {}, Exception("connection lost"))

    monkeypatch.setattr(stripe_events, "store_event", _database_down)
    server = Shop(TestClient(app, raise_server_exceptions=False), shop.db)

    response = server.webhook_raw("payment_intent.succeeded", _succeeded_object(payment),
                                  event_id=event_id)

    assert response.status_code >= 500
    assert _event_row(shop, event_id) is None
    assert event_id not in published_ids()
    assert order_state(shop, order["id"]) == ("new", "pending")


def test_a_broker_that_is_down_still_gets_a_200_and_the_event_is_kept(
    shop, outbox, monkeypatch, caplog
):
    buyer, order, payment = _awaiting_3ds(shop)
    outbox.take()
    event_id = _new_id()

    def _broker_down(*args, **kwargs):
        raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    monkeypatch.setattr(tasks.process_stripe_event, "apply_async", _broker_down)

    with caplog.at_level(logging.WARNING):
        response = shop.webhook_raw("payment_intent.succeeded", _succeeded_object(payment),
                                    event_id=event_id)

    assert response.status_code == 200
    assert response.json() == {"status": "received"}
    assert _event_row(shop, event_id).status == "received"
    assert event_id not in published_ids()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(event_id in message and "sweeper" in message for message in warnings)
    assert order_state(shop, order["id"]) == ("new", "pending")


def test_the_sweeper_finds_an_event_the_broker_never_got_and_the_worker_applies_it(
    shop, outbox, monkeypatch
):
    buyer, order, payment = _awaiting_3ds(shop)
    outbox.take()
    event_id = _new_id()
    real_publish = tasks.process_stripe_event.apply_async

    def _broker_down(*args, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr(tasks.process_stripe_event, "apply_async", _broker_down)
    shop.webhook_raw("payment_intent.succeeded", _succeeded_object(payment), event_id=event_id)
    monkeypatch.setattr(tasks.process_stripe_event, "apply_async", real_publish)

    # Too recent: the route may still be about to publish it.
    assert tasks.sweep_stripe_events() == 0
    assert event_id not in published_ids()

    _backdate(shop, event_id, settings.STRIPE_EVENT_SWEEP_AFTER_MINUTES + 1)
    assert tasks.sweep_stripe_events() == 1
    assert published_ids().count(event_id) == 1

    assert run_event(event_id) == {"status": "processed", "applied": True}
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert [(e[0], e[4]) for e in outbox.take()] == [("status_update", "paid")]
    # Done: the next sweep leaves it alone.
    assert tasks.sweep_stripe_events() == 0


# -- Two workers, one event -------------------------------------------------------

def test_the_task_reads_the_event_with_a_lock_that_skips_a_row_another_worker_holds(shop):
    # SQLite has no row locks and SQLAlchemy sends no FOR UPDATE there, so the
    # lock is checked the way test_payment_concurrency.py checks the order's:
    # the statement, compiled for Postgres, which is what Postgres would run.
    buyer, order, payment = _awaiting_3ds(shop)
    event_id = _new_id()
    shop.webhook_raw("payment_intent.succeeded", _succeeded_object(payment), event_id=event_id)
    rendered = []

    def _record(state):
        rendered.append(str(state.statement.compile(dialect=postgresql.dialect())))

    sa_event.listen(Session, "do_orm_execute", _record)
    try:
        run_event(event_id)
    finally:
        sa_event.remove(Session, "do_orm_execute", _record)

    claims = [s for s in rendered if "FROM webhook_events" in s and s.lstrip().startswith("SELECT")]
    assert claims and "FOR UPDATE SKIP LOCKED" in claims[0]


def test_two_workers_with_the_same_event_apply_it_once(shop, outbox, monkeypatch):
    buyer, order, payment = _awaiting_3ds(shop)
    outbox.take()
    event_id = _new_id()
    shop.webhook_raw("payment_intent.succeeded", _succeeded_object(payment), event_id=event_id)
    real_claim = stripe_events._claim_event
    held = set()
    applied = []
    results = {}

    def _claim_as_postgres_would(db, claimed_id):
        # FOR UPDATE SKIP LOCKED: a row another transaction holds is not
        # returned at all.
        if claimed_id in held:
            return None
        row = real_claim(db, claimed_id)
        held.add(claimed_id)
        return row

    real_apply = stripe_events.apply_event

    def _apply_while_the_other_worker_picks_up_the_same_message(*args, **kwargs):
        applied.append(1)
        results["second"] = run_event(event_id)
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(stripe_events, "_claim_event", _claim_as_postgres_would)
    monkeypatch.setattr(stripe_events, "apply_event",
                        _apply_while_the_other_worker_picks_up_the_same_message)

    results["first"] = run_event(event_id)

    assert results["second"] == {"status": "skipped"}
    assert results["first"] == {"status": "processed", "applied": True}
    assert applied == [1]
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert [(e[0], e[4]) for e in outbox.take()] == [("status_update", "paid")]


def test_a_message_delivered_twice_applies_the_event_once(shop, outbox):
    buyer, order, payment = _awaiting_3ds(shop)
    outbox.take()
    event_id = _new_id()
    shop.webhook_raw("payment_intent.succeeded", _succeeded_object(payment), event_id=event_id)

    first = run_event(event_id)
    second = run_event(event_id)

    assert first == {"status": "processed", "applied": True}
    assert second == {"status": "skipped"}
    assert [(e[0], e[4]) for e in outbox.take()] == [("status_update", "paid")]
    assert _event_row(shop, event_id).attempts == 0


def test_a_message_for_an_event_that_does_not_exist_is_dropped(shop):
    assert run_event("evt_never_stored") == {"status": "skipped"}


# -- Out of order ---------------------------------------------------------------------

@pytest.mark.parametrize("worker_order", [("failed", "succeeded"), ("succeeded", "failed")])
def test_events_applied_in_either_order_end_with_the_payment_the_provider_took(
    shop, outbox, worker_order
):
    # Stripe does not deliver in order, and the worker may take the messages
    # in any order: a failure that is applied after the success must not undo
    # it, and one applied before it must not stop it.
    buyer, order, payment = _awaiting_3ds(shop)
    outbox.take()
    ids = {"failed": _new_id(), "succeeded": _new_id()}
    shop.webhook_raw("payment_intent.succeeded", _succeeded_object(payment),
                     event_id=ids["succeeded"])
    shop.webhook_raw("payment_intent.payment_failed", _failed_object(payment),
                     event_id=ids["failed"])

    for name in worker_order:
        assert run_event(ids[name])["status"] == "processed"

    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 0, "USD", False)]
    assert {_event_row(shop, i).status for i in ids.values()} == {"processed"}
    assert [(e[0], e[4]) for e in outbox.take()] == [("status_update", "paid")]


# -- An event that overtook its payment attempt ---------------------------------------

def _event_overtaking_its_pay_request(shop, monkeypatch):
    """Run /pay with the success event arriving (stored, not yet applied)
    while /pay still waits on the provider, before it stored the
    PaymentIntent id. Returns (buyer, order, event_id)."""
    product = shop.product(price="19.99", stock=3)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    early = {}
    real_create = StripePaymentProvider.create_payment_intent

    def _event_arrives_during_the_call(self, **kwargs):
        intent = real_create(self, **kwargs)
        obj = {"id": intent.id, "object": "payment_intent", "amount": 1999,
               "amount_received": 1999, "currency": "usd",
               "metadata": {"order_id": str(order["id"])}}
        early["event_id"] = _new_id()
        shop.webhook_raw("payment_intent.succeeded", obj, event_id=early["event_id"])
        early["first_try"] = run_event(early["event_id"])
        return intent

    monkeypatch.setattr(StripePaymentProvider, "create_payment_intent",
                        _event_arrives_during_the_call)
    return buyer, order, early


def test_an_event_that_overtakes_its_pay_request_is_retried_with_a_backoff_then_applied(
    shop, outbox, monkeypatch, retries_seen
):
    buyer, order, early = _event_overtaking_its_pay_request(shop, monkeypatch)
    outbox.take()

    paid = shop.pay(buyer, order["id"])
    assert paid.status_code == 200, paid.text

    event_id = early["event_id"]
    assert early["first_try"] == {"status": "retrying"}
    assert retries_seen == [settings.STRIPE_EVENT_RETRY_BASE_SECONDS]
    row = _event_row(shop, event_id)
    assert row.status == "retrying" and row.attempts == 1
    assert "not recorded yet" in row.last_error
    assert row.processed_at is None

    # By the time the retry runs, /pay stored the attempt's id.
    assert run_event(event_id) == {"status": "processed", "applied": True}
    row = _event_row(shop, event_id)
    assert row.status == "processed" and row.attempts == 1 and row.processed_at is not None
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 0, "USD", False)]
    assert [(e[0], e[4]) for e in outbox.take()] == [("status_update", "paid")]


# -- Processing that fails ---------------------------------------------------------------

def _fail_applying(monkeypatch, failing=lambda: True):
    """Make applying a payment_intent.succeeded fail after it changed the
    order but before it committed."""
    real = stripe_events._apply_payment_success

    def _fails(db, order, payment_row, *args, **kwargs):
        if failing():
            payment_row.status = "succeeded"
            order.payment_status = "paid"
            db.flush()
            raise OperationalError("UPDATE orders ...", {}, Exception("connection lost"))
        return real(db, order, payment_row, *args, **kwargs)

    monkeypatch.setattr(stripe_events, "_apply_payment_success", _fails)


def test_a_failure_while_applying_leaves_nothing_behind_and_schedules_a_retry(
    shop, outbox, monkeypatch, retries_seen
):
    # "Record an event only together with what it changed": the order, the
    # payment and the event's processed mark are one transaction.
    buyer, order, payment = _awaiting_3ds(shop)
    outbox.take()
    _fail_applying(monkeypatch)
    event_id = _new_id()
    shop.webhook_raw("payment_intent.succeeded", _succeeded_object(payment), event_id=event_id)

    assert run_event(event_id) == {"status": "retrying"}

    assert order_state(shop, order["id"]) == ("new", "pending")
    assert payment_rows(shop, order["id"]) == [("requires_action", 1999, 0, "USD", False)]
    row = _event_row(shop, event_id)
    assert (row.status, row.attempts, row.processed_at) == ("retrying", 1, None)
    assert "OperationalError" in row.last_error and "connection lost" in row.last_error
    assert retries_seen == [settings.STRIPE_EVENT_RETRY_BASE_SECONDS]
    assert outbox.take() == []


def test_the_retry_applies_the_event_once_the_cause_is_gone(shop, outbox, monkeypatch):
    buyer, order, payment = _awaiting_3ds(shop)
    outbox.take()
    broken = [True]
    _fail_applying(monkeypatch, failing=lambda: broken[0])
    event_id = _new_id()
    shop.webhook_raw("payment_intent.succeeded", _succeeded_object(payment), event_id=event_id)
    assert run_event(event_id) == {"status": "retrying"}

    broken[0] = False

    assert run_event(event_id) == {"status": "processed", "applied": True}
    row = _event_row(shop, event_id)
    assert (row.status, row.attempts) == ("processed", 1)
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert [(e[0], e[4]) for e in outbox.take()] == [("status_update", "paid")]


def test_repeated_failures_back_off_and_end_in_failed(shop, outbox, monkeypatch, caplog,
                                                       retries_seen):
    buyer, order, payment = _awaiting_3ds(shop)
    _fail_applying(monkeypatch)
    event_id = _new_id()
    shop.webhook_raw("payment_intent.succeeded", _succeeded_object(payment), event_id=event_id)
    max_retries = settings.STRIPE_EVENT_MAX_RETRIES

    outcomes = [run_event(event_id) for _ in range(max_retries)]
    assert outcomes == [{"status": "retrying"}] * max_retries
    assert _event_row(shop, event_id).status == "retrying"
    with caplog.at_level(logging.ERROR):
        last = run_event(event_id)

    assert last == {"status": "failed"}
    row = _event_row(shop, event_id)
    assert (row.status, row.attempts) == ("failed", max_retries + 1)
    assert "connection lost" in row.last_error
    assert row.processed_at is None
    assert order_state(shop, order["id"]) == ("new", "pending")
    # 30 s, doubling, capped.
    base, cap = settings.STRIPE_EVENT_RETRY_BASE_SECONDS, settings.STRIPE_EVENT_RETRY_MAX_SECONDS
    assert retries_seen == [min(base * 2 ** n, cap) for n in range(max_retries)]
    assert retries_seen == [30, 60, 120, 240, 480, 900, 900, 900, 900, 900]
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(event_id in message for message in errors)
    # A failed event is left alone by the worker and by the sweeper.
    assert run_event(event_id) == {"status": "skipped"}
    _backdate(shop, event_id, 24 * 60)
    assert tasks.sweep_stripe_events() == 0


def test_an_event_with_no_usable_payload_fails_instead_of_looping(shop, monkeypatch):
    event_id = _new_id()
    shop.db.add(WebhookEvent(event_id=event_id, event_type="payment_intent.succeeded",
                             payload="not json", status="received"))
    shop.db.commit()
    monkeypatch.setattr(settings, "STRIPE_EVENT_MAX_RETRIES", 1)

    assert run_event(event_id) == {"status": "retrying"}
    assert run_event(event_id) == {"status": "failed"}
    assert "JSONDecodeError" in _event_row(shop, event_id).last_error


def test_an_email_that_fails_after_the_commit_does_not_undo_the_event(
    shop, outbox, monkeypatch, caplog
):
    buyer, order, payment = _awaiting_3ds(shop)

    def _smtp_down(*args, **kwargs):
        raise RuntimeError("smtp down")

    monkeypatch.setattr("app.api.v1.orders.send_order_status_update_email", _smtp_down)
    event_id = _new_id()
    shop.webhook_raw("payment_intent.succeeded", _succeeded_object(payment), event_id=event_id)

    with caplog.at_level(logging.ERROR):
        result = run_event(event_id)

    assert result == {"status": "processed", "applied": True}
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert _event_row(shop, event_id).status == "processed"
    assert any(event_id in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


def test_a_failure_after_the_commit_does_not_retry_an_event_that_is_already_applied(
    shop, outbox, monkeypatch, retries_seen
):
    buyer, order, payment = _awaiting_3ds(shop)
    real = stripe_events._apply_payment_success

    def _commits_then_fails(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("raised after the commit")

    monkeypatch.setattr(stripe_events, "_apply_payment_success", _commits_then_fails)
    event_id = _new_id()
    shop.webhook_raw("payment_intent.succeeded", _succeeded_object(payment), event_id=event_id)

    assert run_event(event_id) == {"status": "skipped"}

    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    row = _event_row(shop, event_id)
    assert (row.status, row.attempts) == ("processed", 0)
    assert retries_seen == []


@pytest.mark.parametrize("attempts, expected", [
    (1, 30), (2, 60), (3, 120), (4, 240), (5, 480), (6, 900), (7, 900), (50, 900),
])
def test_the_retry_delay_doubles_from_the_base_up_to_the_cap(attempts, expected):
    assert stripe_events.retry_delay_seconds(attempts) == expected


def test_the_backoff_follows_the_settings(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_EVENT_RETRY_BASE_SECONDS", 10)
    monkeypatch.setattr(settings, "STRIPE_EVENT_RETRY_MAX_SECONDS", 50)

    assert [stripe_events.retry_delay_seconds(n) for n in (1, 2, 3, 4)] == [10, 20, 40, 50]


# -- The beat schedule -------------------------------------------------------------------

def test_the_sweeper_runs_every_few_minutes_on_the_payments_queue():
    entry = tasks.celery.conf.beat_schedule["sweep-stripe-events"]

    assert entry["task"] == "sweep_stripe_events"
    assert entry["schedule"] == timedelta(minutes=5)
    assert entry["task"] in tasks.celery.tasks
    assert tasks.celery.tasks["sweep_stripe_events"].queue == "payments"
    assert tasks.celery.tasks["process_stripe_event"].queue == "payments"
    assert "payments" in tasks.celery.conf.task_queues


def test_the_retry_limit_is_the_events_own_not_celerys():
    # The count lives on the event row (attempts), where an admin can see it
    # and a replay can reset it; Celery's per-message limit must not stop a
    # retry first.
    assert tasks.process_stripe_event.max_retries is None


# -- The sweeper -------------------------------------------------------------------------

def _stored(shop, status, minutes_old, event_type="payment_intent.created"):
    event_id = _new_id()
    shop.db.add(WebhookEvent(
        event_id=event_id, event_type=event_type, status=status,
        payload=json.dumps({"id": event_id, "type": event_type, "data": {"object": {}}}),
        created_at=utc_now() - timedelta(minutes=minutes_old),
        updated_at=utc_now() - timedelta(minutes=minutes_old)))
    shop.db.commit()
    return event_id


def test_the_sweeper_publishes_old_received_and_retrying_events_only(shop):
    old = settings.STRIPE_EVENT_SWEEP_AFTER_MINUTES + 5
    stuck_received = _stored(shop, "received", old)
    stuck_retrying = _stored(shop, "retrying", old)
    recent = _stored(shop, "received", 1)
    done = _stored(shop, "processed", old)
    failed = _stored(shop, "failed", old)

    assert tasks.sweep_stripe_events() == 2

    ids = published_ids()
    assert stuck_received in ids and stuck_retrying in ids
    assert not {recent, done, failed} & set(ids)


def test_the_sweeper_does_not_publish_the_same_event_on_every_run(shop):
    event_id = _stored(shop, "received", settings.STRIPE_EVENT_SWEEP_AFTER_MINUTES + 5)

    assert tasks.sweep_stripe_events() == 1
    assert tasks.sweep_stripe_events() == 0

    assert published_ids().count(event_id) == 1


def test_the_sweeper_takes_the_oldest_first_and_a_batch_at_a_time(shop, monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_EVENT_SWEEP_BATCH", 2)
    old = settings.STRIPE_EVENT_SWEEP_AFTER_MINUTES
    newest = _stored(shop, "received", old + 10)
    oldest = _stored(shop, "received", old + 30)
    middle = _stored(shop, "retrying", old + 20)

    assert tasks.sweep_stripe_events() == 2
    assert [i for i in published_ids() if i in (newest, oldest, middle)] == [oldest, middle]

    assert tasks.sweep_stripe_events() == 1
    assert published_ids().count(newest) == 1


def test_the_sweeper_stops_when_the_broker_is_still_down_and_tries_again_next_run(
    shop, monkeypatch, caplog
):
    event_id = _stored(shop, "received", settings.STRIPE_EVENT_SWEEP_AFTER_MINUTES + 5)
    real_publish = tasks.process_stripe_event.apply_async

    def _broker_down(*args, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr(tasks.process_stripe_event, "apply_async", _broker_down)
    with caplog.at_level(logging.WARNING):
        assert tasks.sweep_stripe_events() == 0
    assert event_id not in published_ids()
    assert any(event_id in r.getMessage() for r in caplog.records)

    monkeypatch.setattr(tasks.process_stripe_event, "apply_async", real_publish)
    assert tasks.sweep_stripe_events() == 1
    assert event_id in published_ids()


def test_a_sweep_with_nothing_stuck_publishes_nothing(shop):
    before = len(published_ids())

    assert tasks.sweep_stripe_events() == 0

    assert len(published_ids()) == before
