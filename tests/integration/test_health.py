from redis.exceptions import RedisError
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

import app.main as main_module


def test_health_check_reports_healthy_when_everything_is_up(client, monkeypatch):
    # /health uses app.main.engine directly (not a request-scoped
    # dependency), so the dependency_overrides that swap in the in-memory
    # SQLite DB elsewhere don't reach it - it's still the real Postgres
    # engine from settings.DATABASE_URI, which doesn't exist in this test
    # environment. Point it at a throwaway working SQLite engine instead of
    # conftest's shared one: app.main.cleanup() (run on the TestClient's
    # own shutdown, i.e. before this test's monkeypatch reverts) calls
    # engine.dispose() - on the shared engine that would tear down its
    # sole StaticPool connection and, with it, the in-memory database every
    # other test in the session depends on.
    healthy_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(main_module, "engine", healthy_engine)

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["services"]["database"] == "healthy"
    assert body["services"]["redis"] == "healthy"


def test_health_check_reports_degraded_and_503_on_database_failure(client, monkeypatch):
    def _broken_connect():
        raise SQLAlchemyError("db is down")

    monkeypatch.setattr(main_module.engine, "connect", _broken_connect)

    response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["services"]["database"] == "unhealthy"
    # Regression test for a security fix: the response must report a
    # generic status, never leak internal exception text (connection
    # strings, driver errors, etc.) to the client.
    assert "db is down" not in str(body)


def test_health_check_reports_degraded_and_503_on_redis_failure(client, monkeypatch):
    def _broken_ping():
        raise RedisError("redis is down")

    monkeypatch.setattr(main_module.redis_service._redis, "ping", _broken_ping)

    response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["services"]["redis"] == "unhealthy"
    assert "redis is down" not in str(body)
