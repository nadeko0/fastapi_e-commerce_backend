import logging
from typing import Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_async_db
from app.core.security import get_optional_active_user
from app.models.product import Product, ProductVariant
from app.models.user import User
from app.schemas.cart import CartOwner, is_guest_cart_id, new_guest_cart_id
from app.schemas.common import (
    CART_LINE_INSUFFICIENT_STOCK,
    CART_LINE_PRODUCT_UNAVAILABLE,
    CART_LINE_VARIANT_UNAVAILABLE,
    APIResponse,
    Cart,
    CartPriceChange,
    CartResponse,
    CartValidationIssue,
    CartValidationResponse,
    cart_unit_price,
    resolve_cart_line,
)
from app.services.redis import (
    CartFullError,
    CartNotFoundError,
    InsufficientStockError,
    ItemNotInCartError,
    RedisService,
)

logger = logging.getLogger(__name__)

# How this router prices a cart
#
# A stored cart line says what the customer wants: product, optional
# variant, quantity. It also keeps the price, name and image shown when it
# was added, but only as a record. Every response that returns a cart is
# priced here, on the server, from the catalog as it is at that moment
# (_priced_cart): two queries, one for all the products and one for all the
# variants, whatever the number of lines. An order charges the live price
# too (create_order in app/api/v1/orders.py), by the same rule
# (cart_unit_price), so what the cart shows is what an order placed now
# would charge.
#
# Every product and variant read below goes through AsyncSession
# (get_async_db), as in app/api/v1/products.py, and the same two rules
# apply: every query is awaited, and nothing may touch an unloaded
# relationship (only columns are read here). RedisService is still the
# synchronous client, so its calls do block the event loop for one local
# round trip each; converting it is a separate step.
#
# Nothing below depends on whose cart it is. The owner is resolved once
# (get_cart_owner), and pricing and validation work on the Cart alone.
#
# Whose cart it is
#
# Every route here serves two kinds of caller through that one dependency.
# A request with an Authorization header is an account request: it is
# authenticated exactly as on any other route (401 for a bad token, never a
# fallback to a guest cart) and works on the account's cart, "cart:{id}".
# A request without one is a guest request and works on the cart named by
# the guest cart cookie, "guest_cart:{identifier}", if there is one.
#
# The guest cart identifier
#
# It is a bearer capability: whoever presents it has that cart, so it is
# made by the server (new_guest_cart_id, 256 random bits), never chosen by
# the client and never derived from anything.
#
# - Issued lazily. Looking at a cart, validating it, or changing a line
#   that is not there costs a guest nothing in Redis and sets no cookie.
#   The identifier is made in the request whose add first has something to
#   store, and the cart is written under it in that same request.
# - Never accepted from outside. An add under a presented identifier only
#   goes into a cart that is already stored under it; if there is none
#   (expired, never issued, made up) the add goes into a new cart under a
#   new identifier and the cookie is replaced. So the only identifiers ever
#   used as a key are ones this server generated. A value of the wrong
#   shape is not looked up at all.
# - Lives exactly as long as the cart. Every write to a guest cart re-arms
#   the key's TTL (REDIS_GUEST_CART_TTL_DAYS) and sets the cookie again with
#   the same Max-Age. A guest cart left with no lines is deleted and the
#   cookie cleared. At login the cart is merged into the account's and the
#   cookie cleared (merge_guest_cart_on_login).
#
# Why a cookie, and why this cookie
#
# The identifier travels in a cookie set by the server and in nothing else:
# not in a response body, not in a header a script could read.
#
# - HttpOnly: page script cannot read it, so an XSS in the shop front end
#   cannot carry the identifier off and use the cart from elsewhere (or
#   merge it into an account of the attacker's choosing). With a header
#   pair the front end would have to keep the identifier where script can
#   reach it. It would also not work across origins as this app is set up:
#   CORS exposes no custom response header to a browser.
# - SameSite=Lax: the cookie is ambient, so cross-site requests are the
#   risk (CSRF). With Lax a browser does not attach it to a cross-site
#   POST, PUT or DELETE, which covers every route that changes a cart and
#   the login that merges it: another site can neither change a visitor's
#   cart nor log the visitor's browser into its own account and collect the
#   cart. It is attached to a cross-site top-level GET, where the only
#   cart route is a read whose response the other site cannot see. Lax
#   rather than Strict so that a server-rendered front end still receives
#   the cart on the first navigation in from an external link.
# - Secure: never sent over plain http. settings.GUEST_CART_COOKIE_SECURE
#   exists for local development only.
# - The __Host- name prefix (with Secure, Path=/ and no Domain, which it
#   requires): a browser then refuses to let any other host, a sibling
#   subdomain included, set or overwrite this cookie, which closes the way
#   to plant a known identifier in a visitor's browser. No Domain also
#   means the cookie goes to this exact host only.
# - Max-Age = the cart's TTL, so the browser forgets the identifier when
#   Redis forgets the cart.
#
# A native app's HTTP client keeps and returns the cookie like a browser
# does (a cookie jar is standard in URLSession, OkHttp and the like), so
# there is one transport and one set of rules for every client.
#
# What SameSite does not cover: "site" is the registrable domain, so a page
# on a sibling subdomain of the shop is same-site and its requests do carry
# the cookie. Such a page could add to or empty a visitor's guest cart (it
# cannot read it: CORS). Checking the Origin header of guest writes against
# BACKEND_CORS_ORIGINS would close that; it is not done here.

