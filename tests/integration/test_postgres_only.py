"""Behaviour that only exists on a real PostgreSQL server.

The rest of the suite is written to pass on SQLite and on PostgreSQL alike;
these tests check what SQLite cannot show: that the order row lock in the pay
path really makes a second payment wait, that the CHECK constraints and the
unique index on lower(email) are enforced by the database itself, and that the
Alembic migrations run forward, back to nothing and forward again.

They are skipped unless TEST_DATABASE_URL points at a server (see
tests/conftest.py); CI's test-postgres job runs them.
"""
import os
import subprocess
import sys
import threading
import time
import uuid

import pytest
from conftest import TEST_DATABASE_URL, TestingSessionLocal, engine, requires_postgres
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from test_check_constraints import _migration as _check_constraints_migration
from test_check_constraints import order_graph  # noqa: F401  (fixture)
from test_payment import (
    ORDERS_PREFIX,
    _create_verified_buyer_with_order,
    _get_order,
    _get_payments,
    _payment_payload,
)

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
    yield


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
    holder = TestingSessionLocal()
    holder.execute(text("SELECT id FROM orders WHERE id = :id FOR UPDATE"), {"id": order_id})

    results = {}
    thread = threading.Thread(target=_pay, args=(client, setup, results, uuid.uuid4().hex))
    thread.start()
    try:
        assert _wait_until(lambda: _blocked_on_a_lock() >= 1), "/pay never waited for the lock"
        time.sleep(0.5)
        assert thread.is_alive(), "/pay answered while another connection held the order row"
        assert _get_order(db_session, order_id).payment_status != "paid"
    finally:
        holder.commit()
        holder.close()
    thread.join(timeout=30)

    assert not thread.is_alive()
    (response,) = results.values()
    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert _get_order(db_session, order_id).payment_status == "paid"


def test_two_concurrent_payments_for_one_order_charge_it_once(client, db_session):
    setup = _create_verified_buyer_with_order(client, db_session)
    order_id = setup["order"]["id"]

    results = {}
    start = threading.Barrier(2)

    def attempt(key):
        start.wait()
        _pay(client, setup, results, key)

    threads = [threading.Thread(target=attempt, args=(uuid.uuid4().hex,)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)

    codes = sorted(response.status_code for response in results.values())
    # One wins; the other is told the order is paid or a payment is in flight.
    assert codes[0] == 200 and codes[1] in (400, 409), [r.text for r in results.values()]

    db_session.expire_all()
    order = _get_order(db_session, order_id)
    assert order.payment_status == "paid"
    succeeded = [p for p in _get_payments(db_session, order_id) if p.status == "succeeded"]
    assert len(succeeded) == 1


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
    present = {name: validated for name, validated in rows}
    for _table, name, _condition in _check_constraints_migration().CONSTRAINTS:
        assert present.get(name) is True, name


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
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stderr[-2000:]}"


def test_migrations_round_trip_and_agree_with_the_session_schema():
    admin_url = make_url(TEST_DATABASE_URL)
    name = f"ecom_pgtests_mig_{uuid.uuid4().hex[:12]}"
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
