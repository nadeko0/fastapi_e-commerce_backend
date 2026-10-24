import os

# At least as long as the hash: 32 bytes for HS256, 64 for the HS512 token a
# negative test signs with it (RFC 7518 3.2). PyJWT warns on shorter keys and
# pyproject.toml turns that warning into an error.
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-long-enough-for-hs256-and-the-hs512-negative-test")
os.environ.setdefault("POSTGRES_SERVER", "localhost")
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("POSTGRES_DB", "test")
# The suite runs as development whatever a developer's .env says: a .env set
# to production would otherwise make Settings refuse to load (placeholder
# secrets, the mock payment provider), and the results would depend on it.
os.environ.setdefault("ENVIRONMENT", "development")
# The suite never writes the real logs/ directory (importing app.main sets up
# logging); tests of the file handler pass their own path.
os.environ.setdefault("LOG_FILE", "")
os.environ.setdefault("REDIS_HOST", "localhost")
# Nothing listens on port 1. Tests use a fake Redis; one that slips past it
# must fail here exactly as in CI (which has no Redis), not quietly use a
# Redis that happens to run on the developer's machine.
os.environ.setdefault("REDIS_PORT", "1")
os.environ.setdefault("SMTP_HOST", "localhost")
os.environ.setdefault("SMTP_USER", "test@example.com")
os.environ.setdefault("SMTP_PASSWORD", "test")
os.environ.setdefault("EMAILS_FROM_EMAIL", "test@example.com")
os.environ.setdefault("EMAILS_FROM_NAME", "Test Shop")
# Never let the suite attempt a real SMTP connection - the LoggingEmailProvider
# only logs and always returns True. Individual tests that need to assert on
# what would have been sent monkeypatch app.services.email.get_email_provider
# (or the specific send_*_email name) directly.
os.environ.setdefault("EMAIL_PROVIDER", "logging")
# The same for payments: the in-process mock, never api.stripe.com, even if
# a developer's .env selects the live provider.
os.environ.setdefault("PAYMENT_PROVIDER", "mock")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")  # no Redis available in this test env
# bcrypt's default cost factor (12 rounds) is deliberately slow (~400ms/hash)
# for brute-force resistance in production; tests register/log in dozens of
# users and don't need that security margin, so use the minimum valid cost
# factor here instead - this is the single biggest lever on suite runtime.
os.environ.setdefault("BCRYPT_ROUNDS", "4")
# A fixed browser origin for tests/integration/test_cors.py: assigned, not
# defaulted, so a developer's .env cannot change what that test expects.
os.environ["BACKEND_CORS_ORIGINS"] = "http://localhost:3000"

import atexit
import fnmatch
import re
import shutil
import subprocess
import sys
import tempfile
import types
import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

import app.core.database as database_module
import app.models  # registers all tables on Base
import app.services.redis_async as redis_async_module
from app.main import app
from app.models.base import Base
from app.services.redis import RedisService

# Hypothesis settings profiles (settings.register_profile / load_profile),
# chosen by HYPOTHESIS_PROFILE; CI sets "ci". Hypothesis also detects CI by
# itself and would load its own built-in "ci" profile (derandomized); loading
# one explicitly here keeps the choice in this file and the same on a laptop.
#   dev: Hypothesis' own defaults (100 examples, 200 ms deadline).
#   ci:  more examples, random seeds (derandomize=False, so every run explores
#        new inputs; the 4.0.0 payment failure was found that way), no
#        database (a fresh checkout has none to replay), no deadline (shared
#        runners are slow), and print_blob=True so a red run prints the
#        @reproduce_failure line needed to replay it.
# Per-test settings stay only where a test has a reason of its own (a cost
# budget for tests that make HTTP calls, or a deadline switched off).
hypothesis_settings.register_profile("dev", max_examples=100, deadline=200, print_blob=False, derandomize=False)
hypothesis_settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    print_blob=True,
    derandomize=False,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)
hypothesis_settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


