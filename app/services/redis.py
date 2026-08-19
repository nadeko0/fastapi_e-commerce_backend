import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from app.schemas.category import CategoryTreeResponse

from pydantic import ValidationError

# RedisError (not just ConnectionError) is caught throughout this file: every
# public method below is meant to fail open/degrade gracefully rather than
# raise out to its caller (cart/product/session endpoints), but ConnectionError
# alone only covers a dropped/refused connection - a socket TimeoutError or a
# ResponseError (both RedisError subclasses, distinct from ConnectionError)
# during an otherwise-live connection previously bypassed every except clause
# here and propagated as a raw, unhandled exception out of the request.
from redis import ConnectionPool, Redis, RedisError

from app.core.config import settings
from app.schemas.cart import CART_KEY_PREFIX, CART_TTL_DAYS
from app.schemas.common import Cart, CartItem, cart_item_key

logger = logging.getLogger(__name__)


class InsufficientStockError(Exception):
    """Raised by an atomic cart mutation when it would leave the item's
    cart quantity above the available stock."""

    def __init__(self, available: int, requested: int):
        self.available = available
        self.requested = requested
        super().__init__(
            f"Not enough stock: requested {requested}, available {available}"
        )


class ItemNotInCartError(Exception):
    """Raised by an atomic cart mutation (update/remove) when the target
    product isn't present in the cart at the moment the mutation runs."""

    def __init__(self, product_id: int):
        self.product_id = product_id
        super().__init__(f"Item not in cart: {product_id}")


