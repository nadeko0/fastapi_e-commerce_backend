import json
import logging
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

from app.core.clock import utc_now

if TYPE_CHECKING:
    from app.schemas.category import CategoryTreeResponse

# RedisError (not just ConnectionError) is caught throughout this file: every
# public method below is meant to fail open/degrade gracefully rather than
# raise out to its caller (cart/product/session endpoints), but ConnectionError
# alone only covers a dropped/refused connection - a socket TimeoutError or a
# ResponseError (both RedisError subclasses, distinct from ConnectionError)
# during an otherwise-live connection previously bypassed every except clause
# here and propagated as a raw, unhandled exception out of the request.
from redis import ConnectionPool, Redis, RedisError

from app.core.config import settings
from app.schemas.cart import (
    CartOwner,
    CartOwnerRef,
    as_cart_owner,
)
from app.schemas.common import Cart

# The Redis-free rules (serialization, TTLs, the cart mutations, the errors
# they raise) live in redis_codec and are shared with AsyncRedisService. The
# errors are re-exported here: callers import them from this module.
from app.services import redis_codec as codec
from app.services.redis_codec import (  # noqa: F401
    CartFullError,
    CartNotFoundError,
    InsufficientStockError,
    ItemNotInCartError,
)

logger = logging.getLogger(__name__)

