import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
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
from app.schemas.cart import (
    CartOwner,
    CartOwnerRef,
    as_cart_owner,
)
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


class CartNotFoundError(Exception):
    """Raised by an atomic cart mutation that was told not to create a cart
    (must_exist) when there is none stored under the owner's key. Nothing
    is written."""


class CartFullError(Exception):
    """Raised by add_to_cart_atomic when the add would be a new line in a
    cart that already holds settings.CART_MAX_LINES of them. Nothing is
    written."""

    def __init__(self, max_lines: int):
        self.max_lines = max_lines
        super().__init__(f"Cart cannot hold more than {max_lines} lines")


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

    # Every cart method below takes the cart's owner first. The parameter
    # is still called user_id, the name its account-only callers use (and
    # pass by keyword); it is a CartOwnerRef: a user id for an account's
    # cart, as before, or a CartOwner, which can also be a guest
    # (app/schemas/cart.py). The owner decides the key and the TTL, and
    # nothing else: the same code reads and changes both kinds of cart.

    def _get_cart_key(self, user_id: CartOwnerRef) -> Optional[str]:
        return as_cart_owner(user_id).key

    def _cart_ttl(self, user_id: Optional[CartOwnerRef] = None) -> timedelta:
        # Read from settings on every call rather than cached in a module
        # constant: the key's TTL and Cart.expires_at both come from here,
        # so they cannot drift apart again (a hard-coded 7 used to be what
        # was applied, whatever REDIS_CART_TTL_DAYS said). A guest's cart
        # has its own setting; with no owner given it is an account's.
        if user_id is not None and as_cart_owner(user_id).is_guest:
            return timedelta(days=settings.REDIS_GUEST_CART_TTL_DAYS)
        return timedelta(days=settings.REDIS_CART_TTL_DAYS)

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
                        parsed['expires_at'] = (datetime.utcnow() + self._cart_ttl()).isoformat()


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


    def _load_cart(self, owner: CartOwner, cart_data: Optional[str]) -> Optional[Cart]:
        """The Cart stored as `cart_data` under `owner`'s key, or None when
        there is none or it cannot be read. A value that names another
        owner than its key does (an account cart must carry that account's
        user_id, a guest cart none) is unreadable like any other value that
        is not this owner's cart."""
        cart = self._deserialize(cart_data, Cart) if cart_data else None
        if cart is not None and cart.user_id != owner.user_id:
            logger.warning("Stored cart does not belong to the key it was read from")
            return None
        return cart

    def get_cart(self, user_id: CartOwnerRef) -> Optional[Cart]:
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
            cart.expires_at = datetime.utcnow() + ttl
            return self._redis.setex(
                cart_key,
                ttl,
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

    def empty_cart(self, user_id: CartOwnerRef) -> Cart:
        """A new, empty Cart for this owner, held in memory only - nothing
        is written to Redis until a mutation stores it."""
        owner = as_cart_owner(user_id)
        now = datetime.utcnow()
        return Cart(
            user_id=owner.user_id,
            items={},
            created_at=now,
            updated_at=now,
            expires_at=now + self._cart_ttl(owner),
        )

    def _load_or_create_cart(self, user_id: CartOwnerRef, cart_data: Optional[str]) -> Cart:
        owner = as_cart_owner(user_id)
        cart = self._load_cart(owner, cart_data)
        if cart is not None:
            return cart
        return self.empty_cart(owner)

    def _atomic_mutate_cart(
        self, user_id: CartOwnerRef, mutator, must_exist: bool = False
    ) -> Optional[Cart]:
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
        outcome: Dict[str, Cart] = {}

        def _txn(pipe):
            cart_data = pipe.get(cart_key)
            if must_exist and not cart_data:
                raise CartNotFoundError()
            cart = self._load_or_create_cart(owner, cart_data)
            mutator(cart)
            # This write re-arms the key's TTL, so expires_at moves with it;
            # it used to keep the value from the day the cart was created.
            ttl = self._cart_ttl(owner)
            cart.expires_at = datetime.utcnow() + ttl
            pipe.multi()
            if owner.is_guest and not cart.items:
                pipe.delete(cart_key)
            else:
                pipe.setex(cart_key, ttl, self._serialize(cart))
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
        price: Optional[Decimal],
        name: Optional[str],
        image: Optional[str],
        stock_quantity: int,
        variant_id: Optional[int] = None,
        must_exist: bool = False,
    ) -> Optional[Cart]:
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
        def _mutator(cart: Cart) -> None:
            str_id = cart_item_key(product_id, variant_id)
            # More of a line already in the cart is always allowed; only a
            # new line counts against the cap. A cart that somehow holds
            # more than the cap still loads and can be reduced.
            if str_id not in cart.items and len(cart.items) >= settings.CART_MAX_LINES:
                raise CartFullError(settings.CART_MAX_LINES)
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

        return self._atomic_mutate_cart(user_id, _mutator, must_exist=must_exist)

    def update_cart_quantity_atomic(
        self,
        user_id: CartOwnerRef,
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

        # No cart is no line to update: must_exist says so without first
        # storing an empty cart (which is all the difference it makes).
        try:
            return self._atomic_mutate_cart(user_id, _mutator, must_exist=True)
        except CartNotFoundError:
            raise ItemNotInCartError(product_id)

    def remove_from_cart_atomic(
        self, user_id: CartOwnerRef, product_id: int, variant_id: Optional[int] = None
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

        try:
            return self._atomic_mutate_cart(user_id, _mutator, must_exist=True)
        except CartNotFoundError:
            raise ItemNotInCartError(product_id)

    def clear_cart_atomic(self, user_id: CartOwnerRef) -> Optional[Cart]:
        """Atomically remove all items from the cart. A cart that does not
        exist is already clear: an empty one is returned and none is
        stored."""
        def _mutator(cart: Cart) -> None:
            cart.clear()

        try:
            return self._atomic_mutate_cart(user_id, _mutator, must_exist=True)
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
            guest_data = pipe.get(guest_key)
            guest_cart = self._load_cart(guest, guest_data)
            if guest_cart is None:
                # Nothing stored, or nothing readable: nothing is written.
                pipe.multi()
                return
            if not guest_cart.items:
                pipe.multi()
                pipe.delete(guest_key)
                return
            cart = self._load_or_create_cart(account, pipe.get(account_key))
            left_out = cart.merge(guest_cart, settings.CART_MAX_LINES)
            if left_out:
                logger.warning(
                    "Guest cart merge: %d line(s) did not fit in the cart of user %s (limit %d)",
                    left_out, user_id, settings.CART_MAX_LINES,
                )
            ttl = self._cart_ttl(account)
            cart.expires_at = datetime.utcnow() + ttl
            pipe.multi()
            pipe.setex(account_key, ttl, self._serialize(cart))
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
            cart_data = pipe.get(cart_key)
            cart = self._load_cart(owner, cart_data)
            if cart is not None:
                for str_id, ordered_item in ordered.items.items():
                    line = cart.items.get(str_id)
                    if line is None:
                        continue
                    if line.quantity <= ordered_item.quantity:
                        del cart.items[str_id]
                    else:
                        line.quantity -= ordered_item.quantity
            pipe.multi()
            if cart is None or not cart.items:
                pipe.delete(cart_key)
                return
            ttl = self._cart_ttl(owner)
            cart.updated_at = datetime.utcnow()
            cart.expires_at = cart.updated_at + ttl
            pipe.setex(cart_key, ttl, self._serialize(cart))

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
            return self._redis.setex(key, expires_in, "1")
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