def scaled_examples(examples: int) -> int:
    """A per-test example budget for expensive tests, doubled under the ci profile.

    A test that makes HTTP calls and writes to the database cannot take the
    profile's 200 examples; it sets its own count, and this keeps the ci
    profile's extra time on the tests where a failure is most valuable.
    """
    return examples * 2 if hypothesis_settings.get_current_profile_name() == "ci" else examples

# Optional: run the suite on a real PostgreSQL server.
#
#   TEST_DATABASE_URL=postgresql+psycopg://user:password@host:5432/postgres
#
# The database named in the URL is only where the connection starts, for
# CREATE DATABASE (it must exist; the role needs CREATEDB). Every session
# creates its own database with a unique name, builds the schema in it and
# drops it at exit, so nothing already on the server is ever written to.
# Without the variable the SQLite path below is used, exactly as before.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or None
IS_POSTGRES = TEST_DATABASE_URL is not None

# For tests that are only meaningful against a real server (row locks, the
# database's own CHECK constraints, the migrations). On the default SQLite
# run they are skipped, with this reason.
requires_postgres = pytest.mark.skipif(
    not IS_POSTGRES,
    reason="needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...",
)

# The tests whose outcome depends on what the database does (row locks,
# CHECK constraints, unique indexes, the payment/refund/dispute tables,
# column types, the migrations), selected with `pytest -m db_sensitive`. CI
# runs exactly these on PostgreSQL (job test-postgres); the whole suite also
# passes there. Chosen by file name so that a new test file following the
# naming convention is picked up without anyone remembering to mark it.
_DB_SENSITIVE_FILES = (
    "test_payment*.py",
    "test_order*.py",
    "test_checkout_session*.py",
    "test_refund*.py",
    "test_disputes.py",
    "test_webhook*.py",
    "test_tasks*.py",
    "test_check_constraints.py",
    "test_email_case.py",
    "test_email_migration.py",
    "test_users.py",
    "test_registration_and_verification.py",
    "test_db_pool_concurrency.py",
    "test_admin_orders.py",
    "test_admin_webhook_events.py",
    "test_stripe_event*.py",
    "test_reconciliation*.py",
    "test_postgres_*.py",
)


# Tests that measure elapsed time against a threshold (a heartbeat gap on the
# event loop, bcrypt calls that must overlap, a burst that must finish in N
# seconds). CPU contention from other xdist workers would make them flaky, so
# a parallel run deselects them (-m "not timing_sensitive") and a second,
# single-process run executes them alone: see README/CI for the two commands.
_TIMING_SENSITIVE_FILES = ("test_no_blocking_on_loop.py", "test_db_pool_concurrency.py")
_TIMING_SENSITIVE_TESTS = (
    "test_verify_password_async_offloads_to_threadpool_without_blocking_event_loop",
    "test_get_password_hash_async_offloads_to_threadpool_without_blocking_event_loop",
)


def pytest_collection_modifyitems(items):
    for item in items:
        if item.path.name in _TIMING_SENSITIVE_FILES or item.originalname in _TIMING_SENSITIVE_TESTS:
            item.add_marker(pytest.mark.timing_sensitive)
        if any(fnmatch.fnmatch(item.path.name, pattern) for pattern in _DB_SENSITIVE_FILES):
            item.add_marker(pytest.mark.db_sensitive)

