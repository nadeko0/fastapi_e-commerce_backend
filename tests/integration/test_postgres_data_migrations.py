"""Data-carrying migrations on PostgreSQL, with rows in the tables.

The migration tests elsewhere look at the SQL a migration renders or at an
empty schema. A migration that rewrites or checks data is only proven by
running it over data: upgrade to the revision before it, insert what a
production database holds, upgrade through it, look at the rows, go back.

  ada27dd39537  timestamps -> timestamptz, USING col AT TIME ZONE 'UTC'
  3f6c2a9d1b7e  unique index on lower(email), refusing case-only duplicates
  d7a3b9e15c28  order snapshots, backfilled from the addresses
  f180919aaf3b  one default address per user (older duplicates unset), the
                payment status CHECK (refusing rows that break it), and
                payments.reconciled_at

Skipped unless TEST_DATABASE_URL names a server; CI's test-postgres job runs it.
"""

import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime

import pytest
from conftest import TEST_DATABASE_URL, requires_postgres
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

pytestmark = requires_postgres

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BEFORE_TIMESTAMPS = "d4e9a1c7b265"
TIMESTAMPS = "ada27dd39537"
BEFORE_EMAIL_INDEX = "e1959653f4a2"
EMAIL_INDEX = "3f6c2a9d1b7e"
BEFORE_ORDER_SNAPSHOTS = "c5e2a8f4b196"
ORDER_SNAPSHOTS = "d7a3b9e15c28"
DEFAULT_ADDRESS = "f180919aaf3b"

# A zone far from UTC with a half-hour offset, set on the database so that a
# conversion that leaned on the session zone instead of 'UTC' would shift.
DATABASE_ZONE = "Asia/Kolkata"


def _alembic(url, *args, check=True):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_ROOT, env={**os.environ, "DATABASE_URI": url},
        capture_output=True, text=True, timeout=300,
                                        check=False,
    )
    if check:
        assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stderr[-2000:]}"
    return result


@pytest.fixture
def migration_db():
    admin_url = make_url(TEST_DATABASE_URL)
    # The name the session database uses, so a run killed halfway leaves a
    # database the next run's sweep in tests/conftest.py removes.
    name = f"ecom_pgtests_{int(time.time())}_{uuid.uuid4().hex[:12]}"
    url = admin_url.set(database=name).render_as_string(hide_password=False)
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
        connection.execute(text(f"ALTER DATABASE \"{name}\" SET timezone = '{DATABASE_ZONE}'"))
    engine = create_engine(url)
    try:
        yield url, engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _insert_user(connection, email, created_at="2026-01-01 00:00:00", **columns):
    values = {
        "email": email, "hashed_password": "x", "is_active": True, "is_email_verified": False,
        "gdpr_consent": True, "privacy_policy_accepted": True, "marketing_consent": False,
        "data_deletion_requested": False, "data_retention_period": 365,
        "consent_history": "[]", "data_export_requests": "[]",
        "created_at": created_at, "updated_at": created_at, **columns,
    }
    names = ", ".join(values)
    params = ", ".join(f":{key}" for key in values)
    return connection.execute(text(f"INSERT INTO users ({names}) VALUES ({params}) RETURNING id"), values).scalar_one()


def _data_type(connection, table, column):
    return connection.execute(
        text("SELECT data_type FROM information_schema.columns WHERE table_name = :t AND column_name = :c"),
        {"t": table, "c": column},
    ).scalar_one()


def _epoch(connection, table, column, row_id):
    return float(connection.execute(text(f"SELECT extract(epoch FROM {column}) FROM {table} WHERE id = :i"), {"i": row_id}).scalar_one())


def _utc_epoch(text_value):
    return datetime.fromisoformat(text_value).replace(tzinfo=UTC).timestamp()


# Wall-clock values written by datetime.utcnow(): an ordinary one, one inside
# the hour a Europe/Berlin clock skips on 2026-03-29, one inside the hour it
# repeats on 2026-10-25, the end of the year, and a fraction of a second.
_STAMPS = {
    "created_at": "2026-06-15 12:34:56",
    "updated_at": "2026-03-29 01:30:00",
    "last_login": "2026-10-25 01:30:00",
    "email_verification_date": "2026-12-31 23:59:59.123456",
}


