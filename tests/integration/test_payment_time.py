"""
Time in the payment chain. The app stores aware UTC (app.core.clock.utc_now) and
must not depend on the machine's local time zone anywhere: order and
attempt timestamps, the in-flight window of a payment attempt
(PAYMENT_ATTEMPT_STALE_AFTER_MINUTES), the webhook signature's timestamp
tolerance (STRIPE_WEBHOOK_TOLERANCE_SECONDS), refunds, and the expiry of
unpaid orders (UNPAID_ORDER_EXPIRE_MINUTES).

Two checks:
- the payment modules call no local-time API at all (static: the source
  is read, nothing is imported or run);
- the chain is driven with the clock frozen at awkward instants - just
  before midnight UTC, the end of a month and of a year, a leap day, and
  the moments the EU and US clocks change - and the local zone set far
  from UTC: naive datetime.now() and time.localtime() then differ from
  UTC by up to 14 hours (see _local_zone). Each window is then crossed by
  a minute either way and must hold to the minute, whatever the local
  offset; a local-time read anywhere in the chain would be hours off.
"""
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from freezegun import freeze_time
from payment_support import (
    PAY_3DS,
    Shop,
    order_state,
    payment_rows,
    reset_provider,
    use_production_sessions,
)
from sqlalchemy import event
from sqlalchemy.orm import Session
from time_support import local_zone

from app import tasks
from app.core.clock import utc_now
from app.core.config import settings
from app.services.payment.stripe_provider import StripePaymentProvider

ROOT = Path(__file__).resolve().parents[2]
PAYMENT_MODULES = [
    "app/api/v1/orders.py",
    "app/services/orders.py",
    "app/tasks.py",
    "app/models/order.py",
    "app/models/payment.py",
    "app/models/payment_ledger.py",
    *sorted(str(p.relative_to(ROOT)).replace("\\", "/")
            for p in (ROOT / "app/services/payment").glob("*.py")),
]

# Calls that read or convert to the local time zone. datetime.now(tz) and
# fromtimestamp(ts, tz) with an explicit zone would be fine; none is used.
LOCAL_TIME_CALLS = re.compile(
    r"datetime\.now\(\s*\)|date\.today\(|time\.localtime\(|time\.mktime\(|"
    r"\.fromtimestamp\(\s*[^,()]+\)|\.astimezone\(\s*\)|time\.strftime\("
)

# (instant, what is awkward about it). Each scenario runs from 30 s before
# the instant, so its steps fall on both sides of it.
INSTANTS = [
    ("2026-12-31 23:59:30", "end of a year"),
    ("2027-02-28 23:59:30", "end of a month"),
    ("2028-02-29 23:59:30", "leap day"),
    ("2027-03-28 00:59:30", "EU clocks go forward (01:00 UTC)"),
    ("2027-10-31 00:59:30", "EU clocks go back (01:00 UTC)"),
    ("2027-03-14 06:59:30", "US Eastern clocks go forward (07:00 UTC)"),
    ("2027-11-07 05:59:30", "US Eastern clocks go back (06:00 UTC)"),
]
LOCAL_OFFSETS = [-10, 14]


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def _row_clocks_follow_freezegun():
    """Columns default to utc_now (created_at, updated_at). SQLAlchemy
    holds that function object, and its compiled statements keep it, where
    freezegun cannot reach it (it patches the names modules look up): rows
    would be stamped with the real clock. Every flush stamps them here
    instead, through a lookup made when it runs."""
    def _stamp(session, _context, _instances):
        for obj in session.new:
            if hasattr(obj, "created_at") and obj.created_at is None:
                obj.created_at = utc_now()
            if hasattr(obj, "updated_at") and obj.updated_at is None:
                obj.updated_at = utc_now()
        for obj in session.dirty:
            if hasattr(obj, "updated_at") and session.is_modified(obj):
                obj.updated_at = utc_now()

    event.listen(Session, "before_flush", _stamp)
    yield
    event.remove(Session, "before_flush", _stamp)


@pytest.fixture
def shop(client, db_session, monkeypatch, _row_clocks_follow_freezegun):
    use_production_sessions(monkeypatch)
    for name in ("send_order_confirmation_email", "send_order_status_update_email",
                 "send_order_cancellation_email"):
        monkeypatch.setattr(f"app.api.v1.orders.{name}", lambda *a, **k: True)
    monkeypatch.setattr("app.tasks.send_order_cancellation_email", lambda *a, **k: True)
    return Shop(client, db_session)


