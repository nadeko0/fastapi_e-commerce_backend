from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from conftest import TestingSessionLocal
from freezegun import freeze_time
from hypothesis import HealthCheck, given
from hypothesis import settings as settings_hypothesis
from hypothesis import strategies as st
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from app import tasks
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
    from app.models.address import Address

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


def test_send_order_confirmation_sends_email_for_existing_order(
    db_session, tasks_db, monkeypatch
):
    sent = []
    monkeypatch.setattr(
        "app.tasks.send_order_confirmation_email",
        lambda to_email, order: sent.append((to_email, order.id)),
    )
    user = _make_verified_user(db_session)
    _, product = _make_category_and_product(db_session)
    order = _make_order_with_item(db_session, user, product)

    tasks.send_order_confirmation(order.id)

    assert sent == [(user.email, order.id)]


def test_send_order_confirmation_does_not_resend_on_redelivery(
    db_session, tasks_db, monkeypatch
):
    """Regression test: Celery's at-least-once delivery (task_acks_late +
    task_reject_on_worker_lost, and now autoretry_for on this task) means
    send_order_confirmation can legitimately run twice for the same
    order_id (worker crash after send but before ack, or a retried
    OperationalError). Without a dedup guard the customer would get the
    "Order Confirmation" email twice."""
    sent = []
    monkeypatch.setattr(
        "app.tasks.send_order_confirmation_email",
        lambda to_email, order: sent.append((to_email, order.id)),
    )
    user = _make_verified_user(db_session)
    _, product = _make_category_and_product(db_session)
    order = _make_order_with_item(db_session, user, product)

    tasks.send_order_confirmation(order.id)
    tasks.send_order_confirmation(order.id)  # simulated redelivery/retry

    assert sent == [(user.email, order.id)]


def test_send_order_confirmation_no_op_for_missing_order(db_session, tasks_db, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.tasks.send_order_confirmation_email", lambda *a, **k: sent.append(a)
    )

    tasks.send_order_confirmation(999999)

    assert sent == []


def test_send_order_status_update_sends_email_for_existing_order(
    db_session, tasks_db, monkeypatch
):
    sent = []
    monkeypatch.setattr(
        "app.tasks.send_order_status_update_email",
        lambda to_email, order: sent.append((to_email, order.id)),
    )
    user = _make_verified_user(db_session)
    _, product = _make_category_and_product(db_session)
    order = _make_order_with_item(db_session, user, product)

    tasks.send_order_status_update(order.id)

    assert sent == [(user.email, order.id)]


def test_send_order_status_update_does_not_resend_for_same_status(
    db_session, tasks_db, monkeypatch
):
    """Same redelivery/retry concern as order confirmation, but keyed on
    (order_id, status): a redelivered/retried task for the *same* status
    must not resend, but a genuine later status change must still email."""
    sent = []
    monkeypatch.setattr(
        "app.tasks.send_order_status_update_email",
        lambda to_email, order: sent.append((to_email, order.id, order.status)),
    )
    user = _make_verified_user(db_session)
    _, product = _make_category_and_product(db_session)
    order = _make_order_with_item(db_session, user, product, status="new")

    tasks.send_order_status_update(order.id)
    tasks.send_order_status_update(order.id)  # simulated redelivery/retry

    assert sent == [(user.email, order.id, "new")]

    order.status = "confirmed"
    db_session.commit()
    tasks.send_order_status_update(order.id)

    assert sent == [
        (user.email, order.id, "new"),
        (user.email, order.id, "confirmed"),
    ]