if IS_POSTGRES:
    _admin_url = make_url(TEST_DATABASE_URL)
    if _admin_url.drivername != "postgresql+psycopg":
        raise RuntimeError("TEST_DATABASE_URL must be a postgresql+psycopg:// URL")
    _pg_name = f"ecom_pgtests_{uuid.uuid4().hex[:12]}"
    _admin_engine = create_engine(_admin_url, isolation_level="AUTOCOMMIT")
    with _admin_engine.connect() as _admin_conn:
        _admin_conn.execute(text(f'CREATE DATABASE "{_pg_name}"'))

    @atexit.register
    def _drop_postgres_database():
        # WITH (FORCE) (PostgreSQL 13+) disconnects pooled leftovers first.
        engine.dispose()
        with _admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{_pg_name}" WITH (FORCE)'))
        _admin_engine.dispose()

    TEST_DB_URL = _admin_url.set(database=_pg_name).render_as_string(hide_password=False)

    # The schema is built by `alembic upgrade head`, not Base.metadata.create_all:
    # the migrations are what production runs, so every session also proves
    # that the chain builds a schema the models agree with (a constraint or
    # column added to a model but forgotten in a migration fails here, which
    # create_all would hide). A subprocess, because Alembic's env.py calls
    # logging.fileConfig and would disable this process's loggers.
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env={**os.environ, "DATABASE_URI": TEST_DB_URL},
        check=True,
    )

    if sys.platform == "win32":
        # psycopg's async mode cannot run on Windows' default ProactorEventLoop.
        # Only a developer's machine needs this; the app itself runs in Linux
        # containers, and CI is Linux.
        import asyncio

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    _pg_options = "-c statement_timeout=60000"
    engine = create_engine(TEST_DB_URL, connect_args={"options": _pg_options}, pool_pre_ping=True)
    # NullPool: TestClient runs each request on its own event loop, and an
    # async psycopg connection cannot outlive the loop that opened it.
    async_engine = create_async_engine(
        TEST_DB_URL, poolclass=NullPool, connect_args={"options": _pg_options}
    )
else:
    TEST_DB_URL = None
    # A SQLite *file*, not :memory:. Routes are split between the sync Session
    # and AsyncSession (see app/core/database.py), which in tests means two
    # drivers (pysqlite and aiosqlite) that must see the same data - and an
    # in-memory SQLite database belongs to the one connection that created it.
    # A temp file is the one thing both can open.
    _db_dir = tempfile.mkdtemp(prefix="ecommerce-tests-")
    _db_url_path = os.path.join(_db_dir, "test.db").replace("\\", "/")

    engine = create_engine(
        f"sqlite:///{_db_url_path}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        # WAL: a test's own db_session usually still has a read transaction open
        # when it calls an endpoint, and in SQLite's default journal mode that
        # open reader would block a write coming from the other driver. WAL lets
        # readers and a writer coexist. It is a property of the file, so the
        # aiosqlite connections get it too.
        cursor.execute("PRAGMA journal_mode=WAL")
        # Throwaway database: skip fsync, the suite recreates every table per test.
        cursor.execute("PRAGMA synchronous=OFF")
        cursor.close()

    # NullPool: TestClient runs each request on its own event loop, and an
    # aiosqlite connection cannot outlive the loop that opened it.
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_url_path}", poolclass=NullPool)

    @atexit.register
    def _remove_test_database():
        engine.dispose()
        shutil.rmtree(_db_dir, ignore_errors=True)


TestingSessionLocal = sessionmaker(autoflush=False, bind=engine)

# Swapped at the factory, not via dependency_overrides: the real
# get_async_db (commit on success, rollback + generic 500 on a database
# error) is then what every test exercises, just against the test database.
database_module.AsyncSessionLocal = async_sessionmaker(
    bind=async_engine, autoflush=False, expire_on_commit=False
)


# Which tables a test wrote to. A test usually touches a handful of the ~25
# tables, and cleaning only those keeps the per-test cost small. Writes are
# noticed where every statement of every engine passes (the sync engine, the
# async one, Celery's SessionLocal, a test's own connection): Engine's
# cursor-execute event. A statement that is not a plain read or a plain
# INSERT/UPDATE/DELETE (DDL, a CTE, COPY, ...) marks every table, so an
# unforeseen kind of write errs towards cleaning more, never less.
_WRITE = re.compile(
    r'^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:ONLY\s+)?(?:"?\w+"?\.)?"?(\w+)"?', re.I
)
_READ = re.compile(r"^\s*\(?\s*(?:SELECT|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|SET|SHOW)\b", re.I)
# This file is imported twice (pytest's own "conftest" and, by the tests that do
# `from tests.conftest import ...`, "tests.conftest"), and each copy registers
# the listener below. The state lives in one object both share.
_state = sys.modules.setdefault("_tests_db_state", types.SimpleNamespace(dirty=set(), schema_changed=False))
_dirty_tables: set[str] = _state.dirty
# SQLite only: a test that ran DDL (a trigger, an index, a dropped table) has
# changed the schema itself, which DELETE cannot undo; the next test starts
# from a rebuilt schema, as every test used to.
_DDL = re.compile(r"^\s*(?:CREATE|DROP|ALTER)\b", re.I)

