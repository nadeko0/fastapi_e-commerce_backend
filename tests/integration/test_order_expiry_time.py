"""
The unpaid-order expiry (app.tasks.expire_unpaid_orders) depends on UTC
only: an order is expired when it was placed more than
UNPAID_ORDER_EXPIRE_MINUTES ago by the UTC clock, whatever the machine's
local zone, and at instants where local time misbehaves - midnight UTC,
month and year ends, a leap day, the daylight saving changes of Europe and
the US. Order.created_at is written with utc_now() and compared
with a cutoff computed the same way, both aware UTC.

freezegun's tz_offset moves the frozen local time (datetime.now,
time.localtime) without moving UTC, so each case runs with the local zone
at a range of offsets; a computation that used local time anywhere would
expire the wrong orders under at least one of them.
"""
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from freezegun import freeze_time
from time_support import local_zone

from app import tasks
from app.core.config import settings
from app.models.address import Address
from app.models.category import Category
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.product import Product
from app.models.user import User

AWKWARD_INSTANTS = {
    "midnight UTC": datetime(2026, 10, 22, 0, 0, 0, tzinfo=UTC),
    "end of a 31-day month": datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC),
    "start of a month": datetime(2026, 11, 1, 0, 10, 0, tzinfo=UTC),
    "new year": datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC),
    "leap day": datetime(2028, 2, 29, 0, 15, 0, tzinfo=UTC),
    "EU summer time begins": datetime(2026, 3, 29, 1, 0, 0, tzinfo=UTC),
    "EU summer time ends": datetime(2026, 10, 25, 1, 0, 0, tzinfo=UTC),
    "US daylight time begins": datetime(2026, 3, 8, 7, 0, 0, tzinfo=UTC),
    "US daylight time ends": datetime(2026, 11, 1, 6, 0, 0, tzinfo=UTC),
}
LOCAL_OFFSETS = [-12, -5, 0, 1, 5.5, 14]


@pytest.fixture(autouse=True)
def _sent(monkeypatch):
    sent = []
    monkeypatch.setattr(tasks, "send_order_cancellation_email",
                        lambda email, order: sent.append(order.id))
    return sent


def _order(db, created_at, stock_left=5, quantity=2):
    user = User(email=f"buyer-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x",
                full_name="Buyer", role="client", is_active=True, is_email_verified=True,
                gdpr_consent=True, privacy_policy_accepted=True)
    category = Category(name=f"c-{uuid.uuid4().hex[:6]}", path=[], level=0)
    db.add_all([user, category])
    db.commit()
    address = Address(user_id=user.id, street="1 Main St", city="City", state="ST",
                      postal_code="10001", country="US")
    product = Product(name="Widget", description="w", price=Decimal("2.50"),
                      stock_quantity=stock_left, category_id=category.id)
    db.add_all([address, product])
    db.commit()
    order = Order(user_id=user.id, status="new", payment_status="pending",
                  shipping_address_id=address.id, total_amount=Decimal("5.00"),
                  created_at=created_at, updated_at=created_at)
    db.add(order)
    db.commit()
    db.add(OrderItem(order_id=order.id, product_id=product.id, quantity=quantity,
                     price_at_time=Decimal("2.50"), created_at=created_at))
    db.commit()
    return order.id, product.id


@pytest.mark.parametrize("instant", list(AWKWARD_INSTANTS), ids=list(AWKWARD_INSTANTS))
@pytest.mark.parametrize("offset", LOCAL_OFFSETS)
def test_only_orders_older_than_the_window_by_utc_expire(
    db_session, _sent, monkeypatch, instant, offset
):
    now = AWKWARD_INSTANTS[instant]
    window = timedelta(minutes=settings.UNPAID_ORDER_EXPIRE_MINUTES)
    expired, expired_product = _order(db_session, now - window - timedelta(seconds=1))
    at_cutoff, _ = _order(db_session, now - window)
    fresh, _ = _order(db_session, now - window + timedelta(minutes=1))
    # An hour either way of the window: what a local/UTC or DST slip would be.
    hour_older, _ = _order(db_session, now - window - timedelta(hours=1))
    hour_younger, _ = _order(db_session, now - window + timedelta(hours=1))

    local_zone(monkeypatch, offset)
    with freeze_time(now, tz_offset=offset):
        cancelled = tasks.expire_unpaid_orders(db_session)

    db_session.expire_all()
    status = {order_id: db_session.get(Order, order_id).status
              for order_id in (expired, at_cutoff, fresh, hour_older, hour_younger)}
    assert status == {expired: "cancelled", at_cutoff: "new", fresh: "new",
                      hour_older: "cancelled", hour_younger: "new"}
    assert cancelled == 2
    assert sorted(_sent) == sorted([expired, hour_older])
    assert db_session.get(Product, expired_product).stock_quantity == 5 + 2


def test_the_local_zone_does_not_move_the_cutoff(db_session, monkeypatch):
    # The same order, the same UTC instant, the local zone 14 hours apart:
    # the same answer.
    now = AWKWARD_INSTANTS["EU summer time ends"]
    window = timedelta(minutes=settings.UNPAID_ORDER_EXPIRE_MINUTES)
    order_id, _ = _order(db_session, now - window + timedelta(minutes=10))

    for offset in (-12, 2, 14):
        local_zone(monkeypatch, offset)
        with freeze_time(now, tz_offset=offset):
            assert tasks.expire_unpaid_orders(db_session) == 0
    local_zone(monkeypatch, -12)
    with freeze_time(now + timedelta(minutes=11), tz_offset=-12):
        assert tasks.expire_unpaid_orders(db_session) == 1

    db_session.expire_all()
    assert db_session.get(Order, order_id).status == "cancelled"
