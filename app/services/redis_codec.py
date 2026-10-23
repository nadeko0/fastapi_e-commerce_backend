"""The Redis-free part of the cart and cache logic.

RedisService (app/services/redis.py, synchronous: Celery and the code that
has not moved to the event loop yet) and AsyncRedisService
(app/services/redis_async.py, for coroutines) differ only in how they talk
to Redis. Everything that decides what is stored lives here once, so the
two cannot drift apart: key formats and TTLs, (de)serialization, the cart
mutation rules (stock check, line cap, merge, subtracting an order) and the
errors those raise. Nothing in this module performs I/O.

The atomic cart operations are split in the same way. A WATCH/MULTI
transaction reads the stored value, decides, then writes; the reads and the
write are the caller's I/O, the decision is a function here that takes the
values read and returns what to write (a CartWrite).
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from app.core.clock import utc_now
from app.core.config import settings
from app.schemas.cart import CartOwner, CartOwnerRef, as_cart_owner
from app.schemas.common import Cart, CartItem, cart_item_key

# One logger for both services, under the name these warnings have always had.
logger = logging.getLogger("app.services.redis")

CATEGORY_TREE_KEY = "category:tree"
PRODUCT_CACHE_TTL = timedelta(hours=1)
SESSION_TTL = timedelta(hours=24)


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


def cart_ttl(user_id: CartOwnerRef | None = None) -> timedelta:
    # Read from settings on every call rather than cached in a module
    # constant: the key's TTL and Cart.expires_at both come from here,
    # so they cannot drift apart again (a hard-coded 7 used to be what
    # was applied, whatever REDIS_CART_TTL_DAYS said). A guest's cart
    # has its own setting; with no owner given it is an account's.
    if user_id is not None and as_cart_owner(user_id).is_guest:
        return timedelta(days=settings.REDIS_GUEST_CART_TTL_DAYS)
    return timedelta(days=settings.REDIS_CART_TTL_DAYS)


def serialize(data: Any) -> str:
    if isinstance(data, (Cart, CartItem)) or hasattr(data, 'model_dump'):
        return data.model_dump_json()
    return json.dumps(data, default=str)


def deserialize(data: str, model_class=None) -> Any:
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
                    parsed['expires_at'] = (utc_now() + cart_ttl()).isoformat()


            if model_class.__name__ == 'CategoryTreeResponse':
                if isinstance(parsed, str):
                    parsed = {'tree': [], 'total_categories': 0, 'max_depth': 0}
                elif not isinstance(parsed, dict):
                    parsed = parsed.model_dump() if hasattr(parsed, 'model_dump') else {'tree': [], 'total_categories': 0, 'max_depth': 0}
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


def log_redis_error(operation: str) -> None:
    logger.error(f"Redis operation failed: {operation}")


def load_cart(owner: CartOwner, cart_data: str | None) -> Cart | None:
    """The Cart stored as `cart_data` under `owner`'s key, or None when
    there is none or it cannot be read. A value that names another
    owner than its key does (an account cart must carry that account's
    user_id, a guest cart none) is unreadable like any other value that
    is not this owner's cart."""
    cart = deserialize(cart_data, Cart) if cart_data else None
    if cart is not None and cart.user_id != owner.user_id:
        logger.warning("Stored cart does not belong to the key it was read from")
        return None
    return cart


def empty_cart(user_id: CartOwnerRef) -> Cart:
    """A new, empty Cart for this owner, held in memory only - nothing
    is written to Redis until a mutation stores it."""
    owner = as_cart_owner(user_id)
    now = utc_now()
    return Cart(
        user_id=owner.user_id,
        items={},
        created_at=now,
        updated_at=now,
        expires_at=now + cart_ttl(owner),
    )


def load_or_create_cart(user_id: CartOwnerRef, cart_data: str | None) -> Cart:
    owner = as_cart_owner(user_id)
    cart = load_cart(owner, cart_data)
    if cart is not None:
        return cart
    return empty_cart(owner)


@dataclass
class CartWrite:
    """What a transaction body decided: store `cart` under the key with
    `ttl`, or delete the key (`cart` is None)."""

    cart: Cart | None = None
    ttl: timedelta | None = None