# Child tables first, so DELETE never meets a foreign key still pointing at it.
_TABLES_CHILDREN_FIRST = [t for t in reversed(Base.metadata.sorted_tables)]
_ALL_TABLES = {t.name for t in _TABLES_CHILDREN_FIRST}


@event.listens_for(Engine, "before_cursor_execute")
def _note_writes(conn, cursor, statement, parameters, context, executemany):
    if _DDL.match(statement):
        _state.schema_changed = True
    match = _WRITE.match(statement)
    if match:
        _dirty_tables.add(match.group(1))
    elif not _READ.match(statement):
        _dirty_tables.update(_ALL_TABLES)


def _clean_dirty_tables():
    if not IS_POSTGRES:
        # SQLite: the tables have no AUTOINCREMENT (no sqlite_sequence), so a
        # table emptied by DELETE hands out id 1 again, as a fresh one does.
        # Nothing else survives a DELETE; the schema was built once at import.
        if _state.schema_changed:
            Base.metadata.drop_all(bind=engine)
            Base.metadata.create_all(bind=engine)
            _state.schema_changed = False
            _dirty_tables.clear()
        elif _dirty_tables:
            with engine.begin() as connection:
                for table in _TABLES_CHILDREN_FIRST:
                    if table.name in _dirty_tables:
                        connection.exec_driver_sql(f'DELETE FROM "{table.name}"')
            _dirty_tables.clear()
        return
    # DELETE plus a sequence reset, not TRUNCATE ... RESTART IDENTITY:
    # TRUNCATE swaps the table's files and costs ~35 ms for two tables on
    # a Windows server against ~3 ms for DELETE of the few rows a test
    # leaves (and it takes ACCESS EXCLUSIVE locks). setval(..., false)
    # makes the next id 1 again, as in the fresh SQLite tables of the
    # default run; it also undoes ids burnt by rolled-back inserts. A table
    # no test wrote to is empty already, so no foreign-key closure is
    # needed.
    statements = []
    for table in _TABLES_CHILDREN_FIRST:
        if table.name not in _dirty_tables:
            continue
        statements.append(f'DELETE FROM "{table.name}"')
        pk = list(table.primary_key.columns)
        if len(pk) == 1 and pk[0].type.python_type is int:
            statements.append(
                f"SELECT setval(pg_get_serial_sequence('\"{table.name}\"', '{pk[0].name}'), 1, false)"
            )
    if statements:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout = '10s'"))
            connection.exec_driver_sql("; ".join(statements))
    _dirty_tables.clear()


if not IS_POSTGRES:
    # The schema is built once per process (one per xdist worker, each with
    # its own file) and the tables a test wrote to are emptied before the next
    # one, as on PostgreSQL. It used to be create_all + drop_all around every
    # test: ~160 s of the ~11 minute run.
    Base.metadata.create_all(bind=engine)
    _dirty_tables.clear()
    _state.schema_changed = False


