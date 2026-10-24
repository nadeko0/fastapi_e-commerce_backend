"""
Driving Stripe webhook events through the route and the Celery task, without
a broker.

The route stores an event and publishes a process_stripe_event message;
here the message is recorded instead of sent (`published`), and a test (or
`deliver`) runs the task body itself with `run_event`, when it wants the
worker to get to it. The task runs against the suite's SQLite database
(tasks.SessionLocal), and its retry request is recorded (`retries`) and
raised as celery's Retry, instead of being scheduled.

Installed when this module is imported, which every test that posts a
webhook does (payment_support, test_payment). Not a test module itself.
"""
import json

from celery.exceptions import Retry
from conftest import TestingSessionLocal

from app import tasks

# (event id, publish options) for every message the route or the sweeper
# published. Never cleared: a test looks for its own event ids.
published = []
# The countdown of every retry the task asked for, in order. Never cleared.
retries = []


def _publish(args=None, kwargs=None, **options):
    published.append((args[0], options))


def _retry(exc=None, countdown=None, **options):
    retries.append(countdown)
    raise Retry(exc=exc, when=countdown)


def install() -> None:
    tasks.SessionLocal = TestingSessionLocal
    tasks.process_stripe_event.apply_async = _publish
    tasks.process_stripe_event.retry = _retry


def published_ids():
    return [event_id for event_id, _ in published]


def run_event(event_id) -> dict:
    """One run of the task for the event, as a worker does it. Returns what
    the task returned; {"status": "retrying"} when it asked to be retried."""
    try:
        return tasks.process_stripe_event.run(event_id)
    except Retry:
        return {"status": "retrying"}


class Delivered:
    """What a test sees of one webhook delivery handled by route and task:
    the status code Stripe got, and the body the event produced once the
    worker applied it (the shape the route answered before events were
    applied in a task)."""

    def __init__(self, route_response, result):
        self.route_response = route_response
        self.status_code = route_response.status_code
        self.result = result
        self.headers = route_response.headers

    def __getattr__(self, name):
        # Anything else (the request, the encoding...) is the route's.
        return getattr(self.route_response, name)

    def json(self):
        return self.result

    @property
    def text(self):
        return json.dumps(self.json())


def deliver(response, content):
    """The route's `response` to the delivery of `content`, followed by the
    worker getting to the event it stored. A delivery the route did not
    store (refused, a duplicate) is returned as it is."""
    if response.status_code != 200 or response.json()["status"] != "received":
        return response
    return Delivered(response, run_event(json.loads(content)["id"]))


install()
