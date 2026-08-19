from decimal import Decimal

from app import tasks
from app.core.security import get_password_hash
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


def test_cleanup_expired_carts_delegates_to_redis_service(monkeypatch):
    calls = []
    monkeypatch.setattr(RedisService, "cleanup_expired_carts", lambda self: calls.append(1) or 3)

    result = tasks.cleanup_expired_carts()

    assert result == 3
    assert calls == [1]


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

    assert schedule["cleanup-expired-carts"]["task"] == "app.tasks.cleanup_expired_carts"
    assert schedule["cleanup-expired-carts"]["schedule"] == tasks.timedelta(hours=1)

    assert schedule["cleanup-inactive-accounts"]["task"] == "app.tasks.cleanup_inactive_accounts"
    assert schedule["cleanup-inactive-accounts"]["schedule"] == tasks.timedelta(days=1)

    assert schedule["update-product-stats"]["task"] == "app.tasks.update_product_stats"
    assert schedule["update-product-stats"]["schedule"] == tasks.timedelta(hours=1)

    assert schedule["check-low-stock"]["task"] == "app.tasks.check_low_stock"
    assert schedule["check-low-stock"]["schedule"] == tasks.timedelta(hours=4)


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