class RedisService:
    """Synchronous Redis access, kept for Celery tasks (app/tasks.py) and
    other code outside the event loop. Routes use AsyncRedisService
    (app/services/redis_async.py), which shares the cart/cache logic through
    app/services/redis_codec.py and writes the same keys."""

    _instance = None
    _pool = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._pool = ConnectionPool(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                # Never passed before: settings.REDIS_PASSWORD exists and
                # docker-compose.yml's redis service requires one
                # (--requirepass), but this connection pool silently never
                # authenticated with it - every Redis operation against a
                # password-protected instance failed. Invisible locally
                # (tests use fakeredis, which doesn't enforce auth) until
                # actually deployed against docker-compose's real Redis.
                password=(
                    settings.REDIS_PASSWORD.get_secret_value() if settings.REDIS_PASSWORD is not None else None
                ),
                decode_responses=True,
                max_connections=settings.REDIS_POOL_SIZE
            )
        return cls._instance

    def __init__(self):
        self._redis: Redis = Redis(connection_pool=self._pool)

    # Every cart method below takes the cart's owner first. The parameter
    # is still called user_id, the name its account-only callers use (and
    # pass by keyword); it is a CartOwnerRef: a user id for an account's
    # cart, as before, or a CartOwner, which can also be a guest
    # (app/schemas/cart.py). The owner decides the key and the TTL, and
    # nothing else: the same code reads and changes both kinds of cart.

    def _get_cart_key(self, user_id: CartOwnerRef) -> str | None:
        return as_cart_owner(user_id).key

    def _cart_ttl(self, user_id: CartOwnerRef | None = None) -> timedelta:
        return codec.cart_ttl(user_id)

    def _serialize(self, data: Any) -> str:
        return codec.serialize(data)

    def _deserialize(self, data: str, model_class=None) -> Any:
        return codec.deserialize(data, model_class)

    def _handle_redis_error(self, operation: str) -> None:
        codec.log_redis_error(operation)

    def _load_cart(self, owner: CartOwner, cart_data: str | None) -> Cart | None:
        return codec.load_cart(owner, cart_data)

    def get_cart(self, user_id: CartOwnerRef) -> Cart | None:
        owner = as_cart_owner(user_id)
        if owner.key is None:
            # A guest without an identifier: there is nothing to look up.
            return None
        try:
            return self._load_cart(owner, self._redis.get(owner.key))
        except RedisError as e:
            self._handle_redis_error(f"get_cart: {str(e)}")
            return None

    def update_cart(self, cart: Cart) -> bool:
        try:
            cart_key = self._get_cart_key(cart.user_id)
            ttl = self._cart_ttl()
            cart.expires_at = utc_now() + ttl
            return self._redis.set(
                cart_key,
                self._serialize(cart),
                ex=ttl,
            )
        except RedisError as e:
            self._handle_redis_error(f"update_cart: {str(e)}")
            return False

    def delete_cart(self, user_id: int) -> bool:
        try:
            return bool(self._redis.delete(self._get_cart_key(user_id)))
        except RedisError as e:
            self._handle_redis_error(f"delete_cart: {str(e)}")
            return False

    # -- Atomic cart mutations -------------------------------------------
    #
    # get_cart/update_cart above are a plain GET then SETEX with no
    # compare-and-set guard: two concurrent mutations that both read the
    # same base cart will both compute their own "new state", and whichever
    # SETEX lands last silently clobbers the other (a lost update - e.g. two
    # concurrent +1 add-to-cart calls collapsing to +1 instead of +2).
    #
    # The methods below fix this with WATCH/MULTI via redis-py's
    # `Redis.transaction()` helper: it watches the cart key, lets the
    # mutator read-and-mutate a fresh copy, then EXECs a MULTI'd SETEX -
    # if another client wrote the key in between, EXEC fails, and
    # `transaction()` transparently retries the whole read-mutate-write
    # cycle. A Lua script (EVAL) would do the same read-modify-write
    # server-side in one round trip and was considered, but this repo's
    # tests run against fakeredis, whose EVAL support requires the
    # optional `lupa` dependency (not installed here) - WATCH/MULTI needs
    # nothing extra and fakeredis implements it faithfully, so it's the
    # better fit for this codebase.
    #
    # Stock-limit checks are done *inside* the mutator, against the
    # cart's item quantity as read on that attempt - not against a
    # quantity read earlier outside the transaction. Because
    # `transaction()` reruns the mutator on every WatchError retry, the
    # check is always against the freshest cart state, so concurrent adds
    # cannot collectively push a cart item's quantity past stock_quantity
    # (matching, and hardening, the existing single-request stock check -
    # no rollback/compensation is needed since nothing is written until
    # the check passes).

    def empty_cart(self, user_id: CartOwnerRef) -> Cart:
        """A new, empty Cart for this owner, held in memory only - nothing
        is written to Redis until a mutation stores it."""
        return codec.empty_cart(user_id)

    def _load_or_create_cart(self, user_id: CartOwnerRef, cart_data: str | None) -> Cart:
        return codec.load_or_create_cart(user_id, cart_data)

    def _atomic_mutate_cart(
        self, user_id: CartOwnerRef, mutator, must_exist: bool = False
    ) -> Cart | None:
        """Read-modify-write this owner's cart atomically.

        `mutator(cart)` is called with the current Cart (a fresh empty one
        if none exists yet) and must mutate it in place; it may raise to
        abort the whole operation, in which case nothing is written.
        `mutator` must be safe to call more than once - it will be
        re-invoked on a fresh cart read for every WatchError retry.

        With `must_exist`, a cart that is not stored is not created:
        CartNotFoundError is raised instead and nothing is written. That is
        how a change that can only make sense on an existing cart (update,
        remove, clear) avoids storing an empty one, and how an add under a
        guest identifier presented by a client is kept from creating a cart
        under an identifier the server did not just issue. A guest without
        an identifier has no key, hence no cart: always CartNotFoundError.

        A guest's cart that the mutation leaves empty is deleted rather
        than stored: Redis holds a guest cart only while it has lines in it.

        Returns None if Redis failed and nothing was written; the caller
        must not treat that as an empty cart.
        """
        owner = as_cart_owner(user_id)
        cart_key = owner.key
        if cart_key is None:
            raise CartNotFoundError()
        outcome: dict[str, Cart] = {}

        def _txn(pipe):
            cart_data = pipe.get(cart_key)
            write, cart = codec.plan_cart_mutation(owner, cart_data, mutator, must_exist)
            pipe.multi()
            if write.cart is None:
                pipe.delete(cart_key)
            else:
                pipe.set(cart_key, self._serialize(write.cart), ex=write.ttl)
            outcome['cart'] = cart

        try:
            self._redis.transaction(_txn, cart_key)
            return outcome.get('cart')
        except RedisError as e:
            self._handle_redis_error(f"_atomic_mutate_cart: {str(e)}")
            return None

    def add_to_cart_atomic(
        self,
        user_id: CartOwnerRef,
        product_id: int,
        quantity: int,
        price: Decimal | None,
        name: str | None,
        image: str | None,
        stock_quantity: int,
        variant_id: int | None = None,
        must_exist: bool = False,
    ) -> Cart | None:
        """Atomically merge `quantity` more of product_id (optionally, a
        specific variant_id) into the cart. A given product_id+variant_id
        pair merges into one line; a different variant_id (or no
        variant_id) is tracked as a distinct line.

        `price`, `name` and `image` are recorded on the line as what the
        customer was shown when adding (see CartItem); they are not what a
        cart response displays or totals.

        Raises InsufficientStockError (nothing written) if the resulting
        cart quantity for this item would exceed stock_quantity (the
        variant's own stock when variant_id is given, else the product's),
        CartFullError (nothing written) if this would be a new line in a
        cart already holding settings.CART_MAX_LINES lines, and, with
        `must_exist`, CartNotFoundError if no cart is stored for the owner
        (see _atomic_mutate_cart).
        """
        mutator = codec.add_mutator(
            product_id, quantity, price, name, image, stock_quantity, variant_id
        )
        return self._atomic_mutate_cart(user_id, mutator, must_exist=must_exist)

    def update_cart_quantity_atomic(
        self,
        user_id: CartOwnerRef,
        product_id: int,
        quantity: int,
        stock_quantity: int,
        variant_id: int | None = None,
    ) -> Cart | None:
        """Atomically set an existing cart item's quantity.

        Raises ItemNotInCartError if the item isn't in the cart, or
        InsufficientStockError if quantity exceeds stock_quantity (nothing
        written in either case).
        """
        mutator = codec.update_quantity_mutator(product_id, quantity, stock_quantity, variant_id)
        # No cart is no line to update: must_exist says so without first
        # storing an empty cart (which is all the difference it makes).
        try:
            return self._atomic_mutate_cart(user_id, mutator, must_exist=True)
        except CartNotFoundError:
            raise ItemNotInCartError(product_id) from None

    def remove_from_cart_atomic(
        self, user_id: CartOwnerRef, product_id: int, variant_id: int | None = None
    ) -> Cart | None:
        """Atomically remove an item from the cart.

        Raises ItemNotInCartError if the item isn't in the cart (nothing
        written).
        """
        try:
            return self._atomic_mutate_cart(
                user_id, codec.remove_item_mutator(product_id, variant_id), must_exist=True
            )
        except CartNotFoundError:
            raise ItemNotInCartError(product_id) from None

    def clear_cart_atomic(self, user_id: CartOwnerRef) -> Cart | None:
        """Atomically remove all items from the cart. A cart that does not
        exist is already clear: an empty one is returned and none is
        stored."""
        try:
            return self._atomic_mutate_cart(user_id, codec.clear_mutator, must_exist=True)
        except CartNotFoundError:
            return self.empty_cart(user_id)

    def merge_guest_cart(self, guest_id: str, user_id: int) -> bool:
        """Atomically move the lines of the guest cart `guest_id` into
        `user_id`'s cart and delete the guest cart (Cart.merge has the
        rule for a line present in both). Called when a login succeeds.

        Both keys are WATCHed, both carts are read, and the account cart's
        SETEX and the guest cart's DEL go out in one MULTI/EXEC: either
        both happen or neither does, and if anything writes to either cart
        between the reads and the EXEC, the EXEC fails and the whole thing
        is redone on fresh reads (Redis.transaction retries on WatchError),
        exactly like _atomic_mutate_cart. So an add that lands in the
        account cart or in the guest cart during a merge is never lost, a
        line is never merged twice, and two logins carrying the same guest
        cart merge it once: the second finds it gone.

        Returns True when no guest cart is left to merge afterwards: it was
        merged, or there was none, or it was empty (deleted), or it could
        not be read (left alone, to expire). The caller then retires the
        identifier. Returns False on a Redis error, with both carts exactly
        as they were; the merge can be attempted again at a later login.

        Both keys in one transaction requires them on one Redis node, which
        they are here (a single instance). On Redis Cluster they would need
        a common hash tag.
        """
        guest = CartOwner.guest(guest_id)
        account = CartOwner.account(user_id)
        guest_key, account_key = guest.key, account.key

        def _txn(pipe):
            action, guest_cart = codec.plan_guest_merge_read(guest, pipe.get(guest_key))
            if action == "nothing":
                # Nothing stored, or nothing readable: nothing is written.
                pipe.multi()
                return
            if action == "delete_guest":
                pipe.multi()
                pipe.delete(guest_key)
                return
            write = codec.plan_merge_into_account(account, user_id, guest_cart, pipe.get(account_key))
            pipe.multi()
            pipe.set(account_key, self._serialize(write.cart), ex=write.ttl)
            pipe.delete(guest_key)

        try:
            self._redis.transaction(_txn, guest_key, account_key)
            return True
        except RedisError as e:
            self._handle_redis_error(f"merge_guest_cart: {str(e)}")
            return False

    def remove_ordered_items_atomic(self, user_id: CartOwnerRef, ordered: Cart) -> bool:
        """Atomically take the lines of `ordered` - the cart exactly as
        checkout read it - out of the stored cart, once the order built
        from them is committed.

        Checkout used to delete_cart() here, which also threw away whatever
        had been added between its read and that delete without it ever
        being ordered. Subtracting only the ordered quantities leaves such
        late additions in the cart; when nothing else is left the key is
        deleted, as before.

        Returns False (stored cart untouched) on a Redis error.
        """
        owner = as_cart_owner(user_id)
        cart_key = owner.key

        def _txn(pipe):
            write = codec.plan_remove_ordered(owner, pipe.get(cart_key), ordered)
            pipe.multi()
            if write.cart is None:
                pipe.delete(cart_key)
                return
            pipe.set(cart_key, self._serialize(write.cart), ex=write.ttl)

        try:
            self._redis.transaction(_txn, cart_key)
            return True
        except RedisError as e:
            self._handle_redis_error(f"remove_ordered_items_atomic: {str(e)}")
            return False


    # token_id: what app.core.security._blacklist_id derives from a verified
    # access token, not the raw token string.
    def add_to_blacklist(self, token_id: str, expires_in: int) -> bool:
        try:
            key = f"blacklist:{token_id}"
            return self._redis.set(key, "1", ex=expires_in)
        except RedisError as e:
            self._handle_redis_error(f"add_to_blacklist: {str(e)}")
            return False

    def is_blacklisted(self, token_id: str) -> bool:
        try:
            return bool(self._redis.exists(f"blacklist:{token_id}"))
        except RedisError as e:
            self._handle_redis_error(f"is_blacklisted: {str(e)}")
            return True  # Safer to assume token is blacklisted on error

    def mark_once(self, key: str, ttl_seconds: int) -> bool:
        """Atomically claim `key` for a one-time side effect (e.g. "has this
        order's confirmation email already been sent").

        Returns True the first time `key` is claimed (the caller should
        proceed with the side effect) and False on every subsequent call
        while the key's TTL hasn't expired (the caller should skip the side
        effect - it already happened). Backed by SET key val NX EX ttl,
        which is atomic on the Redis server, so two callers racing to claim
        the same key can never both get True.

        On a connection error this fails *open* (returns True, i.e. "go
        ahead and do it") rather than closed: for a duplicate-suppression
        guard, false negatives (occasionally sending one extra email because
        Redis was briefly unreachable) are far less harmful than false
        positives (silently skipping an email because Redis looked
        unreachable) would be.
        """
        try:
            return bool(self._redis.set(key, "1", nx=True, ex=ttl_seconds))
        except RedisError as e:
            self._handle_redis_error(f"mark_once: {str(e)}")
            return True

    # -- Refresh token tracking (rotation + reuse detection) -----------------
    #
    # `refresh_active:{jti}` marks a refresh token jti as issued-and-not-yet-
    # redeemed. Redemption (consume_refresh_token) atomically reads-and-
    # deletes it: exactly one caller ever gets the data back, which is what
    # makes concurrent replay of the same token safe to detect. Every jti
    # ever issued for a given rotation chain is also recorded in
    # `refresh_family:{family_id}` so that if a jti is redeemed a second time
    # (a stolen-token signal - the legitimate rotation already consumed it)
    # the whole chain can be revoked, not just the one reused token.

    def register_refresh_token(self, jti: str, user_id: int, family_id: str, ttl_seconds: int) -> bool:
        try:
            key = f"refresh_active:{jti}"
            value = json.dumps({"user_id": user_id, "family": family_id})
            self._redis.set(key, value, ex=ttl_seconds)
            family_key = f"refresh_family:{family_id}"
            self._redis.sadd(family_key, jti)
            self._redis.expire(family_key, ttl_seconds)
            # Every family of the user, so that a logout without a refresh
            # token can end all of their sessions (revoke_user_refresh_families).
            # Its expiry follows the newest family: none outlives it.
            user_key = f"refresh_user:{user_id}"
            self._redis.sadd(user_key, family_id)
            self._redis.expire(user_key, ttl_seconds)
            return True
        except RedisError as e:
            self._handle_redis_error(f"register_refresh_token: {str(e)}")
            return False

    def consume_refresh_token(self, jti: str) -> dict[str, Any] | None:
        """Atomically redeem a refresh token jti (single use).

        Returns the token's {"user_id", "family"} metadata if this call was
        the first to redeem it, or None if it was never issued, has expired,
        or was already redeemed by an earlier call (replay). GET+DELETE run
        inside a Redis transaction so concurrent callers presenting the same
        jti cannot both receive data back.
        """
        try:
            key = f"refresh_active:{jti}"
            pipe = self._redis.pipeline()
            pipe.get(key)
            pipe.delete(key)
            data, deleted = pipe.execute()
            if not data or not deleted:
                return None
            return json.loads(data)
        except RedisError as e:
            self._handle_redis_error(f"consume_refresh_token: {str(e)}")
            return None

    def revoke_refresh_family(self, family_id: str) -> bool:
        """Revoke every jti ever issued in a rotation chain - used on reuse
        detection (stolen-token signal) and on logout."""
        try:
            family_key = f"refresh_family:{family_id}"
            members = self._redis.smembers(family_key)
            if members:
                keys = [f"refresh_active:{jti}" for jti in members]
                self._redis.delete(*keys)
            self._redis.delete(family_key)
            return True
        except RedisError as e:
            self._handle_redis_error(f"revoke_refresh_family: {str(e)}")
            return False

    def revoke_user_refresh_families(self, user_id: int) -> bool:
        """Revoke every rotation chain of a user ("sign out everywhere").
        Families issued before the per-user index existed are not in it:
        logout() also moves the user's token watermark, which refuses
        those (invalidate_tokens_issued_before_now)."""
        try:
            user_key = f"refresh_user:{user_id}"
            for family_id in self._redis.smembers(user_key):
                if not self.revoke_refresh_family(family_id):
                    return False
            self._redis.delete(user_key)
            return True
        except RedisError as e:
            self._handle_redis_error(f"revoke_user_refresh_families: {str(e)}")
            return False


    def cache_product(self, product_id: int, data: dict) -> bool:
        try:
            key = f"product:{product_id}"
            return self._redis.set(
                key,
                self._serialize(data),
                ex=codec.PRODUCT_CACHE_TTL,
            )
        except RedisError as e:
            self._handle_redis_error(f"cache_product: {str(e)}")
            return False

    def cache_category_tree(self, tree_data: 'CategoryTreeResponse') -> bool:
        try:
            serialized = self._serialize(tree_data)

            result = self._redis.set(
                "category:tree",
                serialized,
                ex=timedelta(hours=settings.REDIS_PRODUCT_CACHE_TTL_HOURS),
            )

            if not result:
                self._redis.delete("category:tree")
            return result
        except RedisError as e:
            self._handle_redis_error(f"cache_category_tree: {str(e)}")
            return False

    def get_cached_category_tree(self) -> Optional['CategoryTreeResponse']:
        try:
            from app.schemas.category import CategoryTreeResponse
            data = self._redis.get("category:tree")
            if not data:
                return None


            try:
                parsed = json.loads(data)
                if not isinstance(parsed, dict):
                    self._redis.delete("category:tree")
                    return None
            except json.JSONDecodeError:
                self._redis.delete("category:tree")
                return None

            return self._deserialize(data, CategoryTreeResponse)
        except RedisError as e:
            self._handle_redis_error(f"get_cached_category_tree: {str(e)}")
            return None

    def get_cached_product(self, product_id: int) -> dict | None:
        try:
            data = self._redis.get(f"product:{product_id}")
            return self._deserialize(data) if data else None
        except RedisError as e:
            self._handle_redis_error(f"get_cached_product: {str(e)}")
            return None


    def create_session(self, user_id: int, session_data: dict) -> str:
        try:
            session_id = f"session:{user_id}:{utc_now().timestamp()}"
            self._redis.set(
                session_id,
                self._serialize(session_data),
                ex=timedelta(hours=24),
            )
            return session_id
        except RedisError as e:
            self._handle_redis_error(f"create_session: {str(e)}")
            return ""

    def get_session(self, session_id: str) -> dict | None:
        try:
            data = self._redis.get(session_id)
            return self._deserialize(data) if data else None
        except RedisError as e:
            self._handle_redis_error(f"get_session: {str(e)}")
            return None

    def delete_session(self, session_id: str) -> bool:
        try:
            return bool(self._redis.delete(session_id))
        except RedisError as e:
            self._handle_redis_error(f"delete_session: {str(e)}")
            return False

    def delete(self, key: str) -> bool:
        try:
            return bool(self._redis.delete(key))
        except RedisError as e:
            self._handle_redis_error(f"delete: {str(e)}")
            return False

    def invalidate_category_cache(self) -> bool:
        try:
            return self.delete("category:tree")
        except RedisError as e:
            self._handle_redis_error(f"invalidate_category_cache: {str(e)}")
            return False

    def invalidate_product_cache(self, product_id: int) -> bool:
        try:
            return self.delete(f"product:{product_id}")
        except RedisError as e:
            self._handle_redis_error(f"invalidate_product_cache: {str(e)}")
            return False

    def get(self, key: str) -> Any | None:
        try:
            data = self._redis.get(key)
            return self._deserialize(data) if data else None
        except RedisError as e:
            self._handle_redis_error(f"get: {str(e)}")
            return None

    def setex(self, key: str, seconds: int, value: Any) -> bool:
        try:
            return self._redis.set(key, self._serialize(value), ex=seconds)
        except RedisError as e:
            self._handle_redis_error(f"setex: {str(e)}")
            return False
