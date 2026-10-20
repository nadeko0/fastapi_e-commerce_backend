# Redis key patterns, and who a cart belongs to
#
# The Cart/CartItem/CartResponse Pydantic models formerly defined in this
# module were dead code: app/api/v1/cart.py and app/services/redis.py both
# import their Cart/CartItem/CartResponse from app.schemas.common instead.
#
# A CART_TTL_DAYS = 7 constant also lived here and was the TTL really
# applied, while settings.REDIS_CART_TTL_DAYS looked like the knob but
# changed nothing. The settings are now the only source (REDIS_CART_TTL_DAYS
# for an account's cart, REDIS_GUEST_CART_TTL_DAYS for a guest's).
import re
import secrets
from dataclasses import dataclass
from typing import Optional, Union

# An account's cart: "cart:{user_id}".
CART_KEY_PREFIX = "cart:"
# A guest's cart: "guest_cart:{identifier}". A namespace of its own, and one
# that the pattern "cart:*" does not match, so nothing written for a guest
# can ever be read as an account's cart or the other way round.
GUEST_CART_KEY_PREFIX = "guest_cart:"

# The guest cart identifier is the only thing that stands between a stranger
# and that cart, so it is made by the server from the operating system's
# random source and from nothing else: 32 bytes, 256 bits (the brief asks
# for at least 128), written as 43 URL-safe base64 characters, which are
# also all legal in a cookie value and in a Redis key.
_GUEST_CART_ID_BYTES = 32
_GUEST_CART_ID_RE = re.compile(r"[A-Za-z0-9_-]{43}")


def new_guest_cart_id() -> str:
    return secrets.token_urlsafe(_GUEST_CART_ID_BYTES)


def is_guest_cart_id(value: object) -> bool:
    """Whether `value` has the shape of an identifier new_guest_cart_id()
    makes. Anything else a client sends is not looked up at all: it never
    becomes part of a Redis key."""
    return isinstance(value, str) and _GUEST_CART_ID_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class CartOwner:
    """Whose cart a request is about: an account (user_id) or a guest.

    A guest that has not been issued an identifier yet has neither field
    set. Such an owner has no key and therefore no stored cart; the first
    add issues the identifier (app/api/v1/cart.py)."""
    user_id: Optional[int] = None
    guest_id: Optional[str] = None

    @classmethod
    def account(cls, user_id: int) -> "CartOwner":
        return cls(user_id=user_id)

    @classmethod
    def guest(cls, guest_id: Optional[str] = None) -> "CartOwner":
        if guest_id is not None and not is_guest_cart_id(guest_id):
            raise ValueError("not a guest cart identifier")
        return cls(guest_id=guest_id)

    @property
    def is_guest(self) -> bool:
        return self.user_id is None

    @property
    def key(self) -> Optional[str]:
        """The Redis key of this owner's cart; None for a guest without an
        identifier."""
        if self.user_id is not None:
            return f"{CART_KEY_PREFIX}{self.user_id}"
        if self.guest_id is not None:
            return f"{GUEST_CART_KEY_PREFIX}{self.guest_id}"
        return None


# What the cart methods of RedisService take as their first argument: a
# CartOwner, or a bare user id from the callers that only ever deal with
# accounts (checkout in app/api/v1/orders.py).
CartOwnerRef = Union[int, CartOwner]


def as_cart_owner(owner: CartOwnerRef) -> CartOwner:
    return owner if isinstance(owner, CartOwner) else CartOwner.account(owner)
