"""
GET /admin/webhook-events and POST /admin/webhook-events/{event_id}/replay:
an admin sees the Stripe events the worker could not apply, and can send a
failed one through the worker again once the cause is fixed.

(That the routes refuse anonymous callers and non-admins is audited for every
admin route by test_admin.py.)
"""
import json
import logging
import uuid
from datetime import timedelta

import pytest
from payment_support import (
    PAY_3DS,
    Outbox,
    Shop,
    order_state,
    reset_provider,
    use_production_sessions,
)
from stripe_event_support import published_ids, run_event

import app.services.stripe_events as stripe_events
from app import tasks
from app.core.clock import utc_now
from app.models.payment import WebhookEvent

ADMIN_EVENTS = "/api/v1/admin/webhook-events"


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
def admin(shop):
    return shop.admin()


def _store(shop, status, minutes_ago=0, event_type="payment_intent.succeeded", **fields):
    event_id = f"evt_{uuid.uuid4().hex[:24]}"
    when = utc_now() - timedelta(minutes=minutes_ago)
    shop.db.add(WebhookEvent(
        event_id=event_id, event_type=event_type, status=status,
        payload=json.dumps({"id": event_id, "type": event_type,
                            "data": {"object": {"id": "pi_secret_looking_payload"}}}),
        created_at=when, updated_at=when, **fields))
    shop.db.commit()
    return event_id


def _get(shop, admin, **params):
    return shop.client.get(ADMIN_EVENTS, params=params, headers=admin)


# -- The list ------------------------------------------------------------------------

def test_the_list_is_newest_first_and_carries_no_payload(shop, admin):
    oldest = _store(shop, "processed", minutes_ago=30)
    failed = _store(shop, "failed", minutes_ago=20, attempts=11, last_error="OperationalError: x")
    newest = _store(shop, "received", minutes_ago=1)

    response = _get(shop, admin)

    assert response.status_code == 200
    data = response.json()["data"]
    assert [item["event_id"] for item in data["items"]] == [newest, failed, oldest]
    assert (data["total"], data["page"], data["size"], data["has_more"]) == (3, 1, 20, False)
    item = data["items"][1]
    assert item["status"] == "failed" and item["attempts"] == 11
    assert item["last_error"] == "OperationalError: x"
    assert item["event_type"] == "payment_intent.succeeded"
    assert set(item) == {"id", "event_id", "event_type", "status", "attempts", "last_error",
                         "processed_at", "created_at", "updated_at"}
    assert "pi_secret_looking_payload" not in response.text


def test_the_list_filters_by_status(shop, admin):
    _store(shop, "processed")
    failed_a = _store(shop, "failed", minutes_ago=5)
    failed_b = _store(shop, "failed", minutes_ago=2)
    _store(shop, "retrying")

    response = _get(shop, admin, status="failed")

    data = response.json()["data"]
    assert [item["event_id"] for item in data["items"]] == [failed_b, failed_a]
    assert data["total"] == 2


def test_the_list_is_paginated(shop, admin):
    ids = [_store(shop, "failed", minutes_ago=m) for m in (50, 40, 30, 20, 10)]
    newest_first = list(reversed(ids))

    first = _get(shop, admin, size=2, page=1).json()["data"]
    last = _get(shop, admin, size=2, page=3).json()["data"]

    assert [i["event_id"] for i in first["items"]] == newest_first[:2]
    assert (first["total"], first["has_more"]) == (5, True)
    assert [i["event_id"] for i in last["items"]] == newest_first[4:]
    assert (last["page"], last["has_more"]) == (3, False)


def test_an_unknown_status_is_a_422_that_names_the_valid_ones(shop, admin):
    response = _get(shop, admin, status="exploded")

    assert response.status_code == 422
    assert "received" in response.text and "failed" in response.text


# -- Replay --------------------------------------------------------------------------

