"""Tests for Celery beat schedule configuration.

Celery beat periodic tasks must name tasks that actually exist in the worker's
task registry (tasks.celery.tasks) and must specify schedules of the expected
type (crontab for clock-aligned tasks, timedelta for recurring intervals).
"""

from datetime import timedelta

import pytest
from celery.schedules import crontab, schedule

from app import tasks

EXPECTED_BEAT_SCHEDULE = {
    "cleanup-inactive-accounts": {
        "task": "cleanup_inactive_accounts",
        "schedule": crontab(hour=2, minute=30),
        "kind": crontab,
    },
    "update-product-stats": {
        "task": "update_product_stats",
        "schedule": timedelta(hours=1),
        "kind": (timedelta, schedule),
    },
    "check-low-stock": {
        "task": "check_low_stock",
        "schedule": timedelta(hours=4),
        "kind": (timedelta, schedule),
    },
    "cancel-expired-unpaid-orders": {
        "task": "cancel_expired_unpaid_orders",
        "schedule": timedelta(minutes=5),
        "kind": (timedelta, schedule),
    },
    "check-payment-reconciliation": {
        "task": "check_payment_reconciliation",
        "schedule": crontab(hour=3, minute=15),
        "kind": crontab,
    },
    "sweep-stripe-events": {
        "task": "sweep_stripe_events",
        "schedule": timedelta(minutes=5),
        "kind": (timedelta, schedule),
    },
    "cleanup-expired-retention": {
        "task": "cleanup_expired_retention",
        "schedule": crontab(hour=2, minute=45),
        "kind": crontab,
    },
    "cleanup-expired-idempotency-keys": {
        "task": "cleanup_expired_idempotency_keys",
        "schedule": crontab(hour=3, minute=0),
        "kind": crontab,
    },
}


def test_every_beat_schedule_entry_is_a_registered_celery_task():
    """Verify that every entry in beat_schedule targets a real registered task.
    Previous bug: entries named 'app.tasks.<name>' but tasks were registered under
    bare names, causing Celery to drop them as unregistered tasks."""
    registered = {name for name in tasks.celery.tasks if not name.startswith("celery.")}
    beat_schedule = tasks.celery.conf.beat_schedule

    assert set(beat_schedule.keys()) == set(EXPECTED_BEAT_SCHEDULE.keys())

    for name, entry in beat_schedule.items():
        task_name = entry["task"]
        assert task_name in registered, (
            f"Beat entry {name!r} targets task {task_name!r}, which is not registered in Celery"
        )
        assert task_name in tasks.celery.tasks


@pytest.mark.parametrize("name,expected", list(EXPECTED_BEAT_SCHEDULE.items()))
def test_beat_schedule_entries_have_expected_schedules(name, expected):
    """Verify each periodic task has the expected schedule and schedule kind."""
    entry = tasks.celery.conf.beat_schedule[name]
    assert entry["task"] == expected["task"]
    assert isinstance(entry["schedule"], expected["kind"])
    assert entry["schedule"] == expected["schedule"]
