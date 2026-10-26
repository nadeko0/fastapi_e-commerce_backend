"""
USER_DATA_RETENTION_DAYS is enforced (GDPR Art. 5(1)(e)) and erasure reaches
the personal data kept in stored Stripe webhook payloads (Art. 17).

The privacy policy promises "we keep your data for N days unless required
longer by law, you request deletion, or legal proceedings". The narrowest
reading is applied: deactivated, non-admin accounts with no activity for N
days are anonymised; nobody else is touched.
"""
import json
import logging
import pathlib
from datetime import timedelta

from app import tasks
from app.core.clock import utc_now
from app.core.config import settings
from app.core.security import get_password_hash
from app.models.address import Address
from app.models.order import Order
from app.models.payment import Payment, WebhookEvent
from app.models.user import User
from app.services import stripe_events

RETENTION = settings.USER_DATA_RETENTION_DAYS


def _user(db, email, *, idle_days, is_active=False, role="client", period=None):
    when = utc_now() - timedelta(days=idle_days)
    user = User(
        email=email, hashed_password=get_password_hash("Str0ngPass1"), full_name="Jane Roe",
        phone="+4912345678", is_active=is_active, role=role,
        created_at=when, last_login=when, last_activity_date=when,
        **({"data_retention_period": period} if period else {}),
    )
    db.add(user)
    db.commit()
    return user


def _is_anonymised(db, user):
    db.expire_all()
    return db.get(User, user.id).email == f"deleted-user-{user.id}@deleted.invalid"


def _order_with_payment(db, user, intent_id, session_id=None):
    address = Address(
        user_id=user.id, street="Main 1", city="Berlin", state="BE", postal_code="10115",
        country="DE", is_active=True,
    )
    db.add(address)
    db.commit()
    order = Order(user_id=user.id, shipping_address_id=address.id, total_amount=10)
    db.add(order)
    db.commit()
    db.add(Payment(
        order_id=order.id, payment_intent_id=intent_id, checkout_session_id=session_id,
        amount=1000, currency="eur",
    ))
    db.commit()
    return order


def _event(db, event_id, obj, event_type="payment_intent.succeeded", status="processed"):
    payload = json.dumps({
        "id": event_id, "object": "event", "type": event_type, "created": 1,
        "request": {"id": "req_1"}, "data": {"object": obj},
    })
    row = WebhookEvent(event_id=event_id, event_type=event_type, payload=payload, status=status)
    db.add(row)
    db.commit()
    return row


def _payload(db, row):
    db.expire_all()
    return json.loads(db.get(WebhookEvent, row.id).payload)


# -- the retention task --------------------------------------------------------

def test_retention_purges_exactly_the_deactivated_idle_accounts(db_session):
    old_closed = _user(db_session, "old@example.com", idle_days=RETENTION + 5)
    recent_closed = _user(db_session, "recent@example.com", idle_days=RETENTION - 5)
    old_active = _user(db_session, "active@example.com", idle_days=RETENTION + 5, is_active=True)
    old_admin = _user(db_session, "admin@example.com", idle_days=RETENTION + 5, role="admin")

    assert tasks.purge_expired_retention(db_session) == 1

    assert _is_anonymised(db_session, old_closed)
    assert db_session.get(User, old_closed.id).full_name is None
    for kept in (recent_closed, old_active, old_admin):
        assert not _is_anonymised(db_session, kept)
        assert db_session.get(User, kept.id).full_name == "Jane Roe"


def test_retention_uses_the_users_own_period_and_the_latest_activity(db_session):
    short = _user(db_session, "short@example.com", idle_days=40, period=30)
    long = _user(db_session, "long@example.com", idle_days=40, period=60)
    # Old sign-up and old login, but touched recently: still within retention.
    touched = _user(db_session, "touched@example.com", idle_days=RETENTION + 50)
    touched.last_activity_date = utc_now() - timedelta(days=2)
    db_session.commit()
    never_logged_in = _user(db_session, "never@example.com", idle_days=RETENTION + 5)
    never_logged_in.last_login = None
    never_logged_in.last_activity_date = None
    db_session.commit()

    assert tasks.purge_expired_retention(db_session) == 2

    assert _is_anonymised(db_session, short)
    assert _is_anonymised(db_session, never_logged_in)
    assert not _is_anonymised(db_session, long)
    assert not _is_anonymised(db_session, touched)


