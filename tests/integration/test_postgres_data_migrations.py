"""Data-carrying migrations on PostgreSQL, with rows in the tables.

The migration tests elsewhere look at the SQL a migration renders or at an
empty schema. A migration that rewrites or checks data is only proven by
running it over data: upgrade to the revision before it, insert what a
production database holds, upgrade through it, look at the rows, go back.

  ada27dd39537  timestamps -> timestamptz, USING col AT TIME ZONE 'UTC'
  3f6c2a9d1b7e  unique index on lower(email), refusing case-only duplicates

Skipped unless TEST_DATABASE_URL names a server; CI's test-postgres job runs it.
"""

import os
import subprocess
import sys
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

# A zone far from UTC with a half-hour offset, set on the database so that a
# conversion that leaned on the session zone instead of 'UTC' would shift.
DATABASE_ZONE = "Asia/Kolkata"


def _alembic(url, *args, check=True):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_ROOT, env={**os.environ, "DATABASE_URI": url},
        capture_output=True, text=True, timeout=300,
    )
    if check:
        assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stderr[-2000:]}"
    return result


@pytest.fixture
def migration_db():
    admin_url = make_url(TEST_DATABASE_URL)
    name = f"ecom_pgtests_data_{uuid.uuid4().hex[:12]}"
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
        with pytest.raises(Exception) as refused:
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