def test_timestamps_become_timestamptz_with_the_same_utc_instants_and_come_back(migration_db):
    url, engine = migration_db
    _alembic(url, "upgrade", BEFORE_TIMESTAMPS)
    with engine.begin() as connection:
        user_id = _insert_user(connection, "stamps@example.com", **_STAMPS)
        category_id = connection.execute(
            text(
                "INSERT INTO categories (name, path, level, created_at, updated_at) "
                "VALUES ('c', '{}', 0, '2026-02-03 04:05:06', '2026-02-03 04:05:06') RETURNING id"
            )
        ).scalar_one()
        assert _data_type(connection, "users", "created_at") == "timestamp without time zone"

    _alembic(url, "upgrade", TIMESTAMPS)

    with engine.connect() as connection:
        for column, written in _STAMPS.items():
            assert _data_type(connection, "users", column) == "timestamp with time zone"
            assert _epoch(connection, "users", column, user_id) == _utc_epoch(written), column
        # Columns that were NULL stay NULL, and other tables convert too.
        assert connection.execute(text("SELECT gdpr_consent_date FROM users WHERE id = :i"), {"i": user_id}).scalar_one() is None
        assert _data_type(connection, "categories", "created_at") == "timestamp with time zone"
        assert _epoch(connection, "categories", "created_at", category_id) == _utc_epoch("2026-02-03 04:05:06")
        # What the application reads is an aware UTC instant, whatever the session zone says.
        read = connection.execute(text("SELECT created_at FROM users WHERE id = :i"), {"i": user_id}).scalar_one()
        assert read.utcoffset() is not None and read.timestamp() == _utc_epoch(_STAMPS["created_at"])

    _alembic(url, "downgrade", BEFORE_TIMESTAMPS)

    with engine.connect() as connection:
        assert _data_type(connection, "users", "created_at") == "timestamp without time zone"
        row = connection.execute(
            text("SELECT created_at, updated_at, last_login, email_verification_date FROM users WHERE id = :i"),
            {"i": user_id},
        ).one()
        # The UTC wall clock that was written, not the session zone's.
        assert [value.isoformat(sep=" ") for value in row] == [
            _STAMPS["created_at"], _STAMPS["updated_at"], _STAMPS["last_login"], _STAMPS["email_verification_date"],
        ]


def test_the_email_index_migration_refuses_case_duplicates_without_printing_them(migration_db):
    url, engine = migration_db
    _alembic(url, "upgrade", BEFORE_EMAIL_INDEX)
    with engine.begin() as connection:
        _insert_user(connection, "jane.private@example.com")
        _insert_user(connection, "Jane.Private@Example.com")
        _insert_user(connection, "other@example.com")

    refused = _alembic(url, "upgrade", EMAIL_INDEX, check=False)

    assert refused.returncode != 0
    output = refused.stdout + refused.stderr
    assert "1 email address(es) belong to more than one account" in output
    assert "Merge or rename those accounts" in output
    assert "private" not in output.lower() and "other@" not in output
    with engine.connect() as connection:
        # Nothing was changed to make the index fit, and the revision did not move.
        assert connection.execute(text("SELECT count(*) FROM users")).scalar_one() == 3
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == BEFORE_EMAIL_INDEX
        assert connection.execute(text("SELECT count(*) FROM pg_indexes WHERE indexname = 'uq_users_email_lower'")).scalar_one() == 0


def test_the_email_index_migration_goes_through_once_the_duplicate_is_resolved_and_keeps_the_rows_on_downgrade(migration_db):
    url, engine = migration_db
    _alembic(url, "upgrade", BEFORE_EMAIL_INDEX)
    with engine.begin() as connection:
        _insert_user(connection, "Mixed.Case@Example.com")
        _insert_user(connection, "plain@example.com")

    _alembic(url, "upgrade", EMAIL_INDEX)

    with engine.connect() as connection:
        # Not rewritten: the stored spelling is kept and found by lower(email).
        assert connection.execute(text("SELECT email FROM users WHERE lower(email) = 'mixed.case@example.com'")).scalar_one() == "Mixed.Case@Example.com"
        assert connection.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_users_email_lower'")).scalar_one().endswith("(lower((email)::text))")
    with engine.begin() as connection, pytest.raises(Exception, match="uq_users_email_lower"):
        _insert_user(connection, "MIXED.CASE@example.com")

    _alembic(url, "downgrade", BEFORE_EMAIL_INDEX)

    with engine.begin() as connection:
        assert sorted(connection.execute(text("SELECT email FROM users")).scalars()) == ["Mixed.Case@Example.com", "plain@example.com"]
        assert connection.execute(text("SELECT count(*) FROM pg_indexes WHERE indexname = 'uq_users_email_lower'")).scalar_one() == 0
        _insert_user(connection, "MIXED.CASE@example.com")  # allowed again, as before the migration