def test_retention_is_idempotent_batched_and_logged(db_session, caplog):
    accounts = [_user(db_session, f"u{i}@example.com", idle_days=RETENTION + 5) for i in range(5)]

    with caplog.at_level(logging.INFO, logger="app.tasks"):
        assert tasks.purge_expired_retention(db_session, batch_size=2) == 5
    assert any("anonymised 5 account" in r.getMessage() for r in caplog.records)
    assert all(_is_anonymised(db_session, a) for a in accounts)

    assert tasks.purge_expired_retention(db_session) == 0


def test_retention_task_and_beat_entry(db_session, tasks_db):
    account = _user(db_session, "task@example.com", idle_days=RETENTION + 5)

    assert tasks.cleanup_expired_retention() == 1

    assert _is_anonymised(db_session, account)
    entry = tasks.celery.conf.beat_schedule["cleanup-expired-retention"]
    assert entry["task"] == "cleanup_expired_retention"
    assert entry["schedule"] == tasks.crontab(hour=2, minute=45)
    assert tasks.celery.tasks["cleanup_expired_retention"].queue == "cleanup"


# -- stored Stripe events --------------------------------------------------------

def _pi_object(intent_id, order_id):
    return {
        "id": intent_id, "object": "payment_intent", "amount": 1000, "amount_received": 1000,
        "currency": "eur", "status": "succeeded", "receipt_email": "jane@example.com",
        "metadata": {"order_id": str(order_id), "note": "Jane Roe"},
        "shipping": {"name": "Jane Roe", "address": {"line1": "Main 1"}},
    }


def test_retention_purge_redacts_the_events_of_the_erased_account_only(db_session):
    gone = _user(db_session, "gone@example.com", idle_days=RETENTION + 5)
    other = _user(db_session, "other@example.com", idle_days=1, is_active=True)
    order = _order_with_payment(db_session, gone, "pi_gone", "cs_gone")
    other_order = _order_with_payment(db_session, other, "pi_other")
    pi_event = _event(db_session, "evt_pi", _pi_object("pi_gone", order.id))
    session_event = _event(
        db_session, "evt_cs",
        {"id": "cs_gone", "object": "checkout.session", "payment_intent": "pi_gone",
         "amount_total": 1000, "customer_details": {"email": "jane@example.com"}},
        event_type="checkout.session.completed",
    )
    charge_event = _event(
        db_session, "evt_ch",
        {"id": "ch_1", "object": "charge", "payment_intent": {"id": "pi_gone", "x": 1},
         "billing_details": {"name": "Jane Roe"},
         "refunds": {"data": [{"id": "re_1", "amount": 5, "status": "succeeded",
                               "receipt_number": "R-1"}, "junk"]}},
        event_type="charge.refunded",
    )
    by_metadata = _event(
        db_session, "evt_meta", {"id": "pi_new", "metadata": {"order_id": str(order.id)},
                                 "receipt_email": "jane@example.com"},
    )
    unrelated = _event(db_session, "evt_other", _pi_object("pi_other", other_order.id))
    unparseable = WebhookEvent(
        event_id="evt_bad", event_type="x", payload="not json pi_gone", status="processed"
    )
    not_a_dict = WebhookEvent(
        event_id="evt_list", event_type="x", payload='["pi_gone"]', status="processed"
    )
    no_payload = WebhookEvent(event_id="evt_none", event_type="x", payload=None)
    no_object = WebhookEvent(
        event_id="evt_noobj", event_type="x", status="processed",
        payload='{"id": "evt_noobj", "data": {"object": "pi_gone"}}',
    )
    db_session.add_all([unparseable, not_a_dict, no_payload, no_object])
    mentions_only = _event(db_session, "evt_mention", {"id": "pi_zzz", "description": "pi_gone"})
    db_session.commit()

    assert tasks.purge_expired_retention(db_session) == 1

    assert _payload(db_session, pi_event) == {
        "id": "evt_pi", "object": "event", "type": "payment_intent.succeeded", "created": 1,
        "data": {"object": {
            "id": "pi_gone", "object": "payment_intent", "amount": 1000,
            "amount_received": 1000, "currency": "eur", "status": "succeeded",
            "metadata": {"order_id": str(order.id)},
        }},
        "redacted": True,
    }
    assert _payload(db_session, session_event)["data"]["object"] == {
        "id": "cs_gone", "object": "checkout.session", "payment_intent": "pi_gone",
        "amount_total": 1000,
    }
    assert _payload(db_session, charge_event)["data"]["object"] == {
        "id": "ch_1", "object": "charge", "payment_intent": "pi_gone",
        "refunds": {"data": [{"id": "re_1", "amount": 5, "status": "succeeded"}]},
    }
    assert "receipt_email" not in _payload(db_session, by_metadata)["data"]["object"]
    # Untouched: another customer's event, and rows that are not ours to read.
    assert "jane@example.com" in db_session.get(WebhookEvent, unrelated.id).payload
    assert db_session.get(WebhookEvent, unparseable.id).payload == "not json pi_gone"
    assert db_session.get(WebhookEvent, not_a_dict.id).payload == '["pi_gone"]'
    assert "redacted" not in db_session.get(WebhookEvent, no_object.id).payload
    assert "redacted" not in db_session.get(WebhookEvent, mentions_only.id).payload
    # The audit row stays.
    row = db_session.get(WebhookEvent, pi_event.id)
    assert (row.event_id, row.event_type, row.status) == (
        "evt_pi", "payment_intent.succeeded", "processed"
    )
    assert "Jane" not in "".join(
        r.payload for r in db_session.query(WebhookEvent).filter(
            WebhookEvent.event_id.in_(["evt_pi", "evt_cs", "evt_ch", "evt_meta"])
        )
    )


