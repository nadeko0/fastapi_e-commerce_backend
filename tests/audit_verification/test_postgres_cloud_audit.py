"""PostgreSQL-only reproductions of confirmed findings of the v4.0.3 audit
(.cloud_audit/04_validation_phase2.md). Skipped without TEST_DATABASE_URL;
tests/conftest.py marks this file db_sensitive by its name (test_postgres_*).

As in test_cloud_audit.py, each test asserts the CURRENT, faulty behaviour.
Interleavings are forced, not timed: a statement hook holds one transaction
until pg_stat_activity shows the other one waiting on its lock (or the
server has ended the connection), so no test depends on a sleep.
"""

import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app import tasks
from app.core.clock import utc_now
from app.core.config import settings
from app.main import app
from app.models.user import User

_INTEGRATION = Path(__file__).resolve().parents[1] / "integration"
if str(_INTEGRATION) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION))

from conftest import (  # noqa: E402
    TEST_DB_URL,
    TestingSessionLocal,
    async_engine,
    engine,
    requires_postgres,
)
from payment_support import Shop  # noqa: E402

pytestmark = requires_postgres

WAIT_LIMIT_SECONDS = 30


def _wait_until(condition, what):
    """Poll the server until `condition()` holds; fail (not hang) after
    WAIT_LIMIT_SECONDS. Each poll is a round trip to PostgreSQL."""
    deadline = time.monotonic() + WAIT_LIMIT_SECONDS
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")


def _lock_waiters() -> int:
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND wait_event_type = 'Lock'"
        )).scalar_one()


def _race(first, second, hold_after):
    """Run `first` until it has executed a statement starting with
    `hold_after`, then start `second` and let `first` go on only once
    `second` is blocked on a row lock. Both are callables taking a TestClient;
    returns their responses."""
    results, errors = {}, []
    holding = threading.Event()

    def hold(conn, cursor, statement, *rest):
        if statement.startswith(hold_after) and not holding.is_set():
            holding.set()
            _wait_until(lambda: _lock_waiters() >= 1, "the second transaction to block")

    def run(name, action):
        try:
            with TestClient(app, raise_server_exceptions=False) as c:
                results[name] = action(c)
        except BaseException as exc:  # surfaced in the main thread
            errors.append(exc)
            holding.set()

    event.listen(async_engine.sync_engine, "after_cursor_execute", hold)
    try:
        t1 = threading.Thread(target=run, args=("first", first))
        t1.start()
        assert holding.wait(WAIT_LIMIT_SECONDS), "first transaction never reached the hook"
        t2 = threading.Thread(target=run, args=("second", second))
        t2.start()
        t1.join(2 * WAIT_LIMIT_SECONDS)
        t2.join(2 * WAIT_LIMIT_SECONDS)
    finally:
        event.remove(async_engine.sync_engine, "after_cursor_execute", hold)
    if errors:
        raise errors[0]
    return results["first"], results["second"]


def test_DB01_variant_checkout_deadlocks_plain_plus_variant_checkout(client, db_session):
    """DB-01 (2B-01), Medium. app/services/orders.py:81-91 (lock_stock_rows
    takes FOR UPDATE on products/variants) vs the FK `FOR KEY SHARE` that the
    order_items INSERT takes on the variant line's product; the deadlock is
    answered 503 (app/core/database.py:140).

    Scenario: A's cart is [variant V of P], B's cart is [P plain, V]. A locks
    V; B locks P and waits for V; A's order_items INSERT needs KEY SHARE on P
    -> PostgreSQL detects a deadlock and one checkout gets 503.

    When fixed (with_for_update(key_share=True), i.e. FOR NO KEY UPDATE),
    invert the assertion: both checkouts answer 201.
    """
    shop = Shop(client, db_session)
    product = shop.product(stock=5, name="P")
    variant = shop.variant(product, stock=5)
    a, b = shop.buyer(), shop.buyer()
    assert shop.add_to_cart(a, product, variant=variant).status_code == 200
    assert shop.add_to_cart(b, product).status_code == 200
    assert shop.add_to_cart(b, product, variant=variant).status_code == 200

    first, second = _race(
        lambda c: Shop(c, db_session).place_order(a),
        lambda c: Shop(c, db_session).place_order(b),
        hold_after="UPDATE product_variants SET stock_quantity",
    )
    assert sorted([first.status_code, second.status_code]) == [201, 503], (
        first.text, second.text)