_GUEST_CART_COOKIE = "guest_cart"


def guest_cart_cookie_name() -> str:
    # A browser rejects a __Host- cookie that is not Secure, so the prefix
    # goes with the attribute.
    if settings.GUEST_CART_COOKIE_SECURE:
        return f"__Host-{_GUEST_CART_COOKIE}"
    return _GUEST_CART_COOKIE


def _guest_cart_cookie_attributes() -> dict:
    return {
        "path": "/",
        "secure": settings.GUEST_CART_COOKIE_SECURE,
        "httponly": True,
        "samesite": "lax",
    }


def read_guest_cart_id(request: Request) -> Optional[str]:
    """The guest cart identifier the request carries, or None when it
    carries none or something that is not one."""
    value = request.cookies.get(guest_cart_cookie_name())
    return value if is_guest_cart_id(value) else None


def _set_guest_cart_cookie(response: Response, guest_id: str) -> None:
    response.set_cookie(
        guest_cart_cookie_name(),
        guest_id,
        max_age=settings.REDIS_GUEST_CART_TTL_DAYS * 24 * 60 * 60,
        **_guest_cart_cookie_attributes(),
    )


def _clear_guest_cart_cookie(response: Response) -> None:
    response.delete_cookie(guest_cart_cookie_name(), **_guest_cart_cookie_attributes())


def _sync_guest_cart_cookie(response: Response, owner: CartOwner, cart: Cart) -> None:
    """After a write to a guest's cart: renew the cookie alongside the key,
    or clear it when the cart was left empty and so is no longer stored."""
    if not owner.is_guest:
        return
    if cart.items:
        _set_guest_cart_cookie(response, owner.guest_id)
    elif owner.guest_id is not None:
        _clear_guest_cart_cookie(response)


def merge_guest_cart_on_login(
    request: Request, response: Response, user_id: int, redis: RedisService
) -> None:
    """Called by login (app/api/v1/users.py) once the user is authenticated:
    if the request carries a guest cart cookie, merge that cart into the
    account's and retire the identifier.

    It cannot fail the login. If Redis is down the user is logged in all
    the same, both carts are as they were, and the cookie is left in place
    so that the next login tries again. Otherwise the cookie is cleared:
    after a merge, and also when it named no cart at all (or was not an
    identifier), since it is then of no further use to the client.

    Redis only: no database session is involved.
    """
    name = guest_cart_cookie_name()
    if name not in request.cookies:
        return
    guest_id = read_guest_cart_id(request)
    if guest_id is not None:
        try:
            merged = redis.merge_guest_cart(guest_id, user_id)
        except Exception:
            # RedisService already turns a Redis error into False; this is
            # for anything else. A problem with a cart is not a reason to
            # refuse someone who has just proved who they are.
            logger.exception("Guest cart merge failed unexpectedly for user %s", user_id)
            merged = False
        if not merged:
            logger.warning("Guest cart not merged at login of user %s; left for a later login", user_id)
            return
    _clear_guest_cart_cookie(response)