def test_redaction_is_idempotent_and_skips_pending_placeholders(db_session):
    user = _user(db_session, "p@example.com", idle_days=1)
    order = _order_with_payment(db_session, user, "pending-abc")
    row = _event(db_session, "evt_a", {"id": "pi_a", "metadata": {"order_id": str(order.id)}})

    assert stripe_events.redact_users_events(db_session, [user.id]) == 1
    first = db_session.get(WebhookEvent, row.id).payload
    assert stripe_events.redact_users_events(db_session, [user.id]) == 0
    assert db_session.get(WebhookEvent, row.id).payload == first
    assert stripe_events.redact_event_payload(first) == first


def test_redaction_without_payments_or_users_does_nothing(db_session):
    user = _user(db_session, "n@example.com", idle_days=1)
    _event(db_session, "evt_n", {"id": "pi_n"})

    assert stripe_events.redact_users_events(db_session, []) == 0
    assert stripe_events.redact_users_events(db_session, [user.id]) == 0
    assert "redacted" not in db_session.query(WebhookEvent).one().payload


def test_redacting_a_payload_without_an_object():
    assert json.loads(stripe_events.redact_event_payload('{"id": "e", "data": {}}')) == {
        "id": "e", "data": {"object": {}}, "redacted": True,
    }


def test_a_requested_erasure_redacts_the_events_too(db_session):
    user = _user(db_session, "req@example.com", idle_days=1)
    user.data_deletion_requested = True
    user.data_deletion_date = utc_now() - timedelta(days=settings.DATA_DELETION_GRACE_PERIOD_DAYS + 1)
    db_session.commit()
    order = _order_with_payment(db_session, user, "pi_req")
    row = _event(db_session, "evt_req", _pi_object("pi_req", order.id))

    assert tasks.purge_expired_deletion_requests(db_session) == 1

    assert "jane@example.com" not in db_session.get(WebhookEvent, row.id).payload
    assert _payload(db_session, row)["redacted"] is True


def test_no_stripe_customer_is_created_so_nothing_is_erased_there():
    """The app never creates a Stripe Customer, sets customer_email or puts
    personal data in metadata (only order_id): there is nothing at Stripe to
    delete through the API."""
    app_dir = pathlib.Path(tasks.__file__).parent
    source = "".join(
        p.read_text(encoding="utf-8")
        for p in (*(app_dir / "services" / "payment").glob("*.py"), app_dir / "api" / "v1" / "orders.py")
    )
    assert "customer_email" not in source
    assert "customers.create" not in source



def test_redact_users_events_redacts_by_checkout_session_id(db_session):
    user = _user(db_session, "cs_user@example.com", idle_days=1)
    _order_with_payment(db_session, user, "pending-cs1", session_id="cs_user_123")
    evt = _event(db_session, "evt_cs_123", {"id": "cs_user_123"})

    assert stripe_events.redact_users_events(db_session, [user.id]) == 1
    db_session.commit()
    assert _payload(db_session, evt)["redacted"] is True


