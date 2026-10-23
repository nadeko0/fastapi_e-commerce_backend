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
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

from redis.asyncio import BlockingConnectionPool, Redis
from redis.exceptions import RedisError

from app.core.clock import utc_now
from app.core.config import settings
from app.schemas.cart import CartOwner, CartOwnerRef, as_cart_owner
from app.schemas.common import Cart
from app.services import redis_codec as codec
from app.services.redis_codec import CartNotFoundError, ItemNotInCartError

if TYPE_CHECKING:
    from app.schemas.category import CategoryTreeResponse

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
        password=(
            settings.REDIS_PASSWORD.get_secret_value() if settings.REDIS_PASSWORD is not None else None
        ),
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


async def is_token_superseded(user_id: Union[str, int], issued_at: Optional[Any]) -> bool:
    """Whether a token of this user issued at `issued_at` (its `iat`) predates
    the user's "password changed at" watermark, written by
    invalidate_tokens_issued_before_now in app/core/security.py on a
    password change and on signing out everywhere.

    Fails closed, like is_token_blacklisted: both ask whether a token has
    been revoked, and when Redis cannot be asked - or the watermark cannot
    be read - the token is treated as revoked. This used to read the
    watermark the way RedisService.get does, as None on any error, so a
    token issued before a password reset was accepted again whenever the
    read failed. A token without an `iat` predates every watermark."""
    try:
        data = await get_async_redis().get(f"{PASSWORD_CHANGED_KEY_PREFIX}{user_id}")
    except RedisError as e:
        logger.error(f"Redis operation failed: is_token_superseded: {str(e)}")
        return True
    if not data:
        return False
    try:
        # Stored by RedisService.setex, which JSON-encodes every value.
        changed_at = json.loads(data)
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"Unreadable password-change watermark, token refused: {str(e)}")
        return True
    if isinstance(changed_at, bool) or not isinstance(changed_at, (int, float)):
        logger.error("Unreadable password-change watermark, token refused: not a timestamp")
        return True
    return not isinstance(issued_at, (int, float)) or issued_at < changed_at


