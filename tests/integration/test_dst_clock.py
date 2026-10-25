"""The night the clocks went back (2026-10-25, 01:00 UTC: local 03:00 CEST
became 02:00 CET) must change nothing here: the application stores and
computes in UTC, so every interval is an exact duration and a filter's
offset names one instant.

The project's clock seam, app.core.clock.utc_now, is replaced where each
piece of code imported it and held at 00:30Z (before the change), 01:00Z (the
change) and 01:30Z (after it). An hour of slip anywhere would show as a row on
the wrong side of its cutoff.
"""
import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import jwt
import pytest
from freezegun import freeze_time
from test_admin_orders import ADMIN, _admin_headers, _customer, _order
from test_order_expiry_time import _order as _unpaid_order

from app import tasks
from app.api.v1 import admin
from app.core.config import settings
from app.core.rate_limit import Rate, seconds_until_allowed
from app.core.security import create_access_token, create_refresh_token
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import User
from app.services import reconciliation
from app.services.payment.exceptions import PaymentProviderError

INSTANTS = {
    "before the change": datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
    "at the change": datetime(2026, 10, 25, 1, 0, tzinfo=UTC),
    "after the change": datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
}
at_instant = pytest.mark.parametrize("now", list(INSTANTS.values()), ids=list(INSTANTS))
SECOND = timedelta(seconds=1)
HOUR = timedelta(hours=1)


def _stamp(monkeypatch, module, now):
    monkeypatch.setattr(module, "utc_now", lambda: now)


# --- admin date filters: an offset names one instant --------------------------------------

def test_the_filters_tell_the_two_02_30s_of_the_night_apart(client, db_session):
    customer = _customer(db_session)
    first_pass = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)   # 02:30 CEST
    second_pass = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)  # 02:30 CET
    stamps = [first_pass, first_pass + timedelta(minutes=15),
              second_pass, second_pass + timedelta(minutes=15)]
    orders = [_order(db_session, customer, created_at=stamp).id for stamp in stamps]
    headers = _admin_headers(client, db_session)

    def ids(**params):
        response = client.get(f"{ADMIN}/orders", params=params, headers=headers)
        assert response.status_code == 200, response.text
        return {o["id"] for o in response.json()["items"]}

    cest, cet = "2026-10-25T02:30:00+02:00", "2026-10-25T02:30:00+01:00"
    assert ids(start_date=cest) == set(orders)             # 00:30Z, inclusive
    assert ids(start_date=cet) == set(orders[2:])          # 01:30Z, inclusive
    assert ids(end_date=cest) == set(orders[:1])
    assert ids(end_date=cet) == set(orders[:3])
    assert ids(start_date=cest, end_date=cet) == set(orders[:3])
    # The same instant written in UTC selects the same rows.
    assert ids(start_date="2026-10-25T01:30:00Z") == ids(start_date=cet)


@pytest.mark.parametrize("name", ["start_date", "end_date"])
def test_a_wall_time_without_its_offset_is_a_422(client, db_session, name):
    # 02:30 on that night is two instants; only an offset can choose one.
    response = client.get(f"{ADMIN}/orders", params={name: "2026-10-25T02:30:00"},
                          headers=_admin_headers(client, db_session))

    assert response.status_code == 422
    assert response.json()["errors"][0]["type"] == "timezone_aware"


# --- every cutoff is its configured duration before "now" --------------------------------

@at_instant
def test_unpaid_orders_expire_by_their_exact_window(db_session, monkeypatch, now):
    monkeypatch.setattr(tasks, "send_order_cancellation_email", lambda email, order: None)
    window = timedelta(minutes=settings.UNPAID_ORDER_EXPIRE_MINUTES)
    cases = {
        "a second past": (now - window - SECOND, "cancelled"),
        "at the cutoff": (now - window, "new"),
        "a second short": (now - window + SECOND, "new"),
        "an hour past": (now - window - HOUR, "cancelled"),
        "an hour short": (now - window + HOUR, "new"),
    }
    placed = {name: _unpaid_order(db_session, created)[0] for name, (created, _) in cases.items()}
    _stamp(monkeypatch, tasks, now)

    tasks.expire_unpaid_orders(db_session)

    db_session.expire_all()
    assert {name: db_session.get(Order, order_id).status for name, order_id in placed.items()} == {
        name: expected for name, (_, expected) in cases.items()
    }


@at_instant
def test_idempotency_keys_expire_by_their_exact_ttl(db_session, monkeypatch, now):
    ttl = timedelta(hours=settings.IDEMPOTENCY_KEY_TTL_HOURS)
    cases = {
        "a second past": (now - ttl - SECOND, True),
        "at the cutoff": (now - ttl, False),
        "a second short": (now - ttl + SECOND, False),
        "an hour past": (now - ttl - HOUR, True),
        "an hour short": (now - ttl + HOUR, False),
    }
    placed = {}
    for name, (created, _) in cases.items():
        order_id, _ = _unpaid_order(db_session, created)
        db_session.get(Order, order_id).idempotency_key = f"key-{uuid.uuid4().hex}"
        placed[name] = order_id
    db_session.commit()
    _stamp(monkeypatch, tasks, now)

    tasks.clear_expired_idempotency_keys(db_session)

    db_session.expire_all()
    assert {name: db_session.get(Order, order_id).idempotency_key is None
            for name, order_id in placed.items()} == {
        name: cleared for name, (_, cleared) in cases.items()
    }