def plan_cart_mutation(
    owner: CartOwner, cart_data: str | None, mutator: Callable[[Cart], None], must_exist: bool
) -> tuple[CartWrite, Cart]:
    """The decision half of _atomic_mutate_cart, given the value read.

    Returns the write to queue and the mutated cart (the method's result).
    Raises CartNotFoundError when `must_exist` and nothing is stored, and
    whatever `mutator` raises; nothing is written then. A guest's cart that
    the mutation leaves empty is deleted rather than stored: Redis holds a
    guest cart only while it has lines in it."""
    if must_exist and not cart_data:
        raise CartNotFoundError()
    cart = load_or_create_cart(owner, cart_data)
    mutator(cart)
    # This write re-arms the key's TTL, so expires_at moves with it;
    # it used to keep the value from the day the cart was created.
    ttl = cart_ttl(owner)
    cart.expires_at = utc_now() + ttl
    if owner.is_guest and not cart.items:
        return CartWrite(), cart
    return CartWrite(cart=cart, ttl=ttl), cart


def add_mutator(
    product_id: int,
    quantity: int,
    price: Decimal | None,
    name: str | None,
    image: str | None,
    stock_quantity: int,
    variant_id: int | None = None,
) -> Callable[[Cart], None]:
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

    return _mutator


def update_quantity_mutator(
    product_id: int, quantity: int, stock_quantity: int, variant_id: int | None = None
) -> Callable[[Cart], None]:
    def _mutator(cart: Cart) -> None:
        str_id = cart_item_key(product_id, variant_id)
        if str_id not in cart.items:
            raise ItemNotInCartError(product_id)
        if stock_quantity < quantity:
            raise InsufficientStockError(stock_quantity, quantity)
        cart.update_quantity(product_id, quantity, variant_id=variant_id)

    return _mutator


def remove_item_mutator(product_id: int, variant_id: int | None = None) -> Callable[[Cart], None]:
    def _mutator(cart: Cart) -> None:
        str_id = cart_item_key(product_id, variant_id)
        if str_id not in cart.items:
            raise ItemNotInCartError(product_id)
        cart.remove_item(product_id, variant_id=variant_id)

    return _mutator


def clear_mutator(cart: Cart) -> None:
    cart.clear()


def plan_guest_merge_read(guest: CartOwner, guest_data: str | None) -> tuple[str, Cart | None]:
    """First half of merge_guest_cart: what the guest cart read says.

    Returns (action, guest_cart). Action is "nothing" (none stored, or not
    readable: nothing is written), "delete_guest" (stored but empty) or
    "merge" (the caller now reads the account cart and calls
    plan_merge_into_account)."""
    guest_cart = load_cart(guest, guest_data)
    if guest_cart is None:
        return "nothing", None
    if not guest_cart.items:
        return "delete_guest", guest_cart
    return "merge", guest_cart


def plan_merge_into_account(
    account: CartOwner, user_id: int, guest_cart: Cart, account_data: str | None
) -> CartWrite:
    cart = load_or_create_cart(account, account_data)
    left_out = cart.merge(guest_cart, settings.CART_MAX_LINES)
    if left_out:
        logger.warning(
            "Guest cart merge: %d line(s) did not fit in the cart of user %s (limit %d)",
            left_out, user_id, settings.CART_MAX_LINES,
        )
    ttl = cart_ttl(account)
    cart.expires_at = utc_now() + ttl
    return CartWrite(cart=cart, ttl=ttl)


def plan_remove_ordered(owner: CartOwner, cart_data: str | None, ordered: Cart) -> CartWrite:
    """Subtract the ordered quantities from the stored cart. The returned
    write has cart None (delete the key) when nothing is left."""
    cart = load_cart(owner, cart_data)
    if cart is not None:
        for str_id, ordered_item in ordered.items.items():
            line = cart.items.get(str_id)
            if line is None:
                continue
            if line.quantity <= ordered_item.quantity:
                del cart.items[str_id]
            else:
                line.quantity -= ordered_item.quantity
    if cart is None or not cart.items:
        return CartWrite()
    ttl = cart_ttl(owner)
    cart.updated_at = utc_now()
    cart.expires_at = cart.updated_at + ttl
    return CartWrite(cart=cart, ttl=ttl)


def is_category_tree_payload_valid(data: str) -> bool:
    """False for a stored category tree that is not a JSON object: the
    caller deletes the key and reports a miss."""
    try:
        return isinstance(json.loads(data), dict)
    except json.JSONDecodeError:
        return False