class AsyncRedisService:
    """RedisService for coroutines: the same operations, key formats and
    failure mapping (RedisError -> False/None, a blacklist read failure
    means blacklisted), awaiting redis.asyncio instead of blocking.

    What gets stored, and the rules for changing a cart, are not here but in
    app/services/redis_codec.py, shared with the sync RedisService; this
    class only does the I/O. It holds no connection of its own: each call
    uses the client of the running event loop (get_async_redis), so an
    instance is cheap and may be created per request (get_cache_service).

    See RedisService for the reasoning behind each operation.
    """

    def __init__(self, client: Optional[Redis] = None):
        self._client = client

    @property
    def _redis(self) -> Redis:
        return self._client if self._client is not None else get_async_redis()

    @staticmethod
    def _handle_redis_error(operation: str) -> None:
        codec.log_redis_error(operation)

    def empty_cart(self, user_id: CartOwnerRef) -> Cart:
        return codec.empty_cart(user_id)

    # -- Carts -------------------------------------------------------------

    async def get_cart(self, user_id: CartOwnerRef) -> Optional[Cart]:
        owner = as_cart_owner(user_id)
        if owner.key is None:
            # A guest without an identifier: there is nothing to look up.
            return None
        try:
            return codec.load_cart(owner, await self._redis.get(owner.key))
        except RedisError as e:
            self._handle_redis_error(f"get_cart: {str(e)}")
            return None

    async def update_cart(self, cart: Cart) -> bool:
        try:
            cart_key = as_cart_owner(cart.user_id).key
            ttl = codec.cart_ttl()
            cart.expires_at = utc_now() + ttl
            return await self._redis.set(cart_key, codec.serialize(cart), ex=ttl)
        except RedisError as e:
            self._handle_redis_error(f"update_cart: {str(e)}")
            return False

    async def delete_cart(self, user_id: int) -> bool:
        try:
            return bool(await self._redis.delete(as_cart_owner(user_id).key))
        except RedisError as e:
            self._handle_redis_error(f"delete_cart: {str(e)}")
            return False

    async def _atomic_mutate_cart(
        self, user_id: CartOwnerRef, mutator, must_exist: bool = False
    ) -> Optional[Cart]:
        """Async twin of RedisService._atomic_mutate_cart: WATCH, read,
        decide (codec.plan_cart_mutation), MULTI, SET or DELETE, EXEC,
        retried by Redis.transaction on a WatchError. `mutator` must be safe
        to call more than once. Returns None on a Redis error (nothing
        written)."""
        owner = as_cart_owner(user_id)
        cart_key = owner.key
        if cart_key is None:
            raise CartNotFoundError()
        outcome: Dict[str, Cart] = {}

        async def _txn(pipe):
            cart_data = await pipe.get(cart_key)
            write, cart = codec.plan_cart_mutation(owner, cart_data, mutator, must_exist)
            pipe.multi()
            if write.cart is None:
                pipe.delete(cart_key)
            else:
                pipe.set(cart_key, codec.serialize(write.cart), ex=write.ttl)
            outcome["cart"] = cart

        try:
            await self._redis.transaction(_txn, cart_key)
            return outcome.get("cart")
        except RedisError as e:
            self._handle_redis_error(f"_atomic_mutate_cart: {str(e)}")
            return None

    async def add_to_cart_atomic(
        self,
        user_id: CartOwnerRef,
        product_id: int,
        quantity: int,
        price: Optional[Decimal],
        name: Optional[str],
        image: Optional[str],
        stock_quantity: int,
        variant_id: Optional[int] = None,
        must_exist: bool = False,
    ) -> Optional[Cart]:
        mutator = codec.add_mutator(
            product_id, quantity, price, name, image, stock_quantity, variant_id
        )
        return await self._atomic_mutate_cart(user_id, mutator, must_exist=must_exist)

    async def update_cart_quantity_atomic(
        self,
        user_id: CartOwnerRef,
        product_id: int,
        quantity: int,
        stock_quantity: int,
        variant_id: Optional[int] = None,
    ) -> Optional[Cart]:
        mutator = codec.update_quantity_mutator(product_id, quantity, stock_quantity, variant_id)
        try:
            return await self._atomic_mutate_cart(user_id, mutator, must_exist=True)
        except CartNotFoundError:
            raise ItemNotInCartError(product_id)

    async def remove_from_cart_atomic(
        self, user_id: CartOwnerRef, product_id: int, variant_id: Optional[int] = None
    ) -> Optional[Cart]:
        try:
            return await self._atomic_mutate_cart(
                user_id, codec.remove_item_mutator(product_id, variant_id), must_exist=True
            )
        except CartNotFoundError:
            raise ItemNotInCartError(product_id)

    async def clear_cart_atomic(self, user_id: CartOwnerRef) -> Optional[Cart]:
        try:
            return await self._atomic_mutate_cart(user_id, codec.clear_mutator, must_exist=True)
        except CartNotFoundError:
            return self.empty_cart(user_id)

    async def merge_guest_cart(self, guest_id: str, user_id: int) -> bool:
        """Async twin of RedisService.merge_guest_cart (both keys WATCHed,
        one MULTI/EXEC). True when no guest cart is left to merge, False on
        a Redis error with both carts untouched."""
        guest = CartOwner.guest(guest_id)
        account = CartOwner.account(user_id)
        guest_key, account_key = guest.key, account.key

        async def _txn(pipe):
            action, guest_cart = codec.plan_guest_merge_read(guest, await pipe.get(guest_key))
            if action == "nothing":
                pipe.multi()
                return
            if action == "delete_guest":
                pipe.multi()
                pipe.delete(guest_key)
                return
            write = codec.plan_merge_into_account(
                account, user_id, guest_cart, await pipe.get(account_key)
            )
            pipe.multi()
            pipe.set(account_key, codec.serialize(write.cart), ex=write.ttl)
            pipe.delete(guest_key)

        try:
            await self._redis.transaction(_txn, guest_key, account_key)
            return True
        except RedisError as e:
            self._handle_redis_error(f"merge_guest_cart: {str(e)}")
            return False

    async def remove_ordered_items_atomic(self, user_id: CartOwnerRef, ordered: Cart) -> bool:
        """Async twin of RedisService.remove_ordered_items_atomic. False
        (stored cart untouched) on a Redis error."""
        owner = as_cart_owner(user_id)
        cart_key = owner.key

        async def _txn(pipe):
            write = codec.plan_remove_ordered(owner, await pipe.get(cart_key), ordered)
            pipe.multi()
            if write.cart is None:
                pipe.delete(cart_key)
                return
            pipe.set(cart_key, codec.serialize(write.cart), ex=write.ttl)

        try:
            await self._redis.transaction(_txn, cart_key)
            return True
        except RedisError as e:
            self._handle_redis_error(f"remove_ordered_items_atomic: {str(e)}")
            return False

    # -- Tokens ------------------------------------------------------------

    async def add_to_blacklist(self, token_id: str, expires_in: int) -> bool:
        try:
            return await self._redis.set(f"{BLACKLIST_KEY_PREFIX}{token_id}", "1", ex=expires_in)
        except RedisError as e:
            self._handle_redis_error(f"add_to_blacklist: {str(e)}")
            return False

    async def is_blacklisted(self, token_id: str) -> bool:
        try:
            return bool(await self._redis.exists(f"{BLACKLIST_KEY_PREFIX}{token_id}"))
        except RedisError as e:
            self._handle_redis_error(f"is_blacklisted: {str(e)}")
            return True  # Safer to assume token is blacklisted on error

    async def mark_once(self, key: str, ttl_seconds: int) -> bool:
        """SET NX EX; fails open (True) on a Redis error, like the sync one."""
        try:
            return bool(await self._redis.set(key, "1", nx=True, ex=ttl_seconds))
        except RedisError as e:
            self._handle_redis_error(f"mark_once: {str(e)}")
            return True

    async def register_refresh_token(
        self, jti: str, user_id: int, family_id: str, ttl_seconds: int
    ) -> bool:
        try:
            value = json.dumps({"user_id": user_id, "family": family_id})
            await self._redis.set(f"refresh_active:{jti}", value, ex=ttl_seconds)
            family_key = f"refresh_family:{family_id}"
            await self._redis.sadd(family_key, jti)
            await self._redis.expire(family_key, ttl_seconds)
            user_key = f"refresh_user:{user_id}"
            await self._redis.sadd(user_key, family_id)
            await self._redis.expire(user_key, ttl_seconds)
            return True
        except RedisError as e:
            self._handle_redis_error(f"register_refresh_token: {str(e)}")
            return False

    async def consume_refresh_token(self, jti: str) -> Optional[Dict[str, Any]]:
        """Atomically redeem a refresh token jti (GET + DELETE in one
        MULTI/EXEC): its {"user_id", "family"} for the first caller, None
        for a replay, an unknown or expired jti, or a Redis error."""
        try:
            key = f"refresh_active:{jti}"
            async with self._redis.pipeline() as pipe:
                pipe.get(key)
                pipe.delete(key)
                data, deleted = await pipe.execute()
            if not data or not deleted:
                return None
            return json.loads(data)
        except RedisError as e:
            self._handle_redis_error(f"consume_refresh_token: {str(e)}")
            return None

    async def revoke_refresh_family(self, family_id: str) -> bool:
        try:
            family_key = f"refresh_family:{family_id}"
            members = await self._redis.smembers(family_key)
            if members:
                await self._redis.delete(*[f"refresh_active:{jti}" for jti in members])
            await self._redis.delete(family_key)
            return True
        except RedisError as e:
            self._handle_redis_error(f"revoke_refresh_family: {str(e)}")
            return False

    async def revoke_user_refresh_families(self, user_id: int) -> bool:
        try:
            user_key = f"refresh_user:{user_id}"
            for family_id in await self._redis.smembers(user_key):
                if not await self.revoke_refresh_family(family_id):
                    return False
            await self._redis.delete(user_key)
            return True
        except RedisError as e:
            self._handle_redis_error(f"revoke_user_refresh_families: {str(e)}")
            return False

    # -- Cache -------------------------------------------------------------

    async def cache_product(self, product_id: int, data: Dict) -> bool:
        try:
            return await self._redis.set(
                f"product:{product_id}", codec.serialize(data), ex=codec.PRODUCT_CACHE_TTL
            )
        except RedisError as e:
            self._handle_redis_error(f"cache_product: {str(e)}")
            return False

    async def cache_category_tree(self, tree_data: "CategoryTreeResponse") -> bool:
        try:
            result = await self._redis.set(
                codec.CATEGORY_TREE_KEY,
                codec.serialize(tree_data),
                ex=timedelta(hours=settings.REDIS_PRODUCT_CACHE_TTL_HOURS),
            )
            if not result:
                await self._redis.delete(codec.CATEGORY_TREE_KEY)
            return result
        except RedisError as e:
            self._handle_redis_error(f"cache_category_tree: {str(e)}")
            return False

    async def get_cached_category_tree(self) -> Optional["CategoryTreeResponse"]:
        try:
            from app.schemas.category import CategoryTreeResponse

            data = await self._redis.get(codec.CATEGORY_TREE_KEY)
            if not data:
                return None
            if not codec.is_category_tree_payload_valid(data):
                await self._redis.delete(codec.CATEGORY_TREE_KEY)
                return None
            return codec.deserialize(data, CategoryTreeResponse)
        except RedisError as e:
            self._handle_redis_error(f"get_cached_category_tree: {str(e)}")
            return None

    async def get_cached_product(self, product_id: int) -> Optional[Dict]:
        try:
            data = await self._redis.get(f"product:{product_id}")
            return codec.deserialize(data) if data else None
        except RedisError as e:
            self._handle_redis_error(f"get_cached_product: {str(e)}")
            return None

    async def delete(self, key: str) -> bool:
        try:
            return bool(await self._redis.delete(key))
        except RedisError as e:
            self._handle_redis_error(f"delete: {str(e)}")
            return False

    async def invalidate_category_cache(self) -> bool:
        return await self.delete(codec.CATEGORY_TREE_KEY)

    async def invalidate_product_cache(self, product_id: int) -> bool:
        return await self.delete(f"product:{product_id}")

    async def get(self, key: str) -> Optional[Any]:
        try:
            data = await self._redis.get(key)
            return codec.deserialize(data) if data else None
        except RedisError as e:
            self._handle_redis_error(f"get: {str(e)}")
            return None

    async def setex(self, key: str, seconds: int, value: Any) -> bool:
        try:
            return await self._redis.set(key, codec.serialize(value), ex=seconds)
        except RedisError as e:
            self._handle_redis_error(f"setex: {str(e)}")
            return False


async def get_cache_service() -> AsyncRedisService:
    """FastAPI dependency: the async counterpart of the sync
    `Depends(RedisService)`. A coroutine so that get_async_redis() (which
    needs a running loop) is only ever reached from one."""
    return AsyncRedisService()
