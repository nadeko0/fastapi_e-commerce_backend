"""Behaviour that only exists on a real PostgreSQL server.

The rest of the suite is written to pass on SQLite and on PostgreSQL alike;
these tests check what SQLite cannot show: that the order row lock in the pay
path really makes a second payment wait, that the CHECK constraints, the
unique index on lower(email) and the one default address per user are
enforced by the database itself, and that the
Alembic migrations run forward, back to nothing and forward again.

They are skipped unless TEST_DATABASE_URL points at a server (see
tests/conftest.py); CI's test-postgres job runs them.
"""
import contextlib
import os
import subprocess
import sys
import threading
import time
import uuid

import pytest
from conftest import (
    TEST_DATABASE_URL,
    TestingSessionLocal,
    async_engine,
    engine,
    requires_postgres,
)
from fastapi.testclient import TestClient
from payment_support import Shop
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from test_check_constraints import (
    STATUS_REVISION,
    order_graph,  # noqa: F401  (fixture)
)
from test_check_constraints import _migration as _check_constraints_migration
from test_payment import (
    ORDERS_PREFIX,
    _create_verified_buyer_with_order,
    _get_order,
    _get_payments,
    _payment_payload,
)

from app.main import app
from app.models.address import Address
from app.models.base import Base
from app.models.user import User
from app.services.payment.stripe_provider import StripePaymentProvider

pytestmark = requires_postgres

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _reset_stripe_mock_state():
    StripePaymentProvider.reset_mock_state()
    yield
    StripePaymentProvider.reset_mock_state()


@pytest.fixture(autouse=True)
def _stub_order_confirmation_email(monkeypatch):
    monkeypatch.setattr("app.api.v1.orders.send_order_confirmation_email", lambda *a, **k: True)
    return


# -- The order row lock in the pay path --------------------------------------

def _blocked_on_a_lock():
    """How many backends are waiting for a lock right now."""
    with engine.connect() as connection:
        return connection.execute(text(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND wait_event_type = 'Lock'"
        )).scalar_one()


def _wait_until(condition, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def _pay(client, setup, results, key):
    order = setup["order"]
    headers = {**setup["headers"], "Idempotency-Key": key}
    results[key] = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay", json=_payment_payload(order), headers=headers
    )


def test_pay_waits_while_another_connection_holds_the_order_row(client, db_session):
    setup = _create_verified_buyer_with_order(client, db_session)
    order_id = setup["order"]["id"]

    # Connection A: the lock another payment, a webhook or an admin holds
    # while it decides what to do with the order.
    # Holder takes FOR KEY SHARE, which only conflicts with FOR UPDATE:
    # if with_for_update() were removed from the pay path, the foreign key's
    # KEY SHARE check on payments INSERT would not conflict with this lock,
    # so /pay would not block and would write a payment row.
    holder = TestingSessionLocal()
    holder.execute(text("SELECT id FROM orders WHERE id = :id FOR KEY SHARE"), {"id": order_id})

    results = {}
    hook_fired = threading.Event()

    def hook(conn, cursor, statement, *rest):
        if "from orders" in statement.lower() and "for update" in statement.lower():
            hook_fired.set()

    hooked = async_engine.sync_engine
    event.listen(hooked, "after_cursor_execute", hook)
    try:
        thread = threading.Thread(target=_pay, args=(client, setup, results, uuid.uuid4().hex))
        thread.start()
        try:
            assert _wait_until(lambda: _blocked_on_a_lock() >= 1), "/pay never waited for the lock"
            time.sleep(0.5)
            assert thread.is_alive(), "/pay answered while another connection held the order row"
            db_session.expire_all()
            assert not _get_payments(
                db_session, order_id
            ), "/pay wrote a payment before acquiring the order lock"
            assert _get_order(db_session, order_id).payment_status != "paid"
            with engine.connect() as conn:
                ungranted = conn.execute(
                    text("SELECT count(*) FROM pg_locks WHERE NOT granted")
                ).scalar_one()
                assert ungranted >= 1, "no ungranted lock found in pg_locks"
        finally:
            holder.commit()
            holder.close()
        thread.join(timeout=30)
    finally:
        event.remove(hooked, "after_cursor_execute", hook)

    assert not thread.is_alive()
    assert hook_fired.is_set(), "the SELECT ... FOR UPDATE hook never fired"
    (response,) = results.values()
    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert _get_order(db_session, order_id).payment_status == "paid"