_UNAVAILABLE_MESSAGES = {
    CART_LINE_PRODUCT_UNAVAILABLE: "Product no longer available",
    CART_LINE_VARIANT_UNAVAILABLE: "Product variant no longer available",
}


async def _get_active_product(db: AsyncSession, product_id: int) -> Product:
    """Look up product_id, raising 404 if it doesn't exist or has been
    deactivated. is_active was not checked here before, so a product
    withdrawn from sale could still be put in a cart by its id."""
    product = (await db.scalars(
        select(Product).where(
            Product.id == product_id,
            Product.is_active,
        )
    )).first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )
    return product


async def _get_active_variant_for_product(
    db: AsyncSession, product_id: int, variant_id: int
) -> ProductVariant:
    """Look up variant_id and validate it belongs to product_id and is
    active. Raises 404 otherwise - a variant_id for a different product,
    an inactive variant, or an unknown id are all "not a valid variant to
    add to cart for this product" from the caller's perspective."""
    variant = (await db.scalars(
        select(ProductVariant).where(
            ProductVariant.id == variant_id,
            ProductVariant.product_id == product_id,
            ProductVariant.is_active,
        )
    )).first()
    if not variant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product variant not found"
        )
    return variant


async def _load_catalog(
    db: AsyncSession, cart: Cart
) -> Tuple[Dict[int, Product], Dict[int, ProductVariant]]:
    """The product and variant rows behind `cart`'s lines, keyed by id: one
    query for all the products and one for all the variants, and none at
    all for what the cart does not contain. Inactive rows are included; it
    is resolve_cart_line that decides what a line's state is. An id with no
    row is simply absent from the result."""
    product_ids = {item.product_id for item in cart.items.values()}
    variant_ids = {
        item.variant_id for item in cart.items.values() if item.variant_id is not None
    }

    products: Dict[int, Product] = {}
    if product_ids:
        rows = await db.scalars(select(Product).where(Product.id.in_(sorted(product_ids))))
        products = {product.id: product for product in rows}

    variants: Dict[int, ProductVariant] = {}
    if variant_ids:
        rows = await db.scalars(
            select(ProductVariant).where(ProductVariant.id.in_(sorted(variant_ids)))
        )
        variants = {variant.id: variant for variant in rows}

    return products, variants


async def _priced_cart(db: AsyncSession, cart: Cart) -> CartResponse:
    products, variants = await _load_catalog(db, cart)
    return CartResponse.from_cart(cart, products, variants)


def _cart_or_503(cart: Optional[Cart]) -> Cart:
    """The atomic cart mutations return None when Redis failed and nothing
    was written. That None used to go straight into CartResponse.from_cart
    and surface as a 500 (AttributeError on None.items); a 503 tells the
    client what actually happened - nothing changed, retry."""
    if cart is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cart is temporarily unavailable, please retry"
        )
    return cart

def _cart_full(error: CartFullError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Cart cannot hold more than {error.max_lines} different items"
    )


router = APIRouter(prefix="/cart", tags=["cart"])

async def get_cart_owner(
    request: Request,
    current_user: Optional[User] = Depends(get_optional_active_user),
) -> CartOwner:
    """Whose cart this request is about: the one place that decides it.
    See "Whose cart it is" at the top of this module."""
    if current_user is not None:
        # An account request. A guest cart cookie it may also carry is
        # ignored here; it is dealt with at login.
        return CartOwner.account(current_user.id)
    return CartOwner.guest(read_guest_cart_id(request))

async def get_cart(
    owner: CartOwner = Depends(get_cart_owner),
    redis: RedisService = Depends(),
) -> Cart:
    # A missing cart is answered with an empty one built in memory, not
    # stored. This used to SETEX the empty cart, unconditionally and
    # outside the WATCH/MULTI guard the mutations use: an add landing
    # between the GET and that SETEX was overwritten, and a stored cart
    # that merely failed to parse was replaced for good. The first add
    # creates the key (RedisService._load_or_create_cart). For a guest
    # without an identifier there is not even a read.
    return redis.get_cart(owner) or redis.empty_cart(owner)