def test_send_order_confirmation_and_status_update_tasks_configure_autoretry():
    """Regression test: retry_backoff=True/max_retries=3 alone do nothing -
    Celery only honors them when the task calls self.retry() (requires
    bind=True) or when autoretry_for is set. Without autoretry_for, any
    transient DB error (e.g. a dropped connection) permanently failed the
    task on the first attempt despite looking retry-configured."""
    from sqlalchemy.exc import OperationalError

    assert tasks.send_order_confirmation.autoretry_for == (OperationalError,)
    assert tasks.send_order_status_update.autoretry_for == (OperationalError,)


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


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_send_order_confirmation_retry_after_a_db_error_still_sends_once(
    db_session, tasks_db, monkeypatch, fail_at
):
    """Regression test: the "already sent" marker was claimed before the
    order was even read. A transient OperationalError on that read - the
    very error autoretry_for is there for - triggered the retry, the retry
    found the marker and returned, and the customer never got the email.
    Whichever statement fails, the retry must send it, exactly once."""
    sent = []
    monkeypatch.setattr(
        "app.tasks.send_order_confirmation_email",
        lambda to_email, order: sent.append(
            (to_email, order.id, [i.product_name for i in order.items])
        ),
    )
    user = _make_verified_user(db_session)
    _, product = _make_category_and_product(db_session)
    order = _make_order_with_item(db_session, user, product)
    _first_session_fails_on_statement(monkeypatch, fail_at)

    result = tasks.send_order_confirmation.apply(args=(order.id,))

    assert result.successful(), result.traceback
    assert sent == [(user.email, order.id, ["Widget"])]


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_send_order_status_update_retry_after_a_db_error_still_sends_once(
    db_session, tasks_db, monkeypatch, fail_at
):
    # Same, for the status email: the marker was claimed before
    # order.user (a lazy load) was read for the recipient's address.
    sent = []
    monkeypatch.setattr(
        "app.tasks.send_order_status_update_email",
        lambda to_email, order: sent.append((to_email, order.id)),
    )
    user = _make_verified_user(db_session)
    _, product = _make_category_and_product(db_session)
    order = _make_order_with_item(db_session, user, product)
    _first_session_fails_on_statement(monkeypatch, fail_at)

    result = tasks.send_order_status_update.apply(args=(order.id,))

    assert result.successful(), result.traceback
    assert sent == [(user.email, order.id)]


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


def test_update_product_stats_caches_popular_products(db_session, tasks_db, fake_redis):
    user = _make_verified_user(db_session)
    _, product = _make_category_and_product(db_session)
    _make_order_with_item(
        db_session, user, product, quantity=4, status="confirmed", payment_status="paid"
    )

    tasks.update_product_stats()

    cached = RedisService().get("product_stats")
    assert cached is not None
    assert cached["popular_products"][0]["id"] == product.id
    assert cached["popular_products"][0]["total_quantity"] == 4


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
    assert schedule["cleanup-inactive-accounts"]["schedule"] == tasks.timedelta(days=1)

    assert schedule["update-product-stats"]["task"] == "update_product_stats"
    assert schedule["update-product-stats"]["schedule"] == tasks.timedelta(hours=1)

    assert schedule["check-low-stock"]["task"] == "check_low_stock"
    assert schedule["check-low-stock"]["schedule"] == tasks.timedelta(hours=4)


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

    assert tasks.send_order_confirmation.rate_limit == "100/m"
    assert tasks.send_order_status_update.rate_limit == "100/m"
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
    monkeypatch.setattr("app.tasks.send_order_confirmation_email", lambda *a, **k: True)
    monkeypatch.setattr("app.tasks.send_order_status_update_email", lambda *a, **k: True)
    monkeypatch.setattr("app.tasks.send_low_stock_alert_email", lambda *a, **k: True)

    tasks.send_order_confirmation(999_999)
    tasks.send_order_status_update(999_999)
    tasks.cleanup_inactive_accounts()
    tasks.update_product_stats()
    tasks.check_low_stock()

    assert len(closed) == 5, "each task that opens a session must close it"
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


# --- Email tasks: what is sent, retries, the "already sent" markers ----------


def test_send_order_confirmation_renders_the_real_email(db_session, tasks_db, monkeypatch):
    """End to end through the real message builder, only the provider
    replaced: the task hands it a freshly queried Order, not the
    OrderResponse the API passes, and that difference broke it before."""
    outbox = []

    class Capture:
        def send(self, to_email, subject, html_content):
            outbox.append((to_email, subject, html_content))
            return True

    monkeypatch.setattr("app.services.email.get_email_provider", lambda: Capture())
    user = _make_verified_user(db_session)
    _, product = _make_category_and_product(db_session)
    order = _make_order_with_item(db_session, user, product, quantity=3)

    tasks.send_order_confirmation(order.id)

    [(to_email, subject, body)] = outbox
    assert to_email == user.email
    assert subject == f"Order Confirmation #{order.id}"
    assert "Widget" in body and "<td>3</td>" in body
    assert f"Total Amount: ${order.total_amount}" in body


