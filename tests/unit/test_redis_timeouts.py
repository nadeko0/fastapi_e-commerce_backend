"""A Redis that accepts the connection and then never answers must not
block a caller for long. Both application pools take their socket timeouts
from REDIS_SOCKET_TIMEOUT and REDIS_SOCKET_CONNECT_TIMEOUT (redis-py 8's own
default, 5 s, held every request touching a frozen Redis for 5 s per
command). Celery's broker transport (kombu) passes socket_timeout=None
unless told otherwise, which is what the last test guards."""

import asyncio

import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.services.redis import RedisService
from app.services.redis_async import _create_client
from app.tasks import celery


def _finite(timeout):
    return timeout is not None and 0 < timeout <= 30


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_SOCKET_TIMEOUT", 0.7)
    monkeypatch.setattr(settings, "REDIS_SOCKET_CONNECT_TIMEOUT", 0.4)


def test_the_defaults_are_one_second():
    assert Settings.model_fields["REDIS_SOCKET_TIMEOUT"].default == 1.0
    assert Settings.model_fields["REDIS_SOCKET_CONNECT_TIMEOUT"].default == 1.0


@pytest.mark.parametrize("name", ["REDIS_SOCKET_TIMEOUT", "REDIS_SOCKET_CONNECT_TIMEOUT"])
@pytest.mark.parametrize("value", [0, -1])
def test_a_timeout_must_be_positive(name, value):
    with pytest.raises(ValidationError, match=name):
        Settings(_env_file=None, **{name: value})


def test_sync_pool_uses_the_configured_socket_timeouts(monkeypatch, configured):
    # RedisService is a singleton the test suite has replaced with fakeredis;
    # build a real pool (no connection is opened) and let monkeypatch restore.
    monkeypatch.setattr(RedisService, "_instance", None)
    monkeypatch.setattr(RedisService, "_pool", None)
    RedisService()
    connection = RedisService._pool.make_connection()
    assert connection.socket_timeout == 0.7
    assert connection.socket_connect_timeout == 0.4


def test_async_pool_uses_the_configured_socket_timeouts(configured):
    async def scenario():
        client = _create_client()
        try:
            return client.connection_pool.make_connection()
        finally:
            await client.aclose()

    connection = asyncio.run(scenario())
    assert connection.socket_timeout == 0.7
    assert connection.socket_connect_timeout == 0.4


def test_celery_broker_has_a_socket_timeout():
    # kombu's Channel.socket_timeout defaults to None: a broker that accepts
    # the TCP connection and hangs would block publishers (the Stripe
    # webhook route publishes while Stripe waits) for ever.
    options = celery.conf.broker_transport_options
    assert _finite(options["socket_connect_timeout"])
    assert _finite(options["socket_timeout"])