def test_two_concurrent_payments_for_one_order_charge_it_once(client, db_session):
    setup = _create_verified_buyer_with_order(client, db_session)
    order_id = setup["order"]["id"]

    results = {}
    hook_fired = threading.Event()
    held = threading.Event()
    errors = []

    def hold(conn, cursor, statement, *rest):
        if "from orders" in statement.lower() and "for update" in statement.lower():
            hook_fired.set()
            if not held.is_set():
                held.set()
                assert _wait_until(
                    lambda: _blocked_on_a_lock() >= 1
                ), "second payment never blocked on the order lock"

    def attempt(key):
        try:
            with TestClient(app) as own_client:
                _pay(own_client, setup, results, key)
        except BaseException as exc:  # noqa: BLE001 - worker thread: collected and asserted on by the test
            errors.append(exc)

    hooked = async_engine.sync_engine
    event.listen(hooked, "after_cursor_execute", hold)
    try:
        t1 = threading.Thread(target=attempt, args=(uuid.uuid4().hex,))
        t1.start()
        assert hook_fired.wait(timeout=15), "the SELECT ... FOR UPDATE hook never fired"
        t2 = threading.Thread(target=attempt, args=(uuid.uuid4().hex,))
        t2.start()
        t1.join(timeout=60)
        t2.join(timeout=60)
    finally:
        event.remove(hooked, "after_cursor_execute", hold)

    if errors:
        raise errors[0]
    assert not any(thread.is_alive() for thread in (t1, t2))
    assert hook_fired.is_set(), (
        "the SELECT ... FOR UPDATE hook never fired: the interleaving was not forced"
    )

    codes = sorted(response.status_code for response in results.values())
    # One wins; the other is a 409: the order is paid, or a payment is in flight.
    assert codes == [200, 409], [r.text for r in results.values()]

    db_session.expire_all()
    order = _get_order(db_session, order_id)
    assert order.payment_status == "paid"
    succeeded = [p for p in _get_payments(db_session, order_id) if p.status == "succeeded"]
    assert len(succeeded) == 1


# -- Stock rows are locked in one order --------------------------------------

def _run_holding_after_first_stock_update(actions):
    """Run the callables in threads of their own, each held right after its
    first stock UPDATE until the others have reached the same point (or 3
    seconds have passed, which is what happens when the rows are locked in
    one order: the second one is then still waiting for the first one's
    lock). Returns their results by index."""
    meeting = threading.Barrier(len(actions))
    seen = set()
    results = {}

    def hold(connection, cursor, statement, *rest):
        thread = threading.get_ident()
        if statement.startswith("UPDATE products SET stock_quantity") and thread not in seen:
            seen.add(thread)
            with contextlib.suppress(threading.BrokenBarrierError):
                meeting.wait(timeout=3)

    def run(index):
        # A client of its own per request: requests through one client run
        # one after the other on its event loop and could never overlap.
        with TestClient(app) as own_client:
            results[index] = actions[index](own_client)

    # The routes run on the AsyncSession: their statements go through
    # async_engine's sync engine, not `engine` (a hook there never fired, and
    # the tests depended on thread timing instead of this barrier).
    hooked = async_engine.sync_engine
    event.listen(hooked, "after_cursor_execute", hold)
    try:
        threads = [threading.Thread(target=run, args=(i,)) for i in range(len(actions))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
    finally:
        event.remove(hooked, "after_cursor_execute", hold)
    assert not any(thread.is_alive() for thread in threads)
    assert len(seen) == len(actions), (
        "the hold never fired: the interleaving was not forced"
    )
    return results


def test_placing_an_order_waits_for_another_placement_by_the_same_user(client, db_session):
    # Requests with different keys used to read the same cart and the same
    # count of unpaid orders side by side: eight orders from one cart, past
    # the cap. The user's row is the lock that puts them in line.
    shop = Shop(client, db_session)
    product = shop.product(stock=5)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product).status_code == 200

    holder = TestingSessionLocal()
    holder.execute(text("SELECT id FROM users WHERE id = :id FOR NO KEY UPDATE"),
                   {"id": buyer.user_id})
    results = {}
    thread = threading.Thread(target=lambda: results.update(r=shop.place_order(buyer)))
    thread.start()
    try:
        assert _wait_until(lambda: _blocked_on_a_lock() >= 1), "POST /orders never waited"
        time.sleep(0.3)
        assert thread.is_alive(), "POST /orders went on while another placement held the user"
        assert shop.stock(product) == 5
    finally:
        holder.commit()
        holder.close()
    thread.join(timeout=30)

    assert results["r"].status_code == 201, results["r"].text
    assert shop.stock(product) == 4