class RedisService:
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
                password=settings.REDIS_PASSWORD,
                decode_responses=True,
                max_connections=50
            )
        return cls._instance

    def __init__(self):
        self._redis: Redis = Redis(connection_pool=self._pool)

    def _get_cart_key(self, user_id: int) -> str:
        return f"{CART_KEY_PREFIX}{user_id}"

    def _serialize(self, data: Any) -> str:
        if isinstance(data, (Cart, CartItem)) or hasattr(data, 'model_dump'):
            return json.dumps(data.model_dump(mode='json'), default=str)
        return json.dumps(data, default=str)

    def _deserialize(self, data: str, model_class=None) -> Any:
        if not data:
            return None

        try:
            parsed = json.loads(data)
            if model_class:

                if model_class.__name__ == 'Cart':
                    if 'items' not in parsed:
                        parsed['items'] = {}
                    elif not isinstance(parsed['items'], dict):

                        items_dict = {}
                        for item in parsed['items']:
                            items_dict[str(item['product_id'])] = item
                        parsed['items'] = items_dict


                    if 'expires_at' not in parsed:
                        parsed['expires_at'] = (datetime.utcnow() + timedelta(days=CART_TTL_DAYS)).isoformat()


                if model_class.__name__ == 'CategoryTreeResponse':
                    if isinstance(parsed, str):
                        parsed = {'tree': [], 'total_categories': 0, 'max_depth': 0}
                    elif not isinstance(parsed, dict):
                        parsed = parsed.dict() if hasattr(parsed, 'dict') else {'tree': [], 'total_categories': 0, 'max_depth': 0}
                return model_class.model_validate(parsed)
            return parsed
        except (json.JSONDecodeError, AttributeError, TypeError, KeyError, ValidationError) as e:
            # Logged via the app's logger (not print()) so this actually
            # reaches the JSON log pipeline in production instead of being
            # lost/interleaved on stdout - matches the reasoning in
            # app/services/email/smtp_provider.py's module docstring. Only
            # the exception string is logged, never the raw stored payload,
            # to avoid leaking cart/session contents into logs.
            logger.warning(f"Deserialization error: {str(e)}")
            return None

    def _handle_redis_error(self, operation: str) -> None:
        logger.error(f"Redis operation failed: {operation}")


    def get_cart(self, user_id: int) -> Optional[Cart]:
        try:
            cart_data = self._redis.get(self._get_cart_key(user_id))
            if cart_data:
                return self._deserialize(cart_data, Cart)
            return None
        except RedisError as e:
            self._handle_redis_error(f"get_cart: {str(e)}")
            return None

    def update_cart(self, cart: Cart) -> bool:
        try:
            cart_key = self._get_cart_key(cart.user_id)
            return self._redis.setex(
                cart_key,
                timedelta(days=CART_TTL_DAYS),
                self._serialize(cart)
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

    def _load_or_create_cart(self, user_id: int, cart_data: Optional[str]) -> Cart:
        cart = self._deserialize(cart_data, Cart) if cart_data else None
        if cart is not None:
            return cart
        now = datetime.utcnow()
        return Cart(
            user_id=user_id,
            items={},
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(days=CART_TTL_DAYS),
        )

    def _atomic_mutate_cart(self, user_id: int, mutator) -> Optional[Cart]:
        """Read-modify-write `user_id`'s cart atomically.

        `mutator(cart)` is called with the current Cart (a fresh empty one
        if none exists yet) and must mutate it in place; it may raise to
        abort the whole operation, in which case nothing is written.
        `mutator` must be safe to call more than once - it will be
        re-invoked on a fresh cart read for every WatchError retry.
        """
        cart_key = self._get_cart_key(user_id)
        outcome: Dict[str, Cart] = {}

        def _txn(pipe):
            cart_data = pipe.get(cart_key)
            cart = self._load_or_create_cart(user_id, cart_data)
            mutator(cart)
            pipe.multi()
            pipe.setex(cart_key, timedelta(days=CART_TTL_DAYS), self._serialize(cart))
            outcome['cart'] = cart

        try:
            self._redis.transaction(_txn, cart_key)
            return outcome.get('cart')
        except RedisError as e:
            self._handle_redis_error(f"_atomic_mutate_cart: {str(e)}")
            return None

    def add_to_cart_atomic(
        self,
        user_id: int,
        product_id: int,
        quantity: int,
        price: float,
        name: str,
        image: str,
        stock_quantity: int,
        variant_id: Optional[int] = None,
    ) -> Optional[Cart]:
        """Atomically merge `quantity` more of product_id (optionally, a
        specific variant_id) into the cart. A given product_id+variant_id
        pair merges into one line; a different variant_id (or no
        variant_id) is tracked as a distinct line.

        Raises InsufficientStockError (nothing written) if the resulting
        cart quantity for this item would exceed stock_quantity (the
        variant's own stock when variant_id is given, else the product's).
        """
        def _mutator(cart: Cart) -> None:
            str_id = cart_item_key(product_id, variant_id)
            current_quantity = cart.items[str_id].quantity if str_id in cart.items else 0
            if stock_quantity < current_quantity + quantity:
                raise InsufficientStockError(stock_quantity, current_quantity + quantity)
            cart.add_item(
                product_id=product_id,
                quantity=quantity,
                price=price,
                name=name,
                image=image,
                variant_id=variant_id,
            )

        return self._atomic_mutate_cart(user_id, _mutator)

    def update_cart_quantity_atomic(
        self,
        user_id: int,
        product_id: int,
        quantity: int,
        stock_quantity: int,
        variant_id: Optional[int] = None,
    ) -> Optional[Cart]:
        """Atomically set an existing cart item's quantity.

        Raises ItemNotInCartError if the item isn't in the cart, or
        InsufficientStockError if quantity exceeds stock_quantity (nothing
        written in either case).
        """
        def _mutator(cart: Cart) -> None:
            str_id = cart_item_key(product_id, variant_id)
            if str_id not in cart.items:
                raise ItemNotInCartError(product_id)
            if stock_quantity < quantity:
                raise InsufficientStockError(stock_quantity, quantity)
            cart.update_quantity(product_id, quantity, variant_id=variant_id)

        return self._atomic_mutate_cart(user_id, _mutator)

    def remove_from_cart_atomic(
        self, user_id: int, product_id: int, variant_id: Optional[int] = None
    ) -> Optional[Cart]:
        """Atomically remove an item from the cart.

        Raises ItemNotInCartError if the item isn't in the cart (nothing
        written).
        """
        def _mutator(cart: Cart) -> None:
            str_id = cart_item_key(product_id, variant_id)
            if str_id not in cart.items:
                raise ItemNotInCartError(product_id)
            cart.remove_item(product_id, variant_id=variant_id)

        return self._atomic_mutate_cart(user_id, _mutator)

    def clear_cart_atomic(self, user_id: int) -> Optional[Cart]:
        """Atomically remove all items from the cart."""
        def _mutator(cart: Cart) -> None:
            cart.clear()

        return self._atomic_mutate_cart(user_id, _mutator)


    def add_to_blacklist(self, token: str, expires_in: int) -> bool:
        try:
            key = f"blacklist:{token}"
            return self._redis.setex(key, expires_in, "1")
        except RedisError as e:
            self._handle_redis_error(f"add_to_blacklist: {str(e)}")
            return False

    def is_blacklisted(self, token: str) -> bool:
        try:
            return bool(self._redis.exists(f"blacklist:{token}"))
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
            self._redis.setex(key, ttl_seconds, value)
            family_key = f"refresh_family:{family_id}"
            self._redis.sadd(family_key, jti)
            self._redis.expire(family_key, ttl_seconds)
            return True
        except RedisError as e:
            self._handle_redis_error(f"register_refresh_token: {str(e)}")
            return False

    def consume_refresh_token(self, jti: str) -> Optional[Dict[str, Any]]:
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


    def cache_product(self, product_id: int, data: Dict) -> bool:
        try:
            key = f"product:{product_id}"
            return self._redis.setex(
                key,
                timedelta(hours=1),  # 1 hour cache as per requirements
                self._serialize(data)
            )
        except RedisError as e:
            self._handle_redis_error(f"cache_product: {str(e)}")
            return False

    def cache_category_tree(self, tree_data: 'CategoryTreeResponse') -> bool:
        try:
            serialized = self._serialize(tree_data)

            result = self._redis.setex(
                "category:tree",
                timedelta(hours=settings.REDIS_PRODUCT_CACHE_TTL_HOURS),
                serialized
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

    def get_cached_product(self, product_id: int) -> Optional[Dict]:
        try:
            data = self._redis.get(f"product:{product_id}")
            return self._deserialize(data) if data else None
        except RedisError as e:
            self._handle_redis_error(f"get_cached_product: {str(e)}")
            return None


    def create_session(self, user_id: int, session_data: Dict) -> str:
        try:
            session_id = f"session:{user_id}:{datetime.utcnow().timestamp()}"
            self._redis.setex(
                session_id,
                timedelta(hours=24),
                self._serialize(session_data)
            )
            return session_id
        except RedisError as e:
            self._handle_redis_error(f"create_session: {str(e)}")
            return ""

    def get_session(self, session_id: str) -> Optional[Dict]:
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

    def cleanup_expired_carts(self) -> int:
        try:
            pattern = f"{CART_KEY_PREFIX}*"
            cleaned = 0
            for key in self._redis.scan_iter(pattern):
                if not self._redis.ttl(key):
                    self._redis.delete(key)
                    cleaned += 1
            return cleaned
        except RedisError as e:
            self._handle_redis_error(f"cleanup_expired_carts: {str(e)}")
            return 0

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

    def get(self, key: str) -> Optional[Any]:
        try:
            data = self._redis.get(key)
            return self._deserialize(data) if data else None
        except RedisError as e:
            self._handle_redis_error(f"get: {str(e)}")
            return None

    def setex(self, key: str, seconds: int, value: Any) -> bool:
        try:
            return self._redis.setex(key, seconds, self._serialize(value))
        except RedisError as e:
            self._handle_redis_error(f"setex: {str(e)}")
            return False
