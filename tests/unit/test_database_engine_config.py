"""Proves Part 1 of the pool-leak defense-in-depth: idle_in_transaction_
session_timeout/statement_timeout are passed to Postgres connections (so a
connection stranded in an open transaction - by any future bug, not just the
ones already fixed - is forcibly killed by Postgres itself after 30s instead
of wedging the pool permanently until a manual restart), and are *not*
passed for SQLite, which the whole test suite runs against and which would
error outright on an unrecognized `options` connect arg (sqlite3 has no such
option) rather than silently ignore it.

app.core.database's module-level `engine` is always built from a Postgres
DATABASE_URI in this test environment (see tests/conftest.py's POSTGRES_*
env vars) with pool_size/max_overflow kwargs that plain SQLite's default
pool classes don't accept - so this exercises the pure `_build_connect_args`
helper directly for both dialects instead of reimporting the whole module
under a swapped-out DATABASE_URI, which would fail for an unrelated,
pool-class reason having nothing to do with what's under test here.
"""
from app.core.database import _build_connect_args


def test_postgres_database_uri_gets_idle_in_transaction_timeout():
    connect_args = _build_connect_args("postgresql://user:pw@localhost/dbname")

    assert "options" in connect_args, (
        "postgres connect_args must set idle_in_transaction_session_timeout "
        "so a leaked idle-in-transaction connection self-heals instead of "
        "wedging the pool permanently"
    )
    assert "idle_in_transaction_session_timeout=30000" in connect_args["options"]
    assert "statement_timeout=60000" in connect_args["options"]


def test_postgresql_psycopg2_uri_also_matches():
    # The driver-qualified form (postgresql+psycopg2://...) is what
    # app.core.config's DATABASE_URI validator can actually produce -
    # confirm the dialect check isn't an exact-prefix match that misses it.
    connect_args = _build_connect_args("postgresql+psycopg2://user:pw@localhost/dbname")

    assert "options" in connect_args
    assert "idle_in_transaction_session_timeout=30000" in connect_args["options"]


def test_sqlite_database_uri_does_not_get_postgres_only_connect_args():
    assert _build_connect_args("sqlite:///:memory:") == {}, (
        "SQLite has no idle_in_transaction_session_timeout/statement_timeout "
        "session option - passing Postgres's `options` connect_args to "
        "sqlite3 fails outright rather than being ignored, which would "
        "break the entire test suite (it runs on SQLite)"
    )


def test_file_sqlite_database_uri_does_not_get_postgres_only_connect_args():
    assert _build_connect_args("sqlite:///./somewhere.db") == {}


def test_celery_engine_also_gets_the_same_pool_leak_defense_in_depth():
    # Celery's DB engine (app/tasks.py) is a *separate* SQLAlchemy engine
    # from app.core.database's - a different process (the worker), so it
    # must independently get the same idle_in_transaction_session_timeout/
    # statement_timeout/pool_pre_ping protection, not just a bare
    # create_engine(settings.DATABASE_URI) with none of it. Confirmed live:
    # running a real local worker against real Postgres left 2 connections
    # permanently idle-in-transaction (task_time_limit killed the task
    # mid-transaction) with zero self-healing, because this engine had no
    # timeout protection at all before this fix.
    from app.core.config import settings
    from app.tasks import engine as celery_engine

    assert celery_engine.pool.size() == settings.DB_POOL_SIZE, (
        "app.tasks's engine must be built with the same pool_size as "
        "app.core.database's, not SQLAlchemy's bare default - a give-away "
        "that it's reusing the shared, protected engine-construction args "
        "rather than a plain create_engine(settings.DATABASE_URI)"
    )
