from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

import app.core.database as database_module
import app.main as main_module
import app.services.redis_async as redis_async_module

# /health checks the database through app.core.database.AsyncSessionLocal
# and Redis through the async client (app/services/redis_async.py), the
# two non-blocking paths the rest of the app uses. conftest points the
# first at the test SQLite file and the second at fakeredis, so a plain
# request is a healthy one here.


def _forbid_blocking_checks(monkeypatch):
    # The synchronous engine and Redis client block the event loop for the
    # whole round trip; /health runs on that loop. Using either is a bug.
    def _blocking(*args, **kwargs):
        raise AssertionError("/health used a blocking client")

    monkeypatch.setattr(main_module.engine, "connect", _blocking)
    monkeypatch.setattr(main_module.redis_service._redis, "ping", _blocking)


def test_health_check_reports_healthy_when_everything_is_up(client, monkeypatch):
    _forbid_blocking_checks(monkeypatch)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "services": {"database": "healthy", "redis": "healthy"},
    }


def test_health_check_does_not_disclose_version_or_environment(client):
    body = client.get("/health").json()

    assert "version" not in body
    assert "environment" not in body


def test_health_check_reports_degraded_and_503_on_database_failure(client, monkeypatch):
    _forbid_blocking_checks(monkeypatch)

    class _BrokenSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def execute(self, *args, **kwargs):
            raise SQLAlchemyError("db is down")

    monkeypatch.setattr(database_module, "AsyncSessionLocal", _BrokenSession)

    response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["services"] == {"database": "unhealthy", "redis": "healthy"}
    # The response reports a generic status, never internal exception text
    # (connection strings, driver errors, etc.).
    assert "db is down" not in str(body)


def test_health_check_reports_degraded_and_503_on_redis_failure(client, monkeypatch):
    _forbid_blocking_checks(monkeypatch)

    class _BrokenRedis:
        async def ping(self):
            raise RedisError("redis is down")

    monkeypatch.setattr(redis_async_module, "_create_client", _BrokenRedis)

    response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["services"] == {"database": "healthy", "redis": "unhealthy"}
    assert "redis is down" not in str(body)
