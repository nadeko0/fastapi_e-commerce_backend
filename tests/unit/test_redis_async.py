import asyncio

import fakeredis
import pytest
from pydantic import SecretStr
from redis.asyncio import BlockingConnectionPool

import app.services.redis_async as redis_async
from app.core.config import settings
from app.services.redis import RedisService

# The autouse `fake_redis` fixture (tests/conftest.py) replaces
# redis_async._create_client with a factory for fakeredis clients on the
# same FakeServer as the sync RedisService, and starts every test with an
# empty client registry. Everything else here is the real module.

# Captured at import (collection) time, before any fixture has replaced it.
_real_create_client = redis_async._create_client


def _unreachable_client_factory():
    server = fakeredis.FakeServer()
    server.connected = False
    return lambda: fakeredis.FakeAsyncRedis(server=server, decode_responses=True)


# --- Lifecycle: one client per event loop ---------------------------------


def test_same_loop_gets_the_same_client():
    async def scenario():
        return redis_async.get_async_redis(), redis_async.get_async_redis()

    first, second = asyncio.run(scenario())

    assert first is second


def test_each_event_loop_gets_its_own_client():
    # A redis.asyncio connection cannot be used from a loop other than the
    # one that opened it, so a client must never be handed across loops.
    async def scenario():
        return redis_async.get_async_redis()

    first = asyncio.run(scenario())
    second = asyncio.run(scenario())

    assert first is not second


def test_client_of_a_closed_loop_is_dropped_when_the_next_one_is_created():
    async def scenario():
        redis_async.get_async_redis()
        return asyncio.get_running_loop()

    first_loop = asyncio.run(scenario())
    assert first_loop.is_closed()
    assert first_loop in redis_async._clients

    second_loop = asyncio.run(scenario())

    assert first_loop not in redis_async._clients
    assert list(redis_async._clients) == [second_loop]


def test_close_async_redis_closes_and_forgets_the_running_loops_client():
    closed = []

    async def scenario():
        client = redis_async.get_async_redis()
        await client.set("k", "v")
        original_aclose = client.aclose

        async def tracking_aclose(*args, **kwargs):
            closed.append(client)
            await original_aclose(*args, **kwargs)

        client.aclose = tracking_aclose
        await redis_async.close_async_redis()
        assert asyncio.get_running_loop() not in redis_async._clients
        # Usable again afterwards: a new client is created on demand.
        replacement = redis_async.get_async_redis()
        assert replacement is not client
        assert await replacement.get("k") == "v"
        return client

    client = asyncio.run(scenario())

    assert closed == [client]


def test_close_async_redis_without_a_client_is_a_no_op():
    asyncio.run(redis_async.close_async_redis())

    assert redis_async._clients == {}


def test_get_async_redis_outside_an_event_loop_is_an_error():
    with pytest.raises(RuntimeError):
        redis_async.get_async_redis()


def test_real_client_uses_the_same_connection_settings_as_the_sync_pool(monkeypatch):
    # The real factory, not the fake one the fixture installs. Building a
    # client opens no connection, so nothing here reaches a real Redis.
    monkeypatch.setattr(settings, "REDIS_PASSWORD", SecretStr("s3cret"))

    async def scenario():
        client = _real_create_client()
        try:
            return client.connection_pool
        finally:
            await client.aclose()

    pool = asyncio.run(scenario())
    sync_kwargs = RedisService._pool.connection_kwargs

    assert isinstance(pool, BlockingConnectionPool)
    assert pool.connection_kwargs["host"] == settings.REDIS_HOST == sync_kwargs["host"]
    assert pool.connection_kwargs["port"] == settings.REDIS_PORT == sync_kwargs["port"]
    assert pool.connection_kwargs["db"] == settings.REDIS_DB == sync_kwargs["db"]
    assert pool.connection_kwargs["password"] == "s3cret"
    assert pool.connection_kwargs["decode_responses"] is True
    assert pool.max_connections == settings.REDIS_POOL_SIZE
    assert pool.timeout == settings.REDIS_POOL_TIMEOUT


# --- One keyspace for the sync and the async client ------------------------


def test_token_blacklisted_through_the_sync_service_is_seen_by_the_async_reader():
    redis_service = RedisService()

    assert asyncio.run(redis_async.is_token_blacklisted("tok")) is False

    redis_service.add_to_blacklist("tok", 60)

    assert asyncio.run(redis_async.is_token_blacklisted("tok")) is True


def test_key_written_through_the_async_client_is_seen_by_the_sync_service():
    async def scenario():
        await redis_async.get_async_redis().set(
            f"{redis_async.BLACKLIST_KEY_PREFIX}tok", "1", ex=60
        )

    asyncio.run(scenario())

    assert RedisService().is_blacklisted("tok") is True