def test_redact_users_events_handles_null_payment_intent_id(db_session, monkeypatch):
    class StubResult:
        def all(self):
            return [(None, "cs_abc", 1)]

    monkeypatch.setattr(db_session, "execute", lambda *args, **kwargs: StubResult())
    assert stripe_events.redact_users_events(db_session, [1]) == 0


def test_redact_users_events_matches_compact_json_order_id(db_session):
    user = _user(db_session, "compact@example.com", idle_days=1)
    order = _order_with_payment(db_session, user, "pi_compact")
    compact_payload = json.dumps(
        {
            "id": "evt_compact",
            "object": "event",
            "type": "payment_intent.succeeded",
            "created": 1,
            "data": {"object": {"id": "pi_other", "metadata": {"order_id": str(order.id)}}},
        },
        separators=(",", ":"),
    )
    row = WebhookEvent(
        event_id="evt_compact",
        event_type="payment_intent.succeeded",
        payload=compact_payload,
        status="processed",
    )
    db_session.add(row)
    db_session.commit()

    assert stripe_events.redact_users_events(db_session, [user.id]) == 1
    db_session.commit()
    assert _payload(db_session, row)["redacted"] is True


def test_redact_users_events_applies_sql_needle_filter(db_session, monkeypatch):
    user = _user(db_session, "sql_filter@example.com", idle_days=1)
    _order_with_payment(db_session, user, "pi_sql_filter")

    captured = []
    orig_scalars = db_session.scalars

    def spy_scalars(statement, *args, **kwargs):
        captured.append(statement)
        return orig_scalars(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "scalars", spy_scalars)
    stripe_events.redact_users_events(db_session, [user.id])

    assert len(captured) == 1
    compiled = str(captured[0].compile(compile_kwargs={"literal_binds": True}))
    assert "LIKE" in compiled or "like" in compiled


def test_redact_users_events_continues_past_malformed_json_row(db_session):
    user = _user(db_session, "corrupt@example.com", idle_days=1)
    order = _order_with_payment(db_session, user, "pi_corrupt")
    bad = WebhookEvent(
        event_id="evt_bad_json",
        event_type="payment_intent.succeeded",
        payload=f"invalid json containing pi_corrupt order_id {order.id}",
        status="processed",
    )
    db_session.add(bad)
    db_session.commit()
    good = _event(db_session, "evt_good_json", {"id": "pi_corrupt"})

    assert stripe_events.redact_users_events(db_session, [user.id]) == 1
    db_session.commit()
    assert _payload(db_session, good)["redacted"] is True


def test_redact_users_events_continues_past_already_redacted_row(db_session):
    user = _user(db_session, "already@example.com", idle_days=1)
    _order_with_payment(db_session, user, "pi_already")
    already = WebhookEvent(
        event_id="evt_already",
        event_type="payment_intent.succeeded",
        payload=json.dumps({"redacted": True, "data": {"object": {"id": "pi_already"}}}),
        status="processed",
    )
    db_session.add(already)
    db_session.commit()
    unredacted = _event(db_session, "evt_unredacted", {"id": "pi_already"})

    assert stripe_events.redact_users_events(db_session, [user.id]) == 1
    db_session.commit()
    assert _payload(db_session, unredacted)["redacted"] is True


def test_redact_users_events_continues_past_non_dict_json_row(db_session):
    user = _user(db_session, "nondict@example.com", idle_days=1)
    _order_with_payment(db_session, user, "pi_nondict")
    nondict_row = WebhookEvent(
        event_id="evt_nondict",
        event_type="payment_intent.succeeded",
        payload=json.dumps(["pi_nondict"]),
        status="processed",
    )
    db_session.add(nondict_row)
    db_session.commit()
    valid_row = _event(db_session, "evt_valid_after_nondict", {"id": "pi_nondict"})

    assert stripe_events.redact_users_events(db_session, [user.id]) == 1
    db_session.commit()
    assert _payload(db_session, valid_row)["redacted"] is True


def test_redact_users_events_counts_multiple_redactions(db_session):
    user = _user(db_session, "multi@example.com", idle_days=1)
    _order_with_payment(db_session, user, "pi_multi_1", session_id="cs_multi_2")
    e1 = _event(db_session, "evt_multi_1", {"id": "pi_multi_1"})
    e2 = _event(db_session, "evt_multi_2", {"id": "cs_multi_2"})

    assert stripe_events.redact_users_events(db_session, [user.id]) == 2
    db_session.commit()
    assert _payload(db_session, e1)["redacted"] is True
    assert _payload(db_session, e2)["redacted"] is True