def test_send_order_confirmation_marker_outlives_the_redelivery_window(
    db_session, tasks_db, monkeypatch, fake_redis
):
    monkeypatch.setattr("app.tasks.send_order_confirmation_email", lambda *a: True)
    user = _make_verified_user(db_session)
    _, product = _make_category_and_product(db_session)
    order = _make_order_with_item(db_session, user, product)

    tasks.send_order_confirmation(order.id)

    ttl = fake_redis.ttl(f"email_sent:order_confirmation:{order.id}")
    visibility_timeout = tasks.celery.conf.broker_transport_options["visibility_timeout"]
    assert visibility_timeout < ttl <= tasks.EMAIL_SENT_MARKER_TTL_SECONDS


def test_send_order_confirmation_for_a_missing_order_claims_no_marker(
    db_session, tasks_db, fake_redis
):
    tasks.send_order_confirmation(424242)

    assert fake_redis.exists("email_sent:order_confirmation:424242") == 0


def test_email_tasks_still_send_when_redis_is_down(db_session, tasks_db, monkeypatch, fake_redis):
    # mark_once fails open: one possible duplicate beats a silently lost email.
    from redis.exceptions import ConnectionError as RedisConnectionError

    def unreachable(*args, **kwargs):
        raise RedisConnectionError("Redis is down")

    monkeypatch.setattr(fake_redis, "set", unreachable)
    sent = []
    monkeypatch.setattr(
        "app.tasks.send_order_confirmation_email", lambda to, order: sent.append("confirmation")
    )
    monkeypatch.setattr(
        "app.tasks.send_order_status_update_email", lambda to, order: sent.append("status")
    )
    user = _make_verified_user(db_session)
    _, product = _make_category_and_product(db_session)
    order = _make_order_with_item(db_session, user, product)

    tasks.send_order_confirmation(order.id)
    tasks.send_order_status_update(order.id)

    assert sent == ["confirmation", "status"]


def test_email_task_gives_up_after_max_retries(db_session, tasks_db, monkeypatch):
    """A database that stays down: the first attempt plus max_retries (3)
    retries, then the task fails with the database error - and the marker
    was never claimed, so a later manual re-run still sends the email."""
    attempts = []

    def broken_factory():
        attempts.append(1)
        session = TestingSessionLocal()

        @event.listens_for(session, "do_orm_execute")
        def _fail(orm_execute_state):
            raise OperationalError("SELECT ...", {}, Exception("connection refused"))

        return session

    monkeypatch.setattr("app.tasks.SessionLocal", broken_factory)
    sent = []
    monkeypatch.setattr("app.tasks.send_order_confirmation_email", lambda *a: sent.append(a))

    result = tasks.send_order_confirmation.apply(args=(7,))

    assert result.failed()
    assert isinstance(result.result, OperationalError)
    assert len(attempts) == 1 + tasks.send_order_confirmation.max_retries
    assert sent == []
    assert RedisService().mark_once("email_sent:order_confirmation:7", 60) is True


def test_email_tasks_do_not_retry_a_programming_error(db_session, tasks_db, monkeypatch):
    # autoretry_for is scoped to OperationalError: a bug fails at once.
    attempts = []

    def buggy(to_email, order):
        attempts.append(1)
        raise ValueError("template bug")

    monkeypatch.setattr("app.tasks.send_order_status_update_email", buggy)
    user = _make_verified_user(db_session)
    _, product = _make_category_and_product(db_session)
    order = _make_order_with_item(db_session, user, product)

    result = tasks.send_order_status_update.apply(args=(order.id,))

    assert result.failed()
    assert attempts == [1]


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

    monkeypatch.setattr("app.services.email.get_email_provider", lambda: Capture())
    low = _products(db_session, [1, 2, 3])

    tasks.check_low_stock(threshold=5)

    [(to_email, body)] = outbox
    assert to_email == tasks.settings.ADMIN_EMAIL
    for product in low:
        assert product.name in body


# --- update_product_stats ------------------------------------------------------

STATS_NOW = datetime(2026, 10, 20, 12, 0, 0)


def _stats_fixture(db_session):
    user = db_session.query(User).first() or _make_verified_user(db_session)
    address = db_session.query(Address).first()
    if address is None:
        address = Address(
            user_id=user.id, street="1 Main St", city="Town", state="ST",
            postal_code="10001", country="US",
        )
        db_session.add(address)
        db_session.commit()
    products = db_session.query(Product).order_by(Product.id).all() or _products(
        db_session, [10, 10, 10]
    )
    return user, address, products


