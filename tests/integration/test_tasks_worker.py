"""Integration tests executing Celery tasks through an embedded worker.

Per Celery documentation ("Testing with Celery",
https://docs.celeryq.dev/en/stable/userguide/testing.html):
"The eager mode enabled by the task_always_eager setting is by definition
not suitable for unit tests."
Instead, an embedded worker using celery.contrib.testing.worker.start_worker
with the in-memory broker (memory://) and result backend (cache+memory://)
executes the tasks end-to-end across threads against the test database.
"""

import json
from collections.abc import Generator

import celery.app.task
import pytest
from celery import _state as _celery_state
from celery.contrib.testing.worker import start_worker
from sqlalchemy import select

from app import tasks
from app.models.payment import WEBHOOK_EVENT_PROCESSED, WebhookEvent
from tests.integration.test_tasks import (
    _make_category_and_product,
    _make_order_with_item,
    _make_verified_user,
)


@pytest.fixture
def embedded_worker(monkeypatch) -> Generator[None]:
    """Start an embedded Celery worker with an in-memory broker and backend."""
    orig_broker = tasks.celery.conf.broker_url
    orig_backend = tasks.celery.conf.result_backend
    orig_eager = tasks.celery.conf.task_always_eager
    orig_local_backend = getattr(tasks.celery._local, "backend", None)
    orig_backend_cache = tasks.celery._backend_cache

    tasks.celery.conf.update(
        broker_url="memory://",
        result_backend="cache+memory://",
        task_always_eager=False,
    )
    tasks.celery._local.backend = None
    tasks.celery._backend_cache = None

    # Ensure apply_async and retry are unpatched if stripe_event_support was imported
    orig_apply_async = tasks.process_stripe_event.apply_async
    orig_retry = tasks.process_stripe_event.retry
    monkeypatch.setattr(
        tasks.process_stripe_event,
        "apply_async",
        celery.app.task.Task.apply_async.__get__(
            tasks.process_stripe_event, type(tasks.process_stripe_event)
        ),
    )
    monkeypatch.setattr(
        tasks.process_stripe_event,
        "retry",
        celery.app.task.Task.retry.__get__(
            tasks.process_stripe_event, type(tasks.process_stripe_event)
        ),
    )

    # Declare queues on the in-memory broker so publications do not get dropped
    with tasks.celery.connection_for_write() as conn:
        for q_name in ["default", "emails", "payments"]:
            q = tasks.celery.amqp.queues[q_name](conn.default_channel)
            q.declare()
            q.purge()

    prev_default = _celery_state.default_app
    tasks.celery.set_default()
    try:
        with start_worker(
            tasks.celery,
            pool="solo",
            perform_ping_check=False,
            loglevel="ERROR",
            queues=["default", "emails", "payments"],
        ):
            yield
    finally:
        _celery_state.set_default_app(prev_default)
        tasks.process_stripe_event.apply_async = orig_apply_async
        tasks.process_stripe_event.retry = orig_retry
        tasks.celery.conf.update(
            broker_url=orig_broker,
            result_backend=orig_backend,
            task_always_eager=orig_eager,
        )
        tasks.celery._local.backend = orig_local_backend
        tasks.celery._backend_cache = orig_backend_cache


def test_send_order_confirmation_through_worker(
    db_session, tasks_db, embedded_worker, monkeypatch
):
    """Run send_order_confirmation end to end through the embedded worker."""
    sent: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "app.tasks.send_order_confirmation_email",
        lambda to_email, order: sent.append((to_email, order.id)),
    )

    user = _make_verified_user(db_session)
    _, product = _make_category_and_product(db_session)
    order = _make_order_with_item(db_session, user, product)

    async_result = tasks.send_order_confirmation.apply_async(args=[order.id])
    async_result.get(timeout=10)

    assert sent == [(user.email, order.id)]


def test_process_stripe_event_through_worker(db_session, tasks_db, embedded_worker):
    """Run process_stripe_event end to end through the embedded worker."""
    event_id = "evt_test_worker_123"
    event_payload = {
        "id": event_id,
        "object": "event",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_test_worker_foreign",
                "object": "payment_intent",
                "status": "succeeded",
                "amount": 1999,
                "currency": "usd",
            }
        },
    }

    event = WebhookEvent(
        event_id=event_id,
        event_type="payment_intent.succeeded",
        payload=json.dumps(event_payload),
        status="received",
        attempts=0,
    )
    db_session.add(event)
    db_session.commit()

    async_result = tasks.process_stripe_event.apply_async(args=[event_id])
    result = async_result.get(timeout=10)

    assert result == {"status": "processed", "applied": False}

    db_session.expire_all()
    stored = db_session.scalars(
        select(WebhookEvent).where(WebhookEvent.event_id == event_id)
    ).one()
    assert stored.status == WEBHOOK_EVENT_PROCESSED
    assert stored.processed_at is not None