def test_password_changed_watermark_written_by_the_sync_service_is_read_back():
    RedisService().setex(f"{redis_async.PASSWORD_CHANGED_KEY_PREFIX}42", 60, 1767268800)

    superseded = redis_async.is_token_superseded
    assert asyncio.run(superseded(42, 1767268799)) is True
    assert asyncio.run(superseded("42", 1767268799)) is True
    # Issued in the same second as the change, or later: still good.
    assert asyncio.run(superseded(42, 1767268800)) is False
    assert asyncio.run(superseded(42, 1767268801)) is False
    # Another user has no watermark.
    assert asyncio.run(superseded(43, 1)) is False


def test_watermarks_and_issued_at_times_compare_below_the_second():
    # Both are written with sub-second precision now; what was written in
    # whole seconds before the change still compares the same way.
    RedisService().setex(f"{redis_async.PASSWORD_CHANGED_KEY_PREFIX}42", 60, 1767268800.5)
    superseded = redis_async.is_token_superseded
    assert asyncio.run(superseded(42, 1767268800.25)) is True
    assert asyncio.run(superseded(42, 1767268800.75)) is False
    # A token issued before the change carries a whole-second iat.
    assert asyncio.run(superseded(42, 1767268800)) is True
    assert asyncio.run(superseded(42, 1767268801)) is False
    # A watermark stored in whole seconds before the change.
    RedisService().setex(f"{redis_async.PASSWORD_CHANGED_KEY_PREFIX}43", 60, 1767268800)
    assert asyncio.run(superseded(43, 1767268799.9)) is True
    assert asyncio.run(superseded(43, 1767268800.1)) is False


def test_a_token_without_issued_at_predates_every_watermark():
    RedisService().setex(f"{redis_async.PASSWORD_CHANGED_KEY_PREFIX}42", 60, 1767268800)

    assert asyncio.run(redis_async.is_token_superseded(42, None)) is True
    assert asyncio.run(redis_async.is_token_superseded(43, None)) is False


# --- Failure modes (same as the sync methods they replace) -----------------


def test_is_token_blacklisted_cannot_tell_when_redis_is_unreachable(monkeypatch):
    # Neither True nor False: the caller must not mistake an outage for a
    # revoked token (it answers 503, not 401).
    monkeypatch.setattr(redis_async, "_create_client", _unreachable_client_factory())

    with pytest.raises(redis_async.TokenStateUnavailable):
        asyncio.run(redis_async.is_token_blacklisted("tok"))


def test_is_token_superseded_cannot_tell_when_redis_is_unreachable(monkeypatch):
    # Same question as the blacklist (has this token been revoked?), same
    # answer when it cannot be asked. It used to be "no".
    monkeypatch.setattr(redis_async, "_create_client", _unreachable_client_factory())

    with pytest.raises(redis_async.TokenStateUnavailable):
        asyncio.run(redis_async.is_token_superseded(42, 1767268800))


@pytest.mark.parametrize("stored", ["not json", '"a string"', "true", "[1]", "null"])
def test_is_token_superseded_fails_closed_for_an_unreadable_value(fake_redis, stored):
    fake_redis.set(f"{redis_async.PASSWORD_CHANGED_KEY_PREFIX}42", stored)

    assert asyncio.run(redis_async.is_token_superseded(42, 1767268800)) is True


# --- Application shutdown ---------------------------------------------------


def test_application_shutdown_closes_the_async_client_of_its_loop(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    created = []
    fake_factory = redis_async._create_client

    def _tracking_factory():
        client = fake_factory()
        created.append(client)
        return client

    monkeypatch.setattr(redis_async, "_create_client", _tracking_factory)
    closed = []

    with TestClient(app) as test_client:
        # Any request goes through the rate limiter, which creates the
        # client of the loop the app is running on.
        assert test_client.get("/api/v1/legal/privacy-policy").status_code == 200
        assert test_client.get("/api/v1/legal/privacy-policy").status_code == 200
        assert len(created) == 1
        assert len(redis_async._clients) == 1

        original_aclose = created[0].aclose

        async def _tracking_aclose(*args, **kwargs):
            closed.append(created[0])
            await original_aclose(*args, **kwargs)

        created[0].aclose = _tracking_aclose

    # Leaving the context runs the app's shutdown handler on that same loop.
    assert closed == created
    assert redis_async._clients == {}


def test_application_shutdown_survives_a_failing_redis_close(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    async def _failing_close():
        raise ConnectionError("redis went away first")

    monkeypatch.setattr("app.main.close_async_redis", _failing_close)

    with TestClient(app) as test_client:
        assert test_client.get("/api/v1/legal/privacy-policy").status_code == 200
