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
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")  # no Redis available in this test env

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.core.database import get_db as database_get_db
from app.api.deps import get_db as deps_get_db
from app.models.base import Base
import app.models  # noqa: F401 registers all tables on Base
from app.main import app

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


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.api.v1.users.send_welcome_email", lambda *a, **k: True)
    # is_blacklisted() fails closed (returns True) when Redis is unreachable,
    # which would 401 every authenticated request in this Redis-less test env.
    monkeypatch.setattr("app.core.security.redis_service.is_blacklisted", lambda token: False)
    with TestClient(app) as test_client:
        yield test_client


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