def test_the_offline_script_makes_the_same_refusal_on_a_database_with_duplicates(migration_db):
    # `alembic upgrade --sql` cannot query, so the script carries the check;
    # run by hand against the data it must refuse the same way, then pass.
    url, engine = migration_db
    _alembic(url, "upgrade", BEFORE_EMAIL_INDEX)
    script = _alembic(url, "upgrade", f"{BEFORE_EMAIL_INDEX}:{EMAIL_INDEX}", "--sql").stdout
    with engine.begin() as connection:
        _insert_user(connection, "dup.private@example.com")
        duplicate = _insert_user(connection, "DUP.PRIVATE@example.com")

    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        with pytest.raises(Exception, match="belong to more than one account") as refused:
            cursor.execute(script)
        raw.rollback()
        message = str(refused.value)
        assert "1 email address(es) belong to more than one account" in message
        assert "private" not in message.lower()

        cursor.execute("DELETE FROM users WHERE id = %s", (duplicate,))
        raw.commit()
        cursor.execute(script)
        raw.commit()
        cursor.execute("SELECT count(*) FROM pg_indexes WHERE indexname = 'uq_users_email_lower'")
        assert cursor.fetchone()[0] == 1
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchone()[0] == EMAIL_INDEX
    finally:
        raw.close()


def test_order_snapshots_migration_backfills_shipping_address_on_postgres(migration_db):
    url, engine = migration_db
    _alembic(url, "upgrade", BEFORE_ORDER_SNAPSHOTS)
    with engine.begin() as connection:
        user_id = _insert_user(connection, "snapshot_buyer@example.com")
        addr_id = connection.execute(text(
            f"INSERT INTO addresses (user_id, street, city, state, postal_code, country, address_type, is_default, is_active, created_at, updated_at) "
            f"VALUES ({user_id}, '123 Market St', 'Springfield', 'IL', '62701', 'US', 'home', true, true, now(), now()) RETURNING id"
        )).scalar_one()
        order_id = connection.execute(text(
            f"INSERT INTO orders (user_id, shipping_address_id, total_amount, currency, status, payment_status, created_at, updated_at) "
            f"VALUES ({user_id}, {addr_id}, 50.00, 'USD', 'new', 'pending', now(), now()) RETURNING id"
        )).scalar_one()

    _alembic(url, "upgrade", ORDER_SNAPSHOTS)

    with engine.connect() as connection:
        row = connection.execute(text(
            f"SELECT ship_to_street, ship_to_city, ship_to_state, ship_to_postal_code, ship_to_country "
            f"FROM orders WHERE id = {order_id}"
        )).one()
        assert row.ship_to_street == "123 Market St"
        assert row.ship_to_city == "Springfield"
        assert row.ship_to_state == "IL"
        assert row.ship_to_postal_code == "62701"
        assert row.ship_to_country == "US"

    _alembic(url, "downgrade", BEFORE_ORDER_SNAPSHOTS)

    with engine.connect() as connection:
        cols = connection.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'orders'"
        )).scalars().all()
        assert "ship_to_street" not in cols


def _insert_address(connection, user_id, street, created_at, *, is_default=True, is_active=True):
    return connection.execute(text(
        "INSERT INTO addresses (user_id, street, city, state, postal_code, country, address_type, "
        "is_default, is_active, created_at, updated_at) VALUES (:user_id, :street, 'Town', 'State', "
        "'12345', 'DE', 'home', :is_default, :is_active, :created_at, :created_at) RETURNING id"
    ), {"user_id": user_id, "street": street, "is_default": is_default, "is_active": is_active,
        "created_at": created_at}).scalar_one()


def _insert_payment(connection, user_id, address_id, status):
    order_id = connection.execute(text(
        "INSERT INTO orders (user_id, shipping_address_id, total_amount, currency, status, "
        "payment_status, created_at, updated_at) "
        "VALUES (:user_id, :address_id, 25.00, 'USD', 'new', 'pending', now(), now()) RETURNING id"
    ), {"user_id": user_id, "address_id": address_id}).scalar_one()
    return connection.execute(text(
        "INSERT INTO payments (order_id, provider, payment_intent_id, amount, amount_refunded, "
        "currency, status, requires_manual_review, created_at, updated_at) "
        "VALUES (:order_id, 'stripe', :intent, 2500, 0, 'USD', :status, false, now(), now()) RETURNING id"
    ), {"order_id": order_id, "intent": f"pi_{uuid.uuid4().hex}", "status": status}).scalar_one()


def _active_defaults(connection):
    return sorted(connection.execute(text(
        "SELECT id FROM addresses WHERE is_default AND is_active"
    )).scalars())