def test_changing_the_default_address_waits_for_another_address_change_of_the_user(
    client, db_session
):
    # Two requests moving the default each updated the other's rows and could
    # deadlock (40P01, a 500). The user's row puts them in line.
    shop = Shop(client, db_session)
    buyer = shop.buyer()
    second = client.post("/api/v1/users/addresses", headers=buyer.headers, json={
        "street": "2 Side St", "city": "Town", "state": "ST", "postal_code": "10002",
        "country": "US",
    })
    assert second.status_code == 201, second.text

    holder = TestingSessionLocal()
    holder.execute(text("SELECT id FROM users WHERE id = :id FOR NO KEY UPDATE"),
                   {"id": buyer.user_id})
    results = {}
    thread = threading.Thread(target=lambda: results.update(r=client.post(
        "/api/v1/users/addresses/default", headers=buyer.headers,
        json={"address_id": second.json()["id"]})))
    thread.start()
    try:
        assert _wait_until(lambda: _blocked_on_a_lock() >= 1), "set-default never waited"
        time.sleep(0.3)
        assert thread.is_alive(), "set-default went on while another change held the user"
    finally:
        holder.commit()
        holder.close()
    thread.join(timeout=30)

    assert results["r"].status_code == 200, results["r"].text


def _cart_with(shop, buyer, products):
    for product in products:
        assert shop.add_to_cart(buyer, product).status_code == 200


def test_orders_with_the_same_products_in_opposite_order_do_not_deadlock(client, db_session):
    # Two checkouts whose carts hold the same two products in opposite
    # order. Each is held, right after its first stock UPDATE, until the
    # other has reached the same point: unsorted, each then owns one row and
    # waits for the other's (SQLSTATE 40P01, PostgreSQL aborts one, a 500).
    shop = Shop(client, db_session)
    first, second = shop.product(stock=5, name="A"), shop.product(stock=5, name="B")
    buyers = [shop.buyer(), shop.buyer()]
    _cart_with(shop, buyers[0], (first, second))
    _cart_with(shop, buyers[1], (second, first))

    results = _run_holding_after_first_stock_update([
        lambda c: Shop(c, db_session).place_order(buyers[0]),
        lambda c: Shop(c, db_session).place_order(buyers[1]),
    ])

    assert [results[i].status_code for i in range(2)] == [201, 201], [r.text for r in results.values()]
    assert shop.stock(first) == 3 and shop.stock(second) == 3


@pytest.mark.parametrize("checkout_first_then_second", [True, False])
def test_a_cancellation_and_an_order_with_the_same_products_in_opposite_order_do_not_deadlock(
    client, db_session, checkout_first_then_second
):
    # The restock of a cancelled order against a checkout of the same two
    # products, the cart in either order: whichever order the restock reads
    # the lines in, one of the two is the opposite one.
    shop = Shop(client, db_session)
    first, second = shop.product(stock=5, name="A"), shop.product(stock=5, name="B")
    seller, buyer = shop.buyer(), shop.buyer()
    _cart_with(shop, seller, (first, second))
    order_id = shop.place_order(seller).json()["id"]
    _cart_with(shop, buyer, (first, second) if checkout_first_then_second else (second, first))

    results = _run_holding_after_first_stock_update([
        lambda c: Shop(c, db_session).cancel(seller, order_id),
        lambda c: Shop(c, db_session).place_order(buyer),
    ])

    assert [results[i].status_code for i in range(2)] == [200, 201], [r.text for r in results.values()]
    # 5 - 1 (first order) + 1 (cancelled) - 1 (second order)
    assert shop.stock(first) == 4 and shop.stock(second) == 4