def _stats_order(db_session, user, address, lines, *, status, payment_status, created_at):
    order = Order(
        user_id=user.id, shipping_address_id=address.id, status=status,
        payment_status=payment_status, created_at=created_at,
        total_amount=sum(Decimal(price) * qty for _, qty, price in lines),
    )
    db_session.add(order)
    db_session.commit()
    for product, qty, price in lines:
        db_session.add(OrderItem(
            order_id=order.id, product_id=product.id, quantity=qty, price_at_time=Decimal(price),
        ))
    db_session.commit()


@settings_hypothesis(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    orders=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=2),  # which product
            st.integers(min_value=1, max_value=9),  # quantity
            st.integers(min_value=1, max_value=10_000),  # unit price in cents
            st.sampled_from(["confirmed", "delivered", "cancelled"]),
            st.sampled_from(["paid", "pending", "refunded"]),
            st.integers(min_value=0, max_value=40 * 24),  # age in hours
        ),
        max_size=12,
    )
)
def test_update_product_stats_aggregates_exactly_the_qualifying_lines(
    db_session, tasks_db, fake_redis, orders
):
    """Popular products are the sum of quantity and of quantity *
    price_at_time per product, over paid, non-cancelled orders of the last
    30 days - compared against the same rule computed here in Python."""
    db_session.query(OrderItem).delete()
    db_session.query(Order).delete()
    db_session.commit()
    user, address, products = _stats_fixture(db_session)
    expected = {}
    for index, qty, cents, status, payment_status, age_hours in orders:
        product, price = products[index], Decimal(cents) / 100
        created_at = STATS_NOW - timedelta(hours=age_hours)
        _stats_order(
            db_session, user, address, [(product, qty, str(price))],
            status=status, payment_status=payment_status, created_at=created_at,
        )
        if status != "cancelled" and payment_status == "paid" and age_hours <= 30 * 24:
            quantity, revenue = expected.get(product.id, (0, Decimal(0)))
            expected[product.id] = (quantity + qty, revenue + price * qty)

    # Read inside the frozen clock: fakeredis dates the 1-hour TTL from it.
    with freeze_time(STATS_NOW):
        tasks.update_product_stats()
        popular = RedisService().get("product_stats")["popular_products"]

    assert {p["id"]: (p["total_quantity"], p["total_revenue"]) for p in popular} == {
        pid: (q, float(r)) for pid, (q, r) in expected.items()
    }
    quantities = [p["total_quantity"] for p in popular]
    assert quantities == sorted(quantities, reverse=True)


def test_update_product_stats_window_includes_exactly_thirty_days(
    db_session, tasks_db, fake_redis
):
    user, address, (inside, outside, _) = _stats_fixture(db_session)
    paid = {"status": "confirmed", "payment_status": "paid"}
    _stats_order(db_session, user, address, [(inside, 1, "1.00")],
                 created_at=STATS_NOW - timedelta(days=30), **paid)
    _stats_order(db_session, user, address, [(outside, 1, "1.00")],
                 created_at=STATS_NOW - timedelta(days=30, seconds=1), **paid)

    with freeze_time(STATS_NOW):
        tasks.update_product_stats()
        stats = RedisService().get("product_stats")
        ttl = fake_redis.ttl("product_stats")

    assert [p["id"] for p in stats["popular_products"]] == [inside.id]
    assert stats["updated_at"] == STATS_NOW.isoformat()
    assert 0 < ttl <= 3600


def test_update_product_stats_keeps_the_top_twenty(db_session, tasks_db, fake_redis):
    user, address, _ = _stats_fixture(db_session)
    many = _products(db_session, [10] * 22)
    for qty, product in enumerate(many, start=1):
        _stats_order(db_session, user, address, [(product, qty, "1.00")],
                     status="confirmed", payment_status="paid", created_at=datetime.utcnow())

    tasks.update_product_stats()

    popular = RedisService().get("product_stats")["popular_products"]
    assert len(popular) == 20
    assert [p["total_quantity"] for p in popular] == list(range(22, 2, -1))


def test_update_product_stats_with_no_sales_caches_an_empty_list(
    db_session, tasks_db, fake_redis
):
    tasks.update_product_stats()

    assert RedisService().get("product_stats")["popular_products"] == []