def _exists(connection, sql):
    return connection.execute(text(f"SELECT count(*) FROM {sql}")).scalar_one() > 0


def test_the_default_address_migration_keeps_each_users_newest_default_and_comes_back(migration_db):
    url, engine = migration_db
    _alembic(url, "upgrade", ORDER_SNAPSHOTS)
    with engine.begin() as connection:
        many = _insert_user(connection, "many.defaults@example.com")
        older = _insert_address(connection, many, "1 Old Street", "2026-01-01 00:00:00")
        newest = _insert_address(connection, many, "2 New Street", "2026-03-01 00:00:00")
        middle = _insert_address(connection, many, "3 Middle Street", "2026-02-01 00:00:00")
        # Deleted: outside the index, left as it is although it is newer.
        deleted = _insert_address(connection, many, "4 Gone Street", "2026-04-01 00:00:00", is_active=False)
        _insert_address(connection, many, "5 Plain Street", "2026-05-01 00:00:00", is_default=False)
        single = _insert_user(connection, "one.default@example.com")
        only = _insert_address(connection, single, "6 Only Street", "2026-01-01 00:00:00")
        tied = _insert_user(connection, "tied.defaults@example.com")
        _insert_address(connection, tied, "7 Tie Street", "2026-01-01 00:00:00")
        tie_winner = _insert_address(connection, tied, "8 Tie Street", "2026-01-01 00:00:00")
        payment = _insert_payment(connection, single, only, "succeeded")

    _alembic(url, "upgrade", DEFAULT_ADDRESS)

    with engine.connect() as connection:
        # The newest per user (the higher id when they were made at the same moment).
        assert _active_defaults(connection) == sorted([newest, only, tie_winner])
        assert connection.execute(text("SELECT count(*) FROM addresses")).scalar_one() == 8
        assert connection.execute(text("SELECT is_default FROM addresses WHERE id = :i"), {"i": deleted}).scalar_one()
        assert connection.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'uix_user_default_address'"
        )).scalar_one().endswith("(user_id) WHERE (is_default AND is_active)")
        assert connection.execute(text("SELECT reconciled_at FROM payments WHERE id = :i"), {"i": payment}).scalar_one() is None
        assert connection.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_order_new_created_at'"
        )).scalar_one().endswith("(created_at) WHERE ((status)::text = 'new'::text)")
    with engine.begin() as connection, pytest.raises(Exception, match="uix_user_default_address"):
        connection.execute(text("UPDATE addresses SET is_default = true WHERE id = :i"), {"i": middle})
    with engine.begin() as connection, pytest.raises(Exception, match="ck_payment_attempt_status"):
        connection.execute(text("UPDATE payments SET status = 'canceled'"))

    _alembic(url, "downgrade", ORDER_SNAPSHOTS)

    with engine.begin() as connection:
        assert not _exists(connection, "pg_indexes WHERE indexname = 'uix_user_default_address'")
        assert not _exists(connection, "pg_indexes WHERE indexname = 'idx_order_new_created_at'")
        assert not _exists(connection, "pg_constraint WHERE conname = 'ck_payment_attempt_status'")
        assert not _exists(connection, "information_schema.columns WHERE table_name = 'payments' AND column_name = 'reconciled_at'")
        # The duplicates are not brought back, but two defaults are allowed again.
        assert _active_defaults(connection) == sorted([newest, only, tie_winner])
        connection.execute(text("UPDATE addresses SET is_default = true WHERE id = :i"), {"i": older})


def test_the_default_address_migration_refuses_a_payment_status_the_check_would_reject(migration_db):
    url, engine = migration_db
    _alembic(url, "upgrade", ORDER_SNAPSHOTS)
    with engine.begin() as connection:
        user = _insert_user(connection, "odd.status@example.com")
        first = _insert_address(connection, user, "1 First Street", "2026-01-01 00:00:00")
        second = _insert_address(connection, user, "2 Second Street", "2026-02-01 00:00:00")
        _insert_payment(connection, user, first, "canceled")
        _insert_payment(connection, user, first, "succeeded")

    refused = _alembic(url, "upgrade", DEFAULT_ADDRESS, check=False)

    assert refused.returncode != 0
    output = refused.stdout + refused.stderr
    assert "payments: 1 row break ck_payment_attempt_status" in output
    assert "nothing was changed" in output
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == ORDER_SNAPSHOTS
        # The upgrade stopped before resolving the defaults.
        assert _active_defaults(connection) == [first, second]
        assert not _exists(connection, "pg_indexes WHERE indexname = 'uix_user_default_address'")
