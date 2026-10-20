"""Async Redis access for code that runs on the event loop.

app/services/redis.py (RedisService) is synchronous: every call blocks the
thread it runs on until Redis answers. That is fine in Celery tasks and in
scripts, but the rate limiter runs on every request and the auth dependency
on every authenticated one, both directly on the event loop, so each of
their round trips stopped the whole worker for its duration. This module is
the non-blocking path for those two callers. Both clients talk to the same
Redis database and use the same key formats, so a value written through one
is visible through the other.

Lifecycle
---------
A redis.asyncio connection belongs to the event loop that opened it (its
stream reader/writer are bound to that loop) and cannot be used from
another one. There is therefore no module-level client: get_async_redis()
hands out one client per running event loop, created on first use from
inside that loop.

- Under uvicorn each worker process runs one loop for its whole life, so
  this is one client and one connection pool per worker. Nothing is created
  at import time, so nothing is shared across a fork or a spawn either.
- The test suite runs requests on more than one loop (each TestClient
  context, each asyncio.run()). Every loop gets its own client; the entry
  of a loop that has since been closed is dropped the next time a client is
  created, because nothing can be awaited on a closed loop any more.
- close_async_redis() closes the client of the loop it is awaited on and is
  called from the application's shutdown handler, which uvicorn runs on the
  worker's loop after in-flight requests have finished.
"""

import asyncio
import json
import logging
import threading
from typing import Any, Dict, Optional, Union

from redis.asyncio import BlockingConnectionPool, Redis
from redis.exceptions import RedisError

from app.core.config import settings

logger = logging.getLogger(__name__)

# Key formats written by the sync side: RedisService.add_to_blacklist in
# app/services/redis.py and invalidate_tokens_issued_before_now in
# app/core/security.py. The readers below must stay in step with them.
BLACKLIST_KEY_PREFIX = "blacklist:"
PASSWORD_CHANGED_KEY_PREFIX = "pwd_changed:"

_clients: Dict[asyncio.AbstractEventLoop, Redis] = {}
# Each loop only ever touches its own entry, but loops can live in different
# threads (the test client runs the app in a worker thread), and the stale
# entry sweep below walks the whole dict.
_clients_lock = threading.Lock()


def _create_client() -> Redis:
    """Build a client with the same connection settings as RedisService's
    pool. Tests replace this function to hand out fakeredis clients.

    BlockingConnectionPool rather than the plain ConnectionPool: when every
    connection is in use the plain pool raises immediately, and with many
    requests in flight on one loop that is an ordinary moment, not a fault.
    The rate limiter would fail open and auth would fail closed on it. The
    blocking pool makes the request wait (yielding to the loop) for up to
    REDIS_POOL_TIMEOUT seconds instead.
    """
    pool = BlockingConnectionPool(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
        password=settings.REDIS_PASSWORD,
        decode_responses=True,
        max_connections=settings.REDIS_POOL_SIZE,
        timeout=settings.REDIS_POOL_TIMEOUT,
    )
    # from_pool: the client owns the pool, so aclose() also disconnects it.
    return Redis.from_pool(pool)


def get_async_redis() -> Redis:
    """The async Redis client of the running event loop.

    Must be called from a coroutine (raises RuntimeError otherwise). Creating
    the client opens no connection; that happens on its first command.
    """
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        with _clients_lock:
            client = _clients.get(loop)
            if client is None:
                for stale_loop in [known for known in _clients if known.is_closed()]:
                    del _clients[stale_loop]
                client = _create_client()
                _clients[loop] = client
    return client


async def close_async_redis() -> None:
    """Close the running loop's client and its connection pool, if it has
    one. Safe to call when no client was ever created on this loop."""
    loop = asyncio.get_running_loop()
    with _clients_lock:
        client = _clients.pop(loop, None)
    if client is not None:
        await client.aclose()


async def is_token_blacklisted(token_id: str) -> bool:
    """Async counterpart of RedisService.is_blacklisted (same `token_id`,
    from app.core.security._blacklist_id), with the same failure mode: if
    Redis cannot be asked, the token is treated as blacklisted. Rejecting a
    valid token for the length of an outage is the safer mistake than
    accepting a logged-out one."""
    try:
        return bool(await get_async_redis().exists(f"{BLACKLIST_KEY_PREFIX}{token_id}"))
    except RedisError as e:
        logger.error(f"Redis operation failed: is_token_blacklisted: {str(e)}")
        return True


async def get_password_changed_at(user_id: Union[str, int]) -> Optional[Any]:
    """The "password changed at" watermark for a user (a Unix timestamp), or
    None if there is none. Async counterpart of
    RedisService.get(f"pwd_changed:{user_id}"), including what that does on
    failure: a Redis error or an unreadable value yields None."""
    try:
        data = await get_async_redis().get(f"{PASSWORD_CHANGED_KEY_PREFIX}{user_id}")
    except RedisError as e:
        logger.error(f"Redis operation failed: get_password_changed_at: {str(e)}")
        return None
    if not data:
        return None
    try:
        # Stored by RedisService.setex, which JSON-encodes every value.
        return json.loads(data)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"Deserialization error: {str(e)}")
        return None
