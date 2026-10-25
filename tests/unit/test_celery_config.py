import json
from unittest.mock import patch

import pytest
from celery.exceptions import Retry
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app import tasks
from app.core.config import settings
from app.models.payment import WEBHOOK_EVENT_FAILED, WebhookEvent


def test_long_running_tasks_are_cancelled_on_connection_loss():
    """Tasks are acks_late, so a task running when the broker connection drops
    is redelivered. With the setting unset Celery 5.6 warns that the default
    becomes True in 6.0; setting it explicitly cancels the stale run instead of
    letting it execute in parallel with its redelivered copy."""
    conf = tasks.celery.conf
    assert conf.task_acks_late is True
    assert conf.worker_cancel_long_running_tasks_on_connection_loss is True


def test_task_reliability_and_retry_configuration():
    """Celery documentation ('Configuration', task_reject_on_worker_lost):
    'Even if task_acks_late is enabled, the worker will acknowledge tasks when
    the worker process executing them abruptly exits or is signaled... Setting
    this option will cause the message to be re-queued instead.'
    The documentation warns that enabling this can cause message loops if tasks
    repeatedly cause the worker to crash. Ensure task_acks_late and
    task_reject_on_worker_lost are pinned, and that tasks have bounded retries."""
    conf = tasks.celery.conf
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True

    # send_order_confirmation has a Celery-level retry bound:
    assert tasks.send_order_confirmation.max_retries == 3
    assert tasks.send_order_confirmation.autoretry_for == (OperationalError,)

    # process_stripe_event sets max_retries=None because retry count is bounded
    # at the application layer on WebhookEvent.attempts up to STRIPE_EVENT_MAX_RETRIES.
    assert tasks.process_stripe_event.max_retries is None


def test_send_order_confirmation_stops_after_max_retries():
    """A task that keeps failing stops when its retry limit is reached rather than looping."""
    with patch(
        "app.tasks.task_session",
        side_effect=OperationalError("SELECT 1", {}, Exception("DB down")),
    ):
        result = tasks.send_order_confirmation.apply(args=(1,), throw=False)
        assert result.failed() is True
        assert isinstance(result.result, OperationalError)


def test_stripe_event_retries_are_bounded_and_sweeper_ignores_failed_events(
    db_session, tasks_db, monkeypatch
):
    """When process_stripe_event fails repeatedly, it stops retrying once attempts
    exceed STRIPE_EVENT_MAX_RETRIES. Once failed, it returns 'failed'/'skipped'
    without requesting retries, and the sweeper ignores it to prevent infinite message loops."""
    monkeypatch.setattr(settings, "STRIPE_EVENT_MAX_RETRIES", 2)
    monkeypatch.setattr(settings, "STRIPE_EVENT_SWEEP_AFTER_MINUTES", 0)

    event_id = "evt_test_bounded_retry"
    event_payload = {
        "id": event_id,
        "object": "event",
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_test_bounded",
                "object": "payment_intent",
                "status": "succeeded",
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

    monkeypatch.setattr(
        "app.services.stripe_events.apply_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("permanent failure")),
    )

    # Attempt 1: fails, raises Retry (attempts=1, status=retrying)
    with pytest.raises(Retry):
        tasks.process_stripe_event.run(event_id)

    # Attempt 2: fails, raises Retry (attempts=2, status=retrying)
    with pytest.raises(Retry):
        tasks.process_stripe_event.run(event_id)

    # Attempt 3: attempts (3) > STRIPE_EVENT_MAX_RETRIES (2) -> marks failed, does NOT raise Retry
    outcome = tasks.process_stripe_event.run(event_id)
    assert outcome == {"status": "failed"}

    db_session.expire_all()
    stored = db_session.scalars(
        select(WebhookEvent).where(WebhookEvent.event_id == event_id)
    ).one()
    assert stored.status == WEBHOOK_EVENT_FAILED
    assert stored.attempts == 3

    # Subsequent execution returns skipped (no Retry raised)
    assert tasks.process_stripe_event.run(event_id) == {"status": "skipped"}

    # The sweeper only enqueues received/retrying events, so it ignores failed events
    assert tasks.sweep_stripe_events() == 0
