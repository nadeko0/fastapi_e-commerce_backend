"""The beat schedule across the European clock changes of 2026.

On 2026-10-25 at 01:00 UTC local time in Europe went back one hour (local
03:00 CEST became 02:00 CET, so 02:00-03:00 happened twice); on 2026-03-29
it went forward (02:00-03:00 did not happen). The crontab jobs sit at 02:30,
02:45, 03:00 and 03:15, inside that hour. They are safe only because Celery
keeps its schedule in UTC, where nothing changes. These tests fix that
choice and replay beat, minute by minute, with Celery's own schedule API
(crontab.is_due) and a frozen clock.
"""
import copy
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest
from celery import Celery
from celery.schedules import crontab

from app import tasks

CRON_JOBS = {
    "cleanup-inactive-accounts": (2, 30),
    "cleanup-expired-retention": (2, 45),
    "cleanup-expired-idempotency-keys": (3, 0),
    "check-payment-reconciliation": (3, 15),
}

AUTUMN = (datetime(2026, 10, 24, tzinfo=UTC), datetime(2026, 10, 27, tzinfo=UTC))
SPRING = (datetime(2026, 3, 28, tzinfo=UTC), datetime(2026, 3, 31, tzinfo=UTC))


def test_celery_keeps_its_clock_in_utc():
    """A local zone here would run the 02:30/02:45/03:00/03:15 jobs twice, or
    not at all, on the nights the clocks change. UTC has no such night."""
    conf = tasks.celery.conf
    assert conf.timezone == "UTC"
    assert conf.enable_utc is True


def test_every_crontab_job_is_in_the_replay():
    crontabs = {
        name for name, entry in tasks.celery.conf.beat_schedule.items()
        if isinstance(entry["schedule"], crontab)
    }
    assert crontabs == set(CRON_JOBS)


def _replay(schedule, app, start, end, local_zone=None):
    """The instants (UTC) at which beat would run `schedule` between `start`
    and `end`, looking at the clock once a minute. `local_zone` makes the
    schedule's clock read in that zone, as an app with timezone set to it."""
    clock = {"now": start}

    def nowfun():
        now = clock["now"]
        return now.astimezone(local_zone) if local_zone else now

    job = copy.copy(schedule)
    job.nowfun = nowfun
    job.app = app
    runs = []
    last_run_at = nowfun()
    while clock["now"] < end:
        clock["now"] += timedelta(minutes=1)
        due, _ = job.is_due(last_run_at)
        if due:
            last_run_at = nowfun()
            runs.append(clock["now"])
    return runs


@pytest.mark.parametrize("window", [AUTUMN, SPRING], ids=["clocks go back", "clocks go forward"])
@pytest.mark.parametrize("name", sorted(CRON_JOBS))
def test_each_job_runs_once_a_day_24_hours_apart(name, window):
    start, end = window
    hour, minute = CRON_JOBS[name]
    runs = _replay(tasks.celery.conf.beat_schedule[name]["schedule"], tasks.celery, start, end)

    days = (end - start).days
    assert runs == [
        (start + timedelta(days=day)).replace(hour=hour, minute=minute) for day in range(days)
    ]
    assert {later - earlier for earlier, later in pairwise(runs)} == {timedelta(hours=24)}


def test_the_same_jobs_in_a_local_zone_would_not_keep_24_hours():
    """The control: the replay can tell. An app on Europe/Bratislava runs the
    02:30 job at 00:30 UTC before the change and 01:30 UTC after it."""
    local_app = Celery("control")
    local_app.conf.timezone = "Europe/Bratislava"
    local_app.conf.enable_utc = True
    schedule = tasks.celery.conf.beat_schedule["cleanup-inactive-accounts"]["schedule"]

    runs = _replay(schedule, local_app, *AUTUMN, local_zone=ZoneInfo("Europe/Bratislava"))

    gaps = {later - earlier for earlier, later in pairwise(runs)}
    assert gaps != {timedelta(hours=24)}
