import asyncio

import pytest
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

import app.core.database as database_module
import app.core.probes as probes_module
import app.main as main_module
import app.services.redis_async as redis_async_module
from app.core.config import settings

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
    assert response.headers["Retry-After"]
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
    assert response.headers["Retry-After"]
    body = response.json()
    assert body["status"] == "degraded"
    assert body["services"] == {"database": "healthy", "redis": "unhealthy"}
    assert "redis is down" not in str(body)


# --- ST-01, ST-02, ST-04: no-store, HEAD, and the /livez and /readyz split --------

PROBES = ("/health", "/livez", "/readyz")


def _break_database(monkeypatch):
    class _BrokenSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def execute(self, *args, **kwargs):
            raise SQLAlchemyError("db is down")

    monkeypatch.setattr(database_module, "AsyncSessionLocal", _BrokenSession)


def _break_redis(monkeypatch):
    class _BrokenRedis:
        async def ping(self):
            raise RedisError("redis is down")

    monkeypatch.setattr(redis_async_module, "_create_client", _BrokenRedis)


@pytest.mark.parametrize("path", PROBES)
def test_probes_are_never_cached(client, path):
    assert client.get(path).headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", PROBES)
def test_probes_answer_head_with_headers_and_no_body(client, path):
    response = client.head(path)

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("path", ["/health", "/readyz"])
def test_failing_probes_keep_no_store_and_answer_head(client, monkeypatch, path):
    _break_redis(monkeypatch)

    for response in (client.get(path), client.head(path)):
        assert response.status_code == 503
        assert response.headers["Cache-Control"] == "no-store"
    assert client.head(path).content == b""


def test_livez_is_200_without_touching_database_or_redis(client, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("/livez touched a dependency")

    _break_database(monkeypatch)
    _break_redis(monkeypatch)
    monkeypatch.setattr(probes_module, "database_is_up", _boom)
    monkeypatch.setattr(probes_module, "redis_is_up", _boom)

    response = client.get("/livez")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_is_200_when_database_and_redis_answer(client):
    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}
    assert "Retry-After" not in response.headers


def test_readyz_is_503_with_retry_after_when_the_database_is_down(client, monkeypatch):
    _break_database(monkeypatch)

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(probes_module.RETRY_AFTER_SECONDS)
    assert response.json() == {"status": "fail", "checks": {"database": "fail", "redis": "ok"}}
    assert "db is down" not in response.text


def test_readyz_is_503_with_retry_after_when_redis_is_down(client, monkeypatch):
    _break_redis(monkeypatch)

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(probes_module.RETRY_AFTER_SECONDS)
    assert response.json() == {"status": "fail", "checks": {"database": "ok", "redis": "fail"}}
    assert "redis is down" not in response.text


def test_readyz_counts_a_hung_dependency_as_down(client, monkeypatch):
    class _HungRedis:
        async def ping(self):
            await asyncio.sleep(30)

        async def aclose(self):
            pass

    monkeypatch.setattr(redis_async_module, "_create_client", _HungRedis)
    monkeypatch.setattr(probes_module, "READINESS_TIMEOUT_SECONDS", 1.0)

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "ok", "redis": "fail"}


def test_probes_are_not_rate_limited(client, monkeypatch, fake_redis):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_DEFAULT", "1/minute")
    for _ in range(3):
        for path in PROBES:
            assert client.get(path).status_code == 200
            assert client.head(path).status_code == 200
    assert fake_redis.keys("rl:*") == []