@router.get("", response_model=APIResponse[CartResponse])
async def get_user_cart(
    cart: Cart = Depends(get_cart),
    db: AsyncSession = Depends(get_async_db),
):
    """Retrieve the caller's cart, priced from the catalog as it is now:
    the account's cart with an `Authorization` header, the guest cart named
    by the guest cart cookie without one (empty if there is none). A line
    whose product or variant is no longer on sale is returned with
    `available: false` and is not part of `total_amount`."""

    return APIResponse.success_response(await _priced_cart(db, cart))

@router.post(
    "/items",
    response_model=APIResponse[CartResponse],
    responses={
        400: {"description": "Not enough stock available, or the cart already holds the maximum number of different items"},
        404: {"description": "Product not found or inactive, or variant_id doesn't belong to product_id / isn't active"},
        503: {"description": "Cart store unavailable, nothing was changed"},
    },
)
async def add_to_cart(
    response: Response,
    product_id: int,
    # ge=1: 0 or a negative quantity was accepted, and could take a line to
    # zero or below - which made the whole stored cart unreadable.
    quantity: int = Query(1, ge=1),
    variant_id: Optional[int] = None,
    owner: CartOwner = Depends(get_cart_owner),
    db: AsyncSession = Depends(get_async_db),
    redis: RedisService = Depends(),
):
    """Add a product (optionally, a specific variant) to the caller's cart,
    or increase its quantity. A given product_id+variant_id pair merges
    quantities; a different variant_id is tracked as a separate cart line.

    Without an `Authorization` header this is a guest's cart. The first
    successful add creates it and sets the guest cart cookie (HttpOnly);
    the client only has to send cookies back."""

    product = await _get_active_product(db, product_id)

    variant = None
    if variant_id is not None:
        variant = await _get_active_variant_for_product(db, product_id, variant_id)

    stock_quantity = variant.stock_quantity if variant else product.stock_quantity

    line = dict(
        product_id=product.id,
        quantity=quantity,
        # Recorded on the line as what the customer is being shown now,
        # so a later response can say the price has changed. Not what
        # the cart displays: the response below is priced live.
        price=cart_unit_price(product, variant),
        name=product.name,
        # "" rather than None when there is no image, as before: a
        # worker still running the previous code during a deploy (or
        # after a rollback) requires a string here and would otherwise
        # read the whole cart as unreadable.
        image=product.images[0] if product.images else "",
        stock_quantity=stock_quantity,
        variant_id=variant.id if variant else None,
    )

    try:
        cart = None
        stored = False
        if owner.is_guest and owner.guest_id is not None:
            # A guest presenting an identifier: only into a cart that is
            # stored under it. Whether it is, is decided inside the
            # transaction, so the cart cannot expire in between and be
            # recreated under an identifier that came from the client.
            try:
                cart = redis.add_to_cart_atomic(owner, must_exist=True, **line)
                stored = True
            except CartNotFoundError:
                pass
        if not stored:
            if owner.is_guest:
                # This guest's first line: the identifier is made here, by
                # the server, in the request that stores the cart under it.
                owner = CartOwner.guest(new_guest_cart_id())
            cart = redis.add_to_cart_atomic(owner, **line)
    except InsufficientStockError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not enough stock available"
        )
    except CartFullError as error:
        raise _cart_full(error)

    cart = _cart_or_503(cart)
    _sync_guest_cart_cookie(response, owner, cart)
    return APIResponse.success_response(await _priced_cart(db, cart))

@router.put(
    "/items/{product_id}",
    response_model=APIResponse[CartResponse],
    responses={
        400: {"description": "Not enough stock available"},
        404: {"description": "Product not found or inactive, item not in cart, or variant_id invalid for this product"},
        503: {"description": "Cart store unavailable, nothing was changed"},
    },
)
async def update_cart_item(
    response: Response,
    product_id: int,
    quantity: int = Query(..., ge=1),
    variant_id: Optional[int] = None,
    owner: CartOwner = Depends(get_cart_owner),
    db: AsyncSession = Depends(get_async_db),
    redis: RedisService = Depends(),
):
    """Update the quantity of an item already in the caller's cart."""

    product = await _get_active_product(db, product_id)

    variant = None
    if variant_id is not None:
        variant = await _get_active_variant_for_product(db, product_id, variant_id)
    stock_quantity = variant.stock_quantity if variant else product.stock_quantity

    try:
        cart = redis.update_cart_quantity_atomic(
            owner,
            product_id=product_id,
            quantity=quantity,
            stock_quantity=stock_quantity,
            variant_id=variant.id if variant else None,
        )
    except ItemNotInCartError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Item not in cart"
        )
    except InsufficientStockError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not enough stock available"
        )

    cart = _cart_or_503(cart)
    _sync_guest_cart_cookie(response, owner, cart)
    return APIResponse.success_response(await _priced_cart(db, cart))

