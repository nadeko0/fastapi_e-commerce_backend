"""A Redis that accepts the connection and then never answers must not
block a caller forever. redis-py 8's pools default to 5 s for both
socket_timeout and socket_connect_timeout (checked against the installed
version), so the sync and async pools are pinned here to stay finite;
Celery's broker transport (kombu) passes socket_timeout=None unless told
otherwise, which is what the last test guards."""

import asyncio

from app.services.redis import RedisService
from app.services.redis_async import _create_client
from app.tasks import celery


def _finite(timeout):
    return timeout is not None and 0 < timeout <= 30


def test_sync_pool_has_finite_socket_timeouts(monkeypatch):
    # RedisService is a singleton the test suite has replaced with fakeredis;
    # build a real pool (no connection is opened) and let monkeypatch restore.
    monkeypatch.setattr(RedisService, "_instance", None)
    monkeypatch.setattr(RedisService, "_pool", None)
    RedisService()
    connection = RedisService._pool.make_connection()
    assert _finite(connection.socket_timeout)
    assert _finite(connection.socket_connect_timeout)


def test_async_pool_has_finite_socket_timeouts():
    async def scenario():
        client = _create_client()
        try:
            return client.connection_pool.make_connection()
        finally:
            await client.aclose()

    connection = asyncio.run(scenario())
    assert _finite(connection.socket_timeout)
    assert _finite(connection.socket_connect_timeout)


def test_celery_broker_has_a_socket_timeout():
    # kombu's Channel.socket_timeout defaults to None: a broker that accepts
    # the TCP connection and hangs would block publishers (the Stripe
    # webhook route publishes while Stripe waits) for ever.
    options = celery.conf.broker_transport_options
    assert _finite(options["socket_connect_timeout"])
    assert _finite(options["socket_timeout"])
