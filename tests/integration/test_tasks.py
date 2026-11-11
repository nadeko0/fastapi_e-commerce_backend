from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import TestingSessionLocal
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from app import tasks
from app.core.clock import utc_now
from app.core.security import get_password_hash
from app.models.address import Address
from app.models.category import Category
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.product import Product
from app.models.user import User
from app.services.redis import RedisService


def _make_verified_user(db_session, email="buyer@example.com"):
    user = User(
        email=email,
        hashed_password=get_password_hash("Str0ngPass1"),
        full_name="Buyer Person",
        phone="+12025550123",
        gdpr_consent=True,
        privacy_policy_accepted=True,
        marketing_consent=False,
        is_active=True,
        is_email_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _make_category_and_product(db_session, stock=10):
    category = Category(name="Electronics", parent_id=None, level=0, path=[])
    db_session.add(category)
    db_session.commit()
    db_session.refresh(category)

    product = Product(
        name="Widget",
        description="A fine widget for all your widget needs",
        price=Decimal("9.99"),
        stock_quantity=stock,
        images=["https://example.com/widget.png"],
        characteristics={},
        category_id=category.id,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return category, product


def _make_order_with_item(
    db_session, user, product, quantity=2, status="new", payment_status="paid"
):

    address = Address(
        user_id=user.id,
        street="123 Main Street",
        city="Springfield",
        state="Illinois",
        postal_code="62701",
        country="US",
        is_default=True,
    )
    db_session.add(address)
    db_session.commit()
    db_session.refresh(address)

    order = Order(
        user_id=user.id,
        status=status,
        payment_status=payment_status,
        shipping_address_id=address.id,
        total_amount=product.price * quantity,
    )
    db_session.add(order)
    db_session.commit()
    db_session.refresh(order)

    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        quantity=quantity,
        price_at_time=product.price,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(order)
    return order


def _first_session_fails_on_statement(monkeypatch, fail_at):
    """The first session the task opens raises OperationalError (a dropped
    connection, a statement timeout) on its fail_at-th ORM statement, lazy
    loads included; every later session - the retry's - works."""
    real_factory = tasks.SessionLocal
    sessions = []

    def factory():
        session = real_factory()
        if not sessions:
            executed = []

            @event.listens_for(session, "do_orm_execute")
            def _fail(orm_execute_state):
                executed.append(1)
                if len(executed) == fail_at:
                    raise OperationalError("SELECT ...", {}, Exception("connection lost"))

        sessions.append(session)
        return session

    monkeypatch.setattr("app.tasks.SessionLocal", factory)


def test_no_periodic_task_sweeps_the_cart_keyspace():
    """Regression test (F-41): cleanup_expired_carts ran hourly, sent one TTL
    request per cart and guest-cart key and deleted only keys without an
    expiry. Every cart write is a SETEX (update_cart, the atomic cart
    mutations, merge_guest_cart, remove_ordered_items_atomic - their TTLs
    are asserted in tests/unit/test_redis_service.py and the cart API
    tests), so it could never find anything, while its cost grew with the
    number of guest carts. Redis expires the keys itself."""
    assert "cleanup_expired_carts" not in _registered_task_names()
    assert not hasattr(RedisService, "cleanup_expired_carts")
    for entry in tasks.celery.conf.beat_schedule.values():
        assert entry["task"] != "cleanup_expired_carts"
    assert "cleanup_expired_carts" not in tasks.celery.conf.task_annotations


def test_no_task_writes_a_product_stats_key_nothing_reads():
    """update_product_stats cached the 20 best sellers in Redis every hour
    under a key no route, task or admin view ever read: removed in 6.0.0."""
    assert "update_product_stats" not in _registered_task_names()
    assert "update-product-stats" not in tasks.celery.conf.beat_schedule


def test_check_low_stock_sends_alert_when_products_below_threshold(
    db_session, tasks_db, monkeypatch
):
    sent = []
    monkeypatch.setattr(
        "app.tasks.send_low_stock_alert_email",
        lambda to_email, products: sent.append((to_email, [p.id for p in products])),
    )
    _, low_stock_product = _make_category_and_product(db_session, stock=2)

    tasks.check_low_stock(threshold=5)

    assert len(sent) == 1
    assert sent[0][1] == [low_stock_product.id]


def test_build_redis_broker_url_includes_auth_when_password_set():
    url = tasks.build_redis_broker_url("redis-host", 6379, 0, "s3cret")

    assert url == "redis://:s3cret@redis-host:6379/0"


def test_build_redis_broker_url_omits_auth_when_password_none():
    url = tasks.build_redis_broker_url("redis-host", 6379, 0, None)

    assert url == "redis://redis-host:6379/0"
    assert "@" not in url


def test_build_redis_broker_url_omits_auth_when_password_empty_string():
    url = tasks.build_redis_broker_url("redis-host", 6379, 0, "")

    assert url == "redis://redis-host:6379/0"
    assert "@" not in url


def test_build_redis_broker_url_escapes_special_characters_in_password():
    # A password containing URL-delimiter characters (@, :, /) must not
    # split the userinfo segment early or otherwise corrupt the host/port
    # Celery ends up connecting to.
    url = tasks.build_redis_broker_url("redis-host", 6379, 0, "p@ss:w/rd#1%")

    assert url == "redis://:p%40ss%3Aw%2Frd%231%25@redis-host:6379/0"

    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    assert parsed.hostname == "redis-host"
    assert parsed.port == 6379
    assert unquote(parsed.password) == "p@ss:w/rd#1%"


def test_beat_schedule_registers_all_periodic_tasks():
    schedule = tasks.celery.conf.beat_schedule

    assert schedule["cleanup-inactive-accounts"]["task"] == "cleanup_inactive_accounts"
    assert schedule["cleanup-inactive-accounts"]["schedule"] == tasks.crontab(hour=2, minute=30)

    assert schedule["check-low-stock"]["task"] == "check_low_stock"
    assert schedule["check-low-stock"]["schedule"] == tasks.timedelta(hours=4)


def test_daily_erasure_purge_runs_at_a_fixed_time_not_per_beat_start():
    """A timedelta schedule counts from the start of beat ("relative to the
    time when celery beat was started", Celery periodic-tasks docs), and
    the schedule file sits in the container's /tmp, so every container
    re-create (each deploy) pushed the GDPR purge a full day back. A
    crontab is tied to the clock and does not move."""
    entry = tasks.celery.conf.beat_schedule["cleanup-inactive-accounts"]

    assert isinstance(entry["schedule"], tasks.crontab)
    assert entry["schedule"] == tasks.crontab(hour=2, minute=30)


def _registered_task_names():
    return {name for name in tasks.celery.tasks if not name.startswith("celery.")}


def test_every_beat_entry_names_a_task_the_worker_actually_has():
    """Regression test. The schedule used to say 'app.tasks.<function>' while
    every task is registered under an explicit short name, so beat published
    four tasks no worker knew and none of the periodic jobs ever ran. The
    previous version of the test above compared the schedule with those same
    wrong strings, so it passed. Comparing against the registry is the check
    that cannot drift."""
    registered = _registered_task_names()

    for entry_name, entry in tasks.celery.conf.beat_schedule.items():
        assert entry["task"] in registered, (
            f"beat entry {entry_name!r} schedules {entry['task']!r}, "
            f"which is not a registered task ({sorted(registered)})"
        )


def test_routes_and_annotations_match_real_tasks():
    import fnmatch

    registered = _registered_task_names()

    for pattern in tasks.celery.conf.task_routes:
        assert fnmatch.filter(registered, pattern), (
            f"task_routes pattern {pattern!r} matches no registered task"
        )
    # task_annotations is an exact-name lookup (or '*'); a glob there is
    # silently ignored, which is how the rate limits went unapplied.
    for name in tasks.celery.conf.task_annotations:
        assert name == "*" or name in registered, (
            f"task_annotations key {name!r} is not a registered task name"
        )


def test_rate_limits_are_actually_applied_to_the_tasks():
    tasks.celery.finalize()

    assert tasks.cleanup_inactive_accounts.rate_limit == "10/m"


def test_tasks_close_their_database_session(db_session, tasks_db, monkeypatch):
    """Regression test. Tasks used to take a session with next(get_db()) and
    never close it, leaving the connection idle in an open transaction after
    every run (seen on a real worker against Postgres: 6 such connections
    after 14 task runs, reaped only by the server's 30 s timeout)."""
    closed = []
    real_factory = tasks.SessionLocal

    def tracking_factory():
        session = real_factory()
        original_close = session.close

        def close():
            closed.append(session)
            original_close()

        session.close = close
        return session

    monkeypatch.setattr("app.tasks.SessionLocal", tracking_factory)
    monkeypatch.setattr("app.tasks.send_low_stock_alert_email", lambda *a, **k: True)

    tasks.cleanup_inactive_accounts()
    tasks.check_low_stock()

    assert len(closed) == 2, "each task that opens a session must close it"
    for session in closed:
        assert not session.in_transaction()


def test_check_low_stock_sends_nothing_when_all_products_well_stocked(
    db_session, tasks_db, monkeypatch
):
    sent = []
    monkeypatch.setattr(
        "app.tasks.send_low_stock_alert_email", lambda *a, **k: sent.append(a)
    )
    _make_category_and_product(db_session, stock=100)

    tasks.check_low_stock(threshold=5)

    assert sent == []




# --- check_low_stock -------------------------------------------------------------


def _products(db_session, stocks, active=True):
    category = db_session.query(Category).first()
    if category is None:
        category = Category(name="Shelf", parent_id=None, level=0, path=[])
        db_session.add(category)
        db_session.commit()
    made = []
    for i, stock in enumerate(stocks):
        product = Product(
            name=f"P{len(made)}-{stock}-{active}-{i}", description="A product for the stock alert",
            price=Decimal("1.00"), stock_quantity=stock, images=[], characteristics={},
            category_id=category.id, is_active=active,
        )
        db_session.add(product)
        made.append(product)
    db_session.commit()
    return made


@pytest.mark.parametrize("threshold", [0, 1, 5, 10])
def test_check_low_stock_alerts_at_or_below_the_threshold(
    db_session, tasks_db, monkeypatch, threshold
):
    alerts = []
    monkeypatch.setattr(
        "app.tasks.send_low_stock_alert_email",
        lambda to_email, products: alerts.append((to_email, sorted(p.id for p in products))),
    )
    at, below, above = _products(db_session, [threshold, max(threshold - 1, 0), threshold + 1])

    tasks.check_low_stock(threshold=threshold)

    expected = sorted({at.id, below.id})
    assert alerts == [(tasks.settings.ADMIN_EMAIL, expected)]
    assert above.id not in alerts[0][1]


def test_check_low_stock_ignores_inactive_products(db_session, tasks_db, monkeypatch):
    alerts = []
    monkeypatch.setattr(
        "app.tasks.send_low_stock_alert_email", lambda to, products: alerts.append(products)
    )
    _products(db_session, [0, 1], active=False)

    tasks.check_low_stock()

    assert alerts == []


def test_check_low_stock_default_threshold_is_five(db_session, tasks_db, monkeypatch):
    alerts = []
    monkeypatch.setattr(
        "app.tasks.send_low_stock_alert_email",
        lambda to, products: alerts.append(sorted(p.stock_quantity for p in products)),
    )
    _products(db_session, [5, 6, 0])

    tasks.check_low_stock()

    assert alerts == [[0, 5]]


def test_low_stock_alert_lists_every_low_product_in_one_email(
    db_session, tasks_db, monkeypatch
):
    outbox = []

    class Capture:
        def send(self, to_email, subject, html_content):
            outbox.append((to_email, html_content))
            return True

    monkeypatch.setattr("app.services.email.get_email_provider", Capture)
    low = _products(db_session, [1, 2, 3])

    tasks.check_low_stock(threshold=5)

    [(to_email, body)] = outbox
    assert to_email == tasks.settings.ADMIN_EMAIL
    for product in low:
        assert product.name in body


# -- No database transaction is open while an email is handed to SMTP --------
#
# The SMTP send is a synchronous call to a server that may be slow. A session
# with an open transaction holds a connection "idle in transaction" for the
# whole wait, and PostgreSQL ends such a connection after
# idle_in_transaction_session_timeout (30 s here): the next query of the task
# then fails. With expire_on_commit=True, reading order.user after the commit
# reloads it and opens a new transaction, which is how expire_unpaid_orders
# did exactly that.

def _record_session_state(monkeypatch, sender_name):
    """Patch task_session to remember its session, and the email sender to
    record session.in_transaction() at the moment it is called."""
    from contextlib import contextmanager

    sessions = []
    in_transaction = []

    @contextmanager
    def recording_session():
        db = TestingSessionLocal()
        sessions.append(db)
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr("app.tasks.task_session", recording_session)
    monkeypatch.setattr(
        f"app.tasks.{sender_name}",
        lambda *a, **k: in_transaction.append(sessions[-1].in_transaction()),
    )
    return in_transaction


def test_expire_unpaid_orders_sends_no_email_inside_a_transaction(db_session, monkeypatch):
    user = _make_verified_user(db_session)
    _, product = _make_category_and_product(db_session)
    order = _make_order_with_item(db_session, user, product, payment_status="pending")
    order_id = order.id
    order.created_at = utc_now() - timedelta(days=2)
    db_session.commit()

    seen = []

    def fake_send(to_email, order):
        # What the email is built from, read here as the real sender does.
        seen.append((
            db_session.in_transaction(), to_email, order.id, order.status,
            order.payment_status, order.total_amount, order.currency,
            order.created_at,
        ))
        return True

    monkeypatch.setattr("app.tasks.send_order_cancellation_email", fake_send)

    assert tasks.expire_unpaid_orders(db_session) == 1
    assert len(seen) == 1
    in_transaction, to_email, sent_id, status, payment_status, total, currency, created = seen[0]
    assert in_transaction is False
    assert (to_email, sent_id) == (user.email, order_id)
    assert (status, payment_status) == ("cancelled", "pending")
    assert total == Decimal("19.98")
    assert currency and created




def test_check_low_stock_sends_no_email_inside_a_transaction(
    db_session, tasks_db, monkeypatch
):
    in_transaction = _record_session_state(monkeypatch, "send_low_stock_alert_email")
    _make_category_and_product(db_session, stock=2)

    tasks.check_low_stock(threshold=5)

    assert in_transaction == [False]