@pytest.fixture(autouse=True)
def _reset_database():
    # Isolation: the schema is built once per process (above)
    # and the tables the previous test wrote to are emptied before this
    # one runs. The usual faster pattern, one outer transaction per test
    # with the Session joined to it through SAVEPOINTs and rolled back at
    # the end, does not fit this suite: the app and the tests use several
    # independent connections at once (the sync engine, the async engine
    # with a new connection per request, Celery tasks' own SessionLocal,
    # tests that open a second session on purpose), the code under test
    # commits for real, and the concurrency tests need those commits to be
    # visible to other connections. One outer transaction would hide
    # exactly what they are meant to show. Cleaning before the test (not
    # after) means a previous test that died halfway leaves nothing
    # behind either. lock_timeout turns a session a test forgot to close
    # (it would hold row locks) into an error instead of a hung run.
    _clean_dirty_tables()
    yield


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """Back RedisService with fakeredis instead of a real Redis connection.

    RedisService is a singleton (_instance/_pool class attrs): every
    RedisService() call anywhere - app.core.security's module-level
    redis_service, RateLimiter.__init__, the per-request `Depends()`
    instances in cart/orders/products/admin - returns the *same* object.
    __init__ reruns on every call though, always overwriting self._redis
    with a fresh client. So patching app.services.redis.Redis (the name
    imported into that module) to return one shared FakeRedis instance,
    then constructing RedisService() once here, rewrites _redis on the
    single shared singleton object - which every existing reference
    (including ones captured at import time, like app.core.security's
    module-level redis_service) already points at. A fresh FakeRedis per
    test keeps state (cart contents, blacklist entries, caches) from
    leaking between tests.

    The async client (app/services/redis_async.py: rate limiter and auth
    dependencies) is faked as well, on the *same* FakeServer, so both see
    one keyspace: a token blacklisted through the sync service is rejected
    by the async auth dependency, exactly as with one real Redis. Only the
    client factory is replaced - the real per-event-loop registry in
    get_async_redis()/close_async_redis() is what every test exercises -
    and the registry starts empty for each test, so no client (or its
    FakeServer) is carried over from the previous one.
    """
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeRedis(server=server, decode_responses=True)
    monkeypatch.setattr("app.services.redis.Redis", lambda *a, **k: fake)
    RedisService()
    monkeypatch.setattr(
        redis_async_module,
        "_create_client",
        lambda: fakeredis.FakeAsyncRedis(server=server, decode_responses=True),
    )
    monkeypatch.setattr(redis_async_module, "_clients", {})
    yield fake


@pytest.fixture(autouse=True)
def _session_local_is_the_test_database(monkeypatch):
    """app.core.database.SessionLocal (what session_scope() opens, e.g. for
    reconcile_payments run in a worker thread) is built from the production
    DATABASE_URI. Point it at the test database, like the factory swap does
    for AsyncSessionLocal; otherwise code that opens its own sync session would reach (or fail to
    reach) a real database."""
    monkeypatch.setattr(database_module, "SessionLocal", TestingSessionLocal)


@pytest.fixture
def async_session_factory():
    """The async sessionmaker the app uses, bound to the test database:
    `async with async_session_factory() as session:` inside a coroutine run
    by asyncio.run(). A session must be opened and closed on one loop (the
    engine has no pool, so nothing outlives it). Use it for setup or checks
    that must go through the same driver as the routes; db_session (sync)
    remains the way to seed rows."""
    return database_module.AsyncSessionLocal


@pytest.fixture
def client(monkeypatch, fake_redis):
    monkeypatch.setattr("app.api.v1.users.send_welcome_email", lambda *a, **k: True)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db_session():
    """Direct DB session for test setup that has no HTTP endpoint (e.g.
    creating an admin user - there is no signup-as-admin route)."""
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def tasks_db(monkeypatch):
    """app/tasks.py builds its own SessionLocal bound to settings.DATABASE_URI
    (real Postgres) and opens its own session inside each Celery task body,
    so the AsyncSessionLocal swap above does not reach it. Point its
    SessionLocal at the same in-memory SQLite engine the rest of the suite uses instead."""
    monkeypatch.setattr("app.tasks.SessionLocal", TestingSessionLocal)


@pytest.fixture
def valid_registration_payload():
    return {
        "email": "jane.doe@example.com",
        "password": "Str0ngPass",
        "full_name": "Jane Doe",
        "gdpr_consent": True,
        "privacy_policy_accepted": True,
        "marketing_consent": False,
    }