@router.delete(
    "/items/{product_id}",
    response_model=APIResponse[CartResponse],
    responses={
        404: {"description": "Item not in cart"},
        503: {"description": "Cart store unavailable, nothing was changed"},
    },
)
async def remove_from_cart(
    response: Response,
    product_id: int,
    variant_id: Optional[int] = None,
    owner: CartOwner = Depends(get_cart_owner),
    db: AsyncSession = Depends(get_async_db),
    redis: RedisService = Depends(),
):
    """Remove an item from the caller's cart. This is also how a line
    marked unavailable is cleared: the product does not need to exist.

    A guest's cart left without lines is no longer stored, and its cookie
    is cleared."""

    try:
        cart = redis.remove_from_cart_atomic(owner, product_id, variant_id=variant_id)
    except ItemNotInCartError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Item not in cart"
        )

    cart = _cart_or_503(cart)
    _sync_guest_cart_cookie(response, owner, cart)
    return APIResponse.success_response(await _priced_cart(db, cart))

@router.delete(
    "",
    response_model=APIResponse[CartResponse],
    responses={503: {"description": "Cart store unavailable, nothing was changed"}},
)
async def clear_cart(
    response: Response,
    owner: CartOwner = Depends(get_cart_owner),
    db: AsyncSession = Depends(get_async_db),
    redis: RedisService = Depends(),
):
    """Remove all items from the caller's cart. For a guest this also ends
    the guest cart: it is deleted and its cookie cleared."""

    cart = _cart_or_503(redis.clear_cart_atomic(owner))
    _sync_guest_cart_cookie(response, owner, cart)
    # Priced like every other cart response; with no lines left that is no
    # query at all.
    return APIResponse.success_response(await _priced_cart(db, cart))

@router.post("/validate", response_model=APIResponse[CartValidationResponse])
async def validate_cart(
    cart: Cart = Depends(get_cart),
    db: AsyncSession = Depends(get_async_db),
):
    """Check whether the caller's cart can be ordered right now. A guest
    can validate a cart, but has to log in to order it (`POST /orders`).

    `valid` is true when every line is available and in stock; `issues`
    lists what stands in the way. A price that differs from the one a line
    was added at is not an issue - the cart and the order both use the
    current price - and is reported in `price_changes` for the customer's
    information, without affecting `valid`.
    """

    products, variants = await _load_catalog(db, cart)

    issues = []
    for item in cart.items.values():
        product, variant, unavailable_reason = resolve_cart_line(item, products, variants)
        if unavailable_reason is not None:
            issues.append(CartValidationIssue(
                product_id=item.product_id,
                variant_id=item.variant_id,
                code=unavailable_reason,
                error=_UNAVAILABLE_MESSAGES[unavailable_reason],
            ))
            continue

        available = variant.stock_quantity if variant else product.stock_quantity
        if available < item.quantity:
            issues.append(CartValidationIssue(
                product_id=item.product_id,
                variant_id=item.variant_id,
                code=CART_LINE_INSUFFICIENT_STOCK,
                error="Not enough stock",
                available=available,
                requested=item.quantity,
            ))

    # Taken from the same pricing every cart response goes through, so the
    # two cannot disagree about which lines changed price or to what.
    price_changes = [
        CartPriceChange(
            product_id=line.product_id,
            variant_id=line.variant_id,
            price_when_added=line.price_when_added,
            current_price=line.unit_price,
        )
        for line in CartResponse.from_cart(cart, products, variants).items
        if line.price_changed
    ]

    return APIResponse.success_response(CartValidationResponse(
        valid=len(issues) == 0,
        issues=issues,
        price_changes=price_changes,
    ))
