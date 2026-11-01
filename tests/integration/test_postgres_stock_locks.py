import threading
import time

import pytest
from conftest import async_engine, engine, requires_postgres
from fastapi.testclient import TestClient
from payment_support import Shop
from sqlalchemy import event, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.core.security import get_password_hash
from app.main import app
from app.models.category import Category
from app.models.enums import UserRole
from app.models.user import User

ADMIN_PREFIX = "/api/v1/admin"
USERS_PREFIX = "/api/v1/users"


@pytest.fixture
def postgres_sql():
    """Every ORM statement any session executes while the fixture is active,
    as Postgres would receive it."""
    rendered = []

    def _record(state):
        rendered.append(str(state.statement.compile(dialect=postgresql.dialect())))

    event.listen(Session, "do_orm_execute", _record)
    yield rendered
    event.remove(Session, "do_orm_execute", _record)


def _make_admin(db_session, email="stock_admin@example.com"):
    admin = User(
        email=email,
        hashed_password=get_password_hash("AdminPass1"),
        full_name="Admin User",
        role=UserRole.ADMIN,
        gdpr_consent=True,
        privacy_policy_accepted=True,
        marketing_consent=False,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    return admin


def _login_admin(client, email="stock_admin@example.com"):
    response = client.post(
        f"{USERS_PREFIX}/login",
        data={"username": email, "password": "AdminPass1"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_admin_update_product_renders_for_no_key_update(client, db_session, postgres_sql):
    admin = _make_admin(db_session)
    headers = _login_admin(client, admin.email)
    category = Category(name="CatLocks", path=[], level=0)
    db_session.add(category)
    db_session.commit()
    db_session.refresh(category)

    product = client.post(
        f"{ADMIN_PREFIX}/products",
        json={
            "name": "Lock Item",
            "description": "Stock lock testing",
            "price": "15.00",
            "stock_quantity": 10,
            "category_id": category.id,
            "images": ["https://example.com/img.png"],
        },
        headers=headers,
    ).json()

    postgres_sql.clear()
    response = client.put(
        f"{ADMIN_PREFIX}/products/{product['id']}",
        json={"stock_quantity": 9},
        headers={**headers, "If-Match": "*"},
    )
    assert response.status_code == 200, response.text
    product_selects = [s for s in postgres_sql if "FROM products" in s and "FOR NO KEY UPDATE" in s]
    assert product_selects, "no SELECT with FOR NO KEY UPDATE on products was recorded"
    assert "FOR NO KEY UPDATE OF products" in product_selects[0]


def test_admin_update_variant_renders_for_no_key_update(client, db_session, postgres_sql):
    admin = _make_admin(db_session, email="variant_admin@example.com")
    headers = _login_admin(client, admin.email)
    category = Category(name="VarCatLocks", path=[], level=0)
    db_session.add(category)
    db_session.commit()
    db_session.refresh(category)

    product = client.post(
        f"{ADMIN_PREFIX}/products",
        json={
            "name": "Var Lock Item",
            "description": "Variant lock testing",
            "price": "20.00",
            "stock_quantity": 10,
            "category_id": category.id,
            "images": ["https://example.com/img.png"],
        },
        headers=headers,
    ).json()

    variant = client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": "SKU-LOCK-1", "attributes": {"size": "S"}, "stock_quantity": 5},
        headers=headers,
    ).json()

    postgres_sql.clear()
    response = client.put(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants/{variant['id']}",
        json={"stock_quantity": 4},
        headers={**headers, "If-Match": "*"},
    )
    assert response.status_code == 200, response.text
    variant_selects = [s for s in postgres_sql if "FROM product_variants" in s and "FOR NO KEY UPDATE" in s]
    assert variant_selects, "no SELECT with FOR NO KEY UPDATE on product_variants was recorded"


def test_lock_stock_rows_renders_for_no_key_update(client, db_session, postgres_sql):
    shop = Shop(client, db_session)
    product = shop.product(stock=10, name="OrderLockP")
    variant = shop.variant(product, stock=10)
    simple = shop.product(stock=10, name="OrderLockSimple")
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, simple).status_code == 200
    assert shop.add_to_cart(buyer, product, variant=variant).status_code == 200

    postgres_sql.clear()
    placed = shop.place_order(buyer)
    assert placed.status_code == 201, placed.text

    p_locks = [s for s in postgres_sql if "FROM products" in s and "FOR NO KEY UPDATE" in s]
    v_locks = [s for s in postgres_sql if "FROM product_variants" in s and "FOR NO KEY UPDATE" in s]
    assert p_locks, "no FOR NO KEY UPDATE on products during checkout"
    assert v_locks, "no FOR NO KEY UPDATE on product_variants during checkout"


@requires_postgres
def test_variant_checkout_does_not_deadlock_with_plain_plus_variant_checkout(client, db_session):
    shop = Shop(client, db_session)
    product = shop.product(stock=10, name="DeadlockPG")
    variant = shop.variant(product, stock=10)
    buyer_v = shop.buyer()
    buyer_pv = shop.buyer()

    assert shop.add_to_cart(buyer_v, product, variant=variant).status_code == 200

    results = {}
    holding = threading.Event()

    def hold(conn, cursor, statement, *rest):
        if statement.startswith("UPDATE product_variants SET stock_quantity") and not holding.is_set():
            holding.set()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                with async_engine.sync_engine.connect() as check_conn:
                    waiters = check_conn.execute(text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )).scalar_one()
                if waiters >= 1:
                    break
                time.sleep(0.02)

    def run(name, buyer):
        with TestClient(app, raise_server_exceptions=False) as c:
            results[name] = Shop(c, db_session).place_order(buyer)

    def run_pv():
        with Session(engine) as s:
            from sqlalchemy import update

            from app.models.order import Order
            from app.models.order_items import OrderItem
            from app.services.orders import lock_stock_rows
            lock_stock_rows(s, [(product.id, None), (product.id, variant.id)])
            s.execute(
                update(product.__class__)
                .where(product.__class__.id == product.id)
                .values(stock_quantity=product.__class__.stock_quantity - 1)
            )
            s.execute(
                update(variant.__class__)
                .where(variant.__class__.id == variant.id)
                .values(stock_quantity=variant.__class__.stock_quantity - 1)
            )
            order = Order(
                user_id=buyer_pv.user_id,
                shipping_address_id=buyer_pv.address_id,
                total_amount=20,
                status="new",
                payment_status="pending",
                currency="USD",
            )
            s.add(order)
            s.flush()
            s.add(OrderItem(order_id=order.id, product_id=product.id, quantity=1, price_at_time=10))
            s.add(OrderItem(order_id=order.id, product_id=product.id, variant_id=variant.id, quantity=1, price_at_time=10))
            s.commit()
            class Res:
                status_code = 201
                text = ""
            results["pv"] = Res()

    event.listen(async_engine.sync_engine, "after_cursor_execute", hold)
    try:
        t1 = threading.Thread(target=run, args=("v", buyer_v))
        t1.start()
        assert holding.wait(timeout=15), "first transaction never reached variant stock update"
        t2 = threading.Thread(target=run_pv)
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
    finally:
        event.remove(async_engine.sync_engine, "after_cursor_execute", hold)

    assert results["v"].status_code == 201, results["v"].text
    assert results["pv"].status_code == 201, results["pv"].text
    assert shop.stock(product) == 9
    assert shop.stock(variant) == 8
