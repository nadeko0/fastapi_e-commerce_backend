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
