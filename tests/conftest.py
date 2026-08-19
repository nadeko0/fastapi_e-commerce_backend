import os

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key")
os.environ.setdefault("POSTGRES_SERVER", "localhost")
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("POSTGRES_DB", "test")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "test-encryption-key")
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
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")  # no Redis available in this test env
# bcrypt's default cost factor (12 rounds) is deliberately slow (~400ms/hash)
# for brute-force resistance in production; tests register/log in dozens of
# users and don't need that security margin, so use the minimum valid cost
# factor here instead - this is the single biggest lever on suite runtime.
os.environ.setdefault("BCRYPT_ROUNDS", "4")

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 registers all tables on Base
from app.api.deps import get_db as deps_get_db
from app.core.database import get_db as database_get_db
from app.main import app
from app.models.base import Base
from app.services.redis import RedisService

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


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
    """
    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("app.services.redis.Redis", lambda *a, **k: fake)
    RedisService()
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