@pytest.mark.parametrize("module", PAYMENT_MODULES)
def test_the_payment_chain_reads_no_local_time(module):
    source = (ROOT / module).read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())

    assert LOCAL_TIME_CALLS.findall(code) == []


def _instant(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


@pytest.mark.parametrize("offset", LOCAL_OFFSETS)
@pytest.mark.parametrize("instant,_why", INSTANTS, ids=[i[1] for i in INSTANTS])
def test_payment_windows_hold_to_the_minute_across_awkward_instants(
    shop, tasks_db, monkeypatch, instant, _why, offset
):
    start = _instant(instant)
    local_zone(monkeypatch, offset)
    with freeze_time(start, tz_offset=offset) as clock:
        # The zone really is `offset` hours away, for local readings only.
        assert datetime.now() - utc_now().replace(tzinfo=None) == timedelta(hours=offset)
        assert time.localtime().tm_hour == (start + timedelta(hours=offset)).hour
        product = shop.product(price="10.00", stock=5)
        buyer = shop.buyer()
        admin = shop.admin()
        order = shop.order_of(buyer, product)
        stored = shop.order(order["id"])
        assert stored.created_at == start
        assert shop.pay(buyer, order["id"], token=PAY_3DS).status_code == 200
        attempt = shop.payments(order["id"])[0]
        assert attempt.created_at == start

        # An attempt in 3D Secure blocks another until it goes stale.
        stale = timedelta(minutes=settings.PAYMENT_ATTEMPT_STALE_AFTER_MINUTES)
        clock.move_to(start + stale - timedelta(seconds=1))
        assert shop.pay(buyer, order["id"]).status_code == 409
        clock.move_to(start + stale + timedelta(seconds=1))
        second = shop.pay(buyer, order["id"], token=PAY_3DS)
        assert second.status_code == 200, second.text
        clock.move_to(start + stale - timedelta(seconds=1))

        # The customer completes 3D Secure at Stripe.
        StripePaymentProvider().confirm_payment_intent(
            attempt.payment_intent_id, return_url="https://shop.test/return")
        # A webhook signed just inside the tolerance is accepted, one signed
        # just outside it is refused - on the frozen UTC clock.
        now = start + stale - timedelta(seconds=1)
        tolerance = settings.STRIPE_WEBHOOK_TOLERANCE_SECONDS
        signed_at = int((now - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds())
        too_old = shop.webhook("payment_intent.succeeded", _intent(attempt),
                               timestamp=signed_at - tolerance - 1)
        assert too_old.status_code == 400
        accepted = shop.webhook("payment_intent.succeeded", _intent(attempt),
                                timestamp=signed_at - tolerance + 1)
        assert accepted.status_code == 200, accepted.text
        assert order_state(shop, order["id"]) == ("confirmed", "paid")
        assert shop.order(order["id"]).updated_at == now

        # A refund a minute later, across the instant.
        clock.move_to(now + timedelta(minutes=1))
        refund = shop.refund(admin, order["id"], amount="4.00")
        assert refund.status_code == 200, refund.text
        assert payment_rows(shop, order["id"]) == [
            ("succeeded", 1000, 400, "USD", False), ("requires_action", 1000, 0, "USD", False)]

        # An unpaid order expires UNPAID_ORDER_EXPIRE_MINUTES after it was
        # placed: not a second before, and at the next run after.
        clock.move_to(start)
        unpaid = shop.order_of(shop.buyer(), product)
        expire = timedelta(minutes=settings.UNPAID_ORDER_EXPIRE_MINUTES)
        clock.move_to(start + expire - timedelta(seconds=1))
        assert tasks.cancel_expired_unpaid_orders() == 0
        assert order_state(shop, unpaid["id"]) == ("new", "pending")
        clock.move_to(start + expire + timedelta(seconds=1))
        assert tasks.cancel_expired_unpaid_orders() == 1
        assert order_state(shop, unpaid["id"]) == ("cancelled", "pending")
        assert shop.stock(product) == 4  # the paid order's unit only


def _intent(attempt):
    return {"id": attempt.payment_intent_id, "object": "payment_intent",
            "amount": attempt.amount, "amount_received": attempt.amount,
            "currency": attempt.currency.lower(),
            "metadata": {"order_id": str(attempt.order_id)}}