def test_DB01_variant_checkout_deadlocks_cancel_of_plain_plus_variant_order(client, db_session):
    """DB-01 (2B-01), Medium. Same lock order defect, checkout vs the
    customer's cancel (cancel_order_and_restock locks P then V,
    app/services/orders.py:94).

    Scenario: an existing order holds [P plain, V]; while A's [V] checkout
    holds V, the order's owner cancels it -> deadlock -> one side 503.

    When fixed, invert the assertion: 201 for the checkout and 200 for the
    cancel.
    """
    shop = Shop(client, db_session)
    product = shop.product(stock=5, name="P")
    variant = shop.variant(product, stock=5)
    seller, buyer = shop.buyer(), shop.buyer()
    assert shop.add_to_cart(seller, product).status_code == 200
    assert shop.add_to_cart(seller, product, variant=variant).status_code == 200
    order_id = shop.place_order(seller).json()["id"]
    assert shop.add_to_cart(buyer, product, variant=variant).status_code == 200

    first, second = _race(
        lambda c: Shop(c, db_session).place_order(buyer),
        lambda c: Shop(c, db_session).cancel(seller, order_id),
        hold_after="UPDATE product_variants SET stock_quantity",
    )
    assert 503 in (first.status_code, second.status_code), (first.text, second.text)


def test_2B08_hard_product_delete_racing_checkout_answers_500(client, db_session):
    """2B-08, Low. app/api/v1/admin.py:339-349 (count order_items, then
    DELETE; the count is taken before a concurrent checkout's INSERT).

    Scenario: a never-ordered product is bought while an admin deletes it;
    the DELETE waits for the checkout's row lock, then hits the FK from the
    new order_items row -> IntegrityError -> 500.

    When fixed, invert the assertion: the delete answers 409 or soft-deletes
    (204) and the checkout's 201 stands.
    """
    shop = Shop(client, db_session)
    product = shop.product(stock=5, name="Doomed")
    buyer = shop.buyer()
    admin = shop.admin()
    assert shop.add_to_cart(buyer, product).status_code == 200

    checkout, delete = _race(
        lambda c: Shop(c, db_session).place_order(buyer),
        lambda c: c.delete(f"/api/v1/admin/products/{product.id}", headers=admin),
        hold_after="UPDATE products SET stock_quantity",
    )
    assert checkout.status_code == 201, checkout.text
    assert delete.status_code == 500, delete.text


def test_GDPR02_purge_dies_on_idle_in_transaction_timeout(monkeypatch):
    """GDPR-02 (2D-01 + 2D-C18), Medium. app/tasks.py:346-368 / 370-372
    (bcrypt per account while the batch transaction is open);
    app/core/database.py applies idle_in_transaction_session_timeout=30s to
    every PostgreSQL engine, the Celery one included. At BCRYPT_ROUNDS=12 a
    batch of ~95+ accounts (~50 at 13) exceeds it, every daily run.

    Scenario (scaled down): the purge connection gets a 200 ms idle timeout;
    the first hash is held until the server has ended that backend (a
    stand-in for 100 x 0.3 s of bcrypt). The purge then raises and no account
    is anonymised; the next run would start over from the same batch.

    When fixed, invert the assertion: the purge completes (returns 3) even
    when hashing takes longer than the idle timeout.
    """
    with TestingSessionLocal() as setup:
        old = utc_now() - timedelta(days=settings.DATA_DELETION_GRACE_PERIOD_DAYS + 2)
        for i in range(3):
            setup.add(User(email=f"due{i}@example.com", hashed_password="x", is_active=False,
                           data_deletion_requested=True, data_deletion_date=old))
        setup.commit()

    purge_engine = create_engine(TEST_DB_URL, poolclass=NullPool)
    states = []
    try:
        with purge_engine.connect() as conn:
            pid = conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            conn.exec_driver_sql("SET idle_in_transaction_session_timeout = '200ms'")
            conn.commit()

            def backend_state():
                with engine.connect() as probe:
                    return probe.execute(
                        text("SELECT state FROM pg_stat_activity WHERE pid = :pid"),
                        {"pid": pid}).scalar_one_or_none()

            real_hash = tasks.get_password_hash

            def slow_hash(password):
                if not states:
                    states.append(backend_state())
                    _wait_until(lambda: backend_state() is None,
                                "the server to end the idle transaction")
                return real_hash(password)

            monkeypatch.setattr(tasks, "get_password_hash", slow_hash)
            db = Session(bind=conn, autoflush=False)
            try:
                with pytest.raises(DBAPIError) as raised:
                    tasks.purge_expired_deletion_requests(db)
            finally:
                db.close()
    finally:
        purge_engine.dispose()

    assert states == ["idle in transaction"]
    assert "idle-in-transaction" in str(raised.value) or "terminat" in str(raised.value)
    with TestingSessionLocal() as check:
        assert check.query(User).filter(User.email.like("due%@example.com")).count() == 3