# -- Constraints enforced by the database ------------------------------------

@pytest.mark.parametrize(
    "statement,constraint",
    [
        ("UPDATE products SET price = -0.01", "ck_product_price_non_negative"),
        ("UPDATE products SET stock_quantity = -1", "ck_product_stock_non_negative"),
        ("UPDATE product_variants SET stock_quantity = -1", "ck_product_variant_stock_non_negative"),
        ("UPDATE order_items SET quantity = 0", "ck_order_item_quantity_positive"),
        ("UPDATE orders SET total_amount = -1", "ck_order_total_non_negative"),
        ("UPDATE payments SET amount_refunded = amount + 1", "ck_payment_refunded_within_amount"),
        ("UPDATE payments SET status = 'canceled'", "ck_payment_attempt_status"),
    ],
)
def test_the_database_rejects_a_bad_row_and_names_the_constraint(db_session, order_graph, statement, constraint):  # noqa: F811
    with pytest.raises(IntegrityError) as error:
        db_session.execute(text(statement))
    db_session.rollback()
    # 23514 = check_violation
    assert error.value.orig.sqlstate == "23514"
    assert error.value.orig.diag.constraint_name == constraint


def test_every_check_constraint_the_migration_adds_exists_and_is_validated(db_session):
    rows = db_session.execute(text(
        "SELECT conname, convalidated FROM pg_constraint WHERE contype = 'c'"
    )).all()
    present = dict(rows)
    added = [
        *_check_constraints_migration().CONSTRAINTS,
        *_check_constraints_migration(STATUS_REVISION).CONSTRAINTS,
    ]
    for _table, name, _condition in added:
        assert present.get(name) is True, name


def _address_row(user_id, street, *, is_default=True, is_active=True):
    return Address(
        user_id=user_id, street=street, city="Town", state="State", postal_code="12345",
        country="DE", is_default=is_default, is_active=is_active,
    )


def test_a_second_active_default_address_is_refused_by_its_index(db_session):
    user = _new_user("defaults@example.com")
    db_session.add(user)
    db_session.flush()
    # A deleted default and any number of non-defaults are outside the index.
    db_session.add_all([
        _address_row(user.id, "1 Kept Street"),
        _address_row(user.id, "2 Deleted Street", is_active=False),
        _address_row(user.id, "3 Other Street", is_default=False),
    ])
    db_session.commit()

    db_session.add(_address_row(user.id, "4 Second Street"))
    with pytest.raises(IntegrityError) as error:
        db_session.commit()
    db_session.rollback()

    assert error.value.orig.sqlstate == "23505"
    assert error.value.orig.diag.constraint_name == "uix_user_default_address"


def _new_user(email):
    return User(
        email=email, hashed_password="x", full_name="Someone",
        gdpr_consent=True, privacy_policy_accepted=True,
    )


def test_two_accounts_cannot_share_an_email_that_differs_only_in_case(db_session):
    db_session.add(_new_user("Case@Example.com"))
    db_session.commit()

    db_session.add(_new_user("case@example.com"))
    with pytest.raises(IntegrityError) as error:
        db_session.commit()
    db_session.rollback()

    # 23505 = unique_violation, from the expression index, not ix_users_email
    # (which is case-sensitive and would have let this through).
    assert error.value.orig.sqlstate == "23505"
    assert error.value.orig.diag.constraint_name == "uq_users_email_lower"


def test_lower_email_lookup_is_served_by_the_unique_index(db_session):
    db_session.add(_new_user("a@example.com"))
    db_session.commit()
    db_session.execute(text("SET enable_seqscan = off"))
    plan = "\n".join(db_session.execute(text(
        "EXPLAIN SELECT id FROM users WHERE lower(email) = 'a@example.com'"
    )).scalars())
    db_session.rollback()
    assert "uq_users_email_lower" in plan


def test_the_expiry_sweep_has_a_partial_index_on_new_orders_by_age(db_session):
    # cancel_expired_unpaid_orders (app/tasks.py) asks for new orders older
    # than a cutoff. Which index the planner picks depends on the table's
    # statistics (on a near-empty test table it may take idx_order_status), so
    # the index is checked by its definition, as PostgreSQL reports it.
    definition = db_session.execute(text(
        "SELECT pg_get_indexdef('idx_order_new_created_at'::regclass)"
    )).scalar_one()
    assert "ON public.orders USING btree (created_at)" in definition
    assert "WHERE ((status)::text = 'new'::text)" in definition