@at_instant
def test_retention_counts_exact_24_hour_days(db_session, monkeypatch, now):
    """USER_DATA_RETENTION_DAYS is that many 24 h days, not calendar days:
    the 25-hour day of the change does not stretch it, nor does the 23-hour
    one of March shorten it."""
    period = timedelta(days=settings.USER_DATA_RETENTION_DAYS)
    cases = {
        "a second past": (now - period - SECOND, True),
        "at the cutoff": (now - period, False),
        "an hour short": (now - period + HOUR, False),
        "an hour past": (now - period - HOUR, True),
    }
    placed = {}
    for index, (name, (seen, _)) in enumerate(cases.items()):
        user = User(email=f"idle-{index}@example.com", hashed_password="x", full_name="Idle",
                    role="client", is_active=False, created_at=seen, last_login=seen,
                    last_activity_date=seen)
        db_session.add(user)
        db_session.commit()
        placed[name] = user.id
    _stamp(monkeypatch, tasks, now)

    tasks.purge_expired_retention(db_session)

    db_session.expire_all()
    assert {name: db_session.get(User, user_id).email.endswith("@deleted.invalid")
            for name, user_id in placed.items()} == {
        name: purged for name, (_, purged) in cases.items()
    }


class _Provider:
    """Records which payments the run asks about; every ask fails, which the
    run counts and goes on from."""

    def __init__(self):
        self.asked = []

    def retrieve_payment_intent(self, payment_intent_id):
        self.asked.append(payment_intent_id)
        raise PaymentProviderError("not asked for real")


@at_instant
def test_reconciliation_window_is_exact_days_and_minutes(db_session, monkeypatch, now):
    window = timedelta(days=settings.RECONCILIATION_WINDOW_DAYS)
    min_age = timedelta(minutes=settings.RECONCILIATION_MIN_AGE_MINUTES)
    cases = {
        "a second before the window": (now - window - SECOND, False),
        "the start of the window": (now - window, True),
        "an hour into the window": (now - window + HOUR, True),
        "an hour before the window": (now - window - HOUR, False),
        "the minimum age": (now - min_age, True),
        "a second too young": (now - min_age + SECOND, False),
    }
    intents = {}
    for name, (created, _) in cases.items():
        order_id, _ = _unpaid_order(db_session, created)
        intent = f"pi_{uuid.uuid4().hex}"
        db_session.add(Payment(order_id=order_id, payment_intent_id=intent, amount=500,
                               currency="usd", created_at=created))
        intents[intent] = name
    db_session.commit()
    _stamp(monkeypatch, reconciliation, now)
    provider = _Provider()

    reconciliation.reconcile_payments(db_session, provider)

    assert {intents[i] for i in provider.asked} == {n for n, (_, in_it) in cases.items() if in_it}


@at_instant
def test_token_lifetimes_are_exact(now):
    with freeze_time(now):
        access = jwt.decode(create_access_token(7), options={"verify_signature": False})
        refresh = jwt.decode(create_refresh_token(7), options={"verify_signature": False})

    assert access["exp"] == int(now.timestamp()) + settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert refresh["exp"] == int(now.timestamp()) + settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400
    assert access["iat"] == now.timestamp()


@at_instant
@pytest.mark.parametrize("period,hours", [("24h", 24), ("7d", 7 * 24), ("30d", 30 * 24)])
def test_stats_periods_are_rolling_hours_not_calendar_days(
    client, db_session, monkeypatch, now, period, hours
):
    """Decision: "7d" is the last 168 hours, "30d" the last 720, "24h" the
    last 24, whatever the length of the local days in between. A calendar
    week holding the 25-hour day would be 169 hours."""
    customer = _customer(db_session)
    span = timedelta(hours=hours)
    inside = [now - span, now - span + HOUR]
    outside = [now - span - SECOND, now - span - HOUR]
    for created in inside + outside:
        _order(db_session, customer, total="10.00", payment_status="paid", created_at=created)
    headers = _admin_headers(client, db_session)
    _stamp(monkeypatch, admin, now)

    stats = client.get(f"{ADMIN}/stats", params={"period": period}, headers=headers).json()

    assert stats["order_count"] == len(inside)
    assert Decimal(stats["total_revenue"]) == Decimal("10.00") * len(inside)


# --- the rate-limit window does not know local time ---------------------------------------

def test_a_rate_limit_window_runs_its_full_length_across_the_change():
    """A one-hour window opened at 00:30Z (02:30 CEST) closes at 01:30Z (02:30
    CET, ninety minutes later by the wall clock): Redis counts milliseconds,
    so what the caller is told is the real time left."""
    def hit(times=1):
        async def run():
            return [await seconds_until_allowed("k", Rate(2, 3600)) for _ in range(times)]
        return asyncio.run(run())

    with freeze_time("2026-10-25 00:30:00") as clock:
        assert hit(2) == [0, 0]
        clock.tick(timedelta(minutes=45))   # 01:15Z, past the change
        assert hit() == [900]               # 15 minutes left, not 75
        clock.tick(timedelta(minutes=15, seconds=1))   # 01:30:01Z
        assert hit(3) == [0, 0, 3600]