def test_replay_of_an_unknown_event_is_a_404(shop, admin):
    response = shop.client.post(f"{ADMIN_EVENTS}/evt_nope/replay", headers=admin)

    assert response.status_code == 404


@pytest.mark.parametrize("status", ["received", "processing", "processed", "retrying"])
def test_replay_of_an_event_that_has_not_failed_is_a_409(shop, admin, status):
    event_id = _store(shop, status)

    response = shop.client.post(f"{ADMIN_EVENTS}/{event_id}/replay", headers=admin)

    assert response.status_code == 409
    shop.db.expire_all()
    assert shop.db.query(WebhookEvent).filter_by(event_id=event_id).one().status == status
    assert event_id not in published_ids()


def test_replay_resets_a_failed_event_and_hands_it_to_the_worker(shop, admin):
    event_id = _store(shop, "failed", minutes_ago=90, attempts=11, last_error="boom")

    response = shop.client.post(f"{ADMIN_EVENTS}/{event_id}/replay", headers=admin)

    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["event_id"], data["status"], data["attempts"]) == (event_id, "received", 0)
    shop.db.expire_all()
    row = shop.db.query(WebhookEvent).filter_by(event_id=event_id).one()
    assert (row.status, row.attempts) == ("received", 0)
    assert row.processed_at is None
    assert row.last_error == "boom"
    assert row.updated_at > utc_now() - timedelta(minutes=1)
    assert published_ids().count(event_id) == 1


def test_replay_with_the_broker_down_keeps_the_event_for_the_sweeper(
    shop, admin, monkeypatch, caplog
):
    event_id = _store(shop, "failed")

    def _broker_down(*args, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr(tasks.process_stripe_event, "apply_async", _broker_down)
    with caplog.at_level(logging.WARNING):
        response = shop.client.post(f"{ADMIN_EVENTS}/{event_id}/replay", headers=admin)

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "received"
    assert event_id not in published_ids()
    assert any(event_id in r.getMessage() for r in caplog.records)


# -- The whole path ------------------------------------------------------------------

def test_a_failed_event_is_listed_replayed_and_applied(shop, admin, monkeypatch):
    outbox = Outbox(monkeypatch)
    product = shop.product(price="19.99", stock=5)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    assert shop.pay(buyer, order["id"], token=PAY_3DS).status_code == 200
    payment = shop.payments(order["id"])[0]
    outbox.take()
    real = stripe_events._apply_payment_success
    broken = [True]

    def _fails_while_broken(*args, **kwargs):
        if broken[0]:
            raise RuntimeError("the database is on fire")
        return real(*args, **kwargs)

    monkeypatch.setattr(stripe_events, "_apply_payment_success", _fails_while_broken)
    obj = {"id": payment.payment_intent_id, "object": "payment_intent", "amount": 1999,
           "amount_received": 1999, "currency": "usd",
           "metadata": {"order_id": str(order["id"])}}
    event_id = f"evt_{uuid.uuid4().hex[:24]}"
    shop.webhook_raw("payment_intent.succeeded", obj, event_id=event_id)
    attempts = 0
    while run_event(event_id)["status"] != "failed":
        attempts += 1
        assert attempts < 50
    assert order_state(shop, order["id"]) == ("new", "pending")

    listed = _get(shop, admin, status="failed").json()["data"]["items"]
    assert [i["event_id"] for i in listed] == [event_id]
    assert "the database is on fire" in listed[0]["last_error"]

    broken[0] = False
    assert shop.client.post(f"{ADMIN_EVENTS}/{event_id}/replay", headers=admin).status_code == 200
    assert run_event(event_id) == {"status": "processed", "applied": True}

    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert _get(shop, admin, status="failed").json()["data"]["items"] == []
    done = _get(shop, admin, status="processed").json()["data"]["items"]
    assert [i["event_id"] for i in done] == [event_id]
    assert done[0]["processed_at"] is not None
    assert [(e[0], e[4]) for e in outbox.take()] == [("status_update", "paid")]