# -- The migrations ----------------------------------------------------------

def _schema_snapshot(url):
    """Tables, columns (type, nullability), indexes and constraints, as the
    database reports them, to compare two databases built different ways."""
    snapshot_engine = create_engine(url)
    try:
        with snapshot_engine.connect() as connection:
            def rows(sql):
                return sorted(tuple(map(str, row)) for row in connection.execute(text(sql)))

            return {
                "columns": rows(
                    "SELECT table_name, column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns WHERE table_schema = 'public' "
                    "AND table_name <> 'alembic_version'"
                ),
                "indexes": rows(
                    "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"
                ),
                "constraints": rows(
                    "SELECT conrelid::regclass, conname, pg_get_constraintdef(oid) "
                    "FROM pg_constraint WHERE connamespace = 'public'::regnamespace"
                ),
            }
    finally:
        snapshot_engine.dispose()


def _alembic(url, *args):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_ROOT, env={**os.environ, "DATABASE_URI": url},
        capture_output=True, text=True, timeout=300,
                                        check=False,
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stderr[-2000:]}"


def test_migrations_round_trip_and_agree_with_the_session_schema():
    admin_url = make_url(TEST_DATABASE_URL)
    # The name the session database uses, so a run killed halfway leaves a
    # database the next run's sweep in tests/conftest.py removes.
    name = f"ecom_pgtests_{int(time.time())}_{uuid.uuid4().hex[:12]}"
    url = admin_url.set(database=name).render_as_string(hide_password=False)
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))

        _alembic(url, "upgrade", "head")
        first = _schema_snapshot(url)
        assert first["constraints"] and first["indexes"]

        _alembic(url, "downgrade", "base")
        check = create_engine(url)
        try:
            leftovers = set(inspect(check).get_table_names()) - {"alembic_version"}
        finally:
            check.dispose()
        assert leftovers == set(), "downgrade base left tables behind"

        _alembic(url, "upgrade", "head")
        assert _schema_snapshot(url) == first

        # The migrated schema and the models agree: autogenerate finds nothing
        # to add, drop or alter (types and server defaults included).
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext

        from app.models.base import Base

        compare_engine = create_engine(url)
        try:
            with compare_engine.connect() as connection:
                context = MigrationContext.configure(
                    connection, opts={"compare_type": True, "compare_server_default": True}
                )
                assert compare_metadata(context, Base.metadata) == []
        finally:
            compare_engine.dispose()

        # The schema every test of the session runs on was built the same way.
        from conftest import TEST_DB_URL

        assert _schema_snapshot(TEST_DB_URL) == first
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_every_model_table_exists_after_the_migrations(db_session):
    existing = set(inspect(db_session.connection()).get_table_names())
    assert {table.name for table in Base.metadata.sorted_tables} <= existing


def test_webhook_event_claimed_once_with_skip_locked():
    """3B-11: Proves one webhook event is claimed once with FOR UPDATE SKIP LOCKED
    across two concurrent PostgreSQL connections."""
    from app.models.payment import WebhookEvent
    from app.services import stripe_events

    event_id = f"evt_skip_locked_{uuid.uuid4().hex[:16]}"
    with TestingSessionLocal() as setup_db:
        setup_db.add(WebhookEvent(event_id=event_id, event_type="charge.dispute.created", payload="{}"))
        setup_db.commit()

    conn1 = TestingSessionLocal()
    conn2 = TestingSessionLocal()
    try:
        claimed1 = stripe_events._claim_event(conn1, event_id)
        assert claimed1 is not None
        assert claimed1.event_id == event_id

        # Connection 2 attempts to claim the same event while connection 1 holds it.
        # With SKIP LOCKED, PostgreSQL returns None immediately instead of blocking.
        claimed2 = stripe_events._claim_event(conn2, event_id)
        assert claimed2 is None
    finally:
        conn1.rollback()
        conn1.close()
        conn2.rollback()
        conn2.close()
