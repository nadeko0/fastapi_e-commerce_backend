import os

# At least as long as the hash: 32 bytes for HS256, 64 for the HS512 token a
# negative test signs with it (RFC 7518 3.2). PyJWT warns on shorter keys and
# pyproject.toml turns that warning into an error.
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-long-enough-for-hs256-and-the-hs512-negative-test")
os.environ.setdefault("POSTGRES_SERVER", "localhost")
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("POSTGRES_DB", "test")
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

import atexit
import shutil
import tempfile

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

import app.core.database as database_module
import app.models  # noqa: F401 registers all tables on Base
import app.services.redis_async as redis_async_module
from app.api.deps import get_db as deps_get_db
from app.core.database import get_db as database_get_db
from app.main import app
from app.models.base import Base
from app.services.redis import RedisService

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


TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# NullPool: TestClient runs each request on its own event loop, and an
# aiosqlite connection cannot outlive the loop that opened it.
async_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_url_path}", poolclass=NullPool)

# Swapped at the factory, not via dependency_overrides: the real
# get_async_db (commit on success, rollback + generic 500 on a database
# error) is then what every test exercises, just against SQLite.
database_module.AsyncSessionLocal = async_sessionmaker(
    bind=async_engine, autoflush=False, expire_on_commit=False
)


@atexit.register
def _remove_test_database():
    engine.dispose()
    shutil.rmtree(_db_dir, ignore_errors=True)


def _override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


# The app has two separate get_db implementations (app.core.database and
# app.api.deps) used inconsistently across routers; both must be overridden.
app.dependency_overrides[database_get_db] = _override_get_db
app.dependency_overrides[deps_get_db] = _override_get_db


@pytest.fixture(autouse=True)
def _reset_database():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


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
    (real Postgres) and calls next(get_db()) inside each Celery task body -
    it never goes through app.core.database.get_db/app.api.deps.get_db, so
    the dependency_overrides above don't reach it. Point its SessionLocal at
    the same in-memory SQLite engine the rest of the suite uses instead."""
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
