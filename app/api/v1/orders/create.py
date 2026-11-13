import logging
from decimal import Decimal
from typing import Annotated

from fastapi import (
    BackgroundTasks,
    Body,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from redis.exceptions import RedisError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.api.v1.orders._create import (
    _created_order,
    _drop_product_details,
    _load_order_for_response,
    _order_by_idempotency_key,
)
from app.api.v1.orders._router import router
from app.core.config import settings
from app.core.currencies import MINIMUM_CHARGE
from app.core.dependencies import Cache, CurrentUser, DbSession
from app.core.errors import Problem, ProblemType
from app.core.idempotency import (
    ensure_same_request,
    order_key_header_description,
    request_fingerprint,
)
from app.core.openapi import LOCATION_HEADER
from app.models.address import Address
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.product import Product, ProductVariant
from app.models.user import User
from app.schemas.order import (
    ORDER_TOTAL_MAX,
    OrderCreate,
    OrderResponse,
    OrderStatus,
    PaymentStatus,
)
from app.services.email import send_order_confirmation_email
from app.services.orders import (
    UNPAID_PAYMENT_STATUSES,
    lock_stock_rows,
    product_has_active_variants,
    products_with_cached_stock,
)

# The name the module had before the orders package split: log filters and
# alerts keep matching.
logger = logging.getLogger("app.api.v1.orders")


@router.post(
    "",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {
            "description": "Order created; Location is its URL. A replay of the same "
                           "Idempotency-Key answers the same 201 and Location.",
            "headers": LOCATION_HEADER,
        },
        400: {"description": "Cart is empty, profile incomplete (full name and phone are required), "
                             "or the Idempotency-Key header is missing or malformed"},
        403: {"description": "Email verification required before placing orders"},
        404: {"description": "Shipping address not found (it must be an active address of the current user)"},
        409: {"description": "A cart item is out of stock (insufficient-stock) or no longer on sale, a product "
                             "with variants was added without a variant, or the user already holds "
                             "MAX_PENDING_ORDERS_PER_USER (10 by default) unpaid orders: pay for or cancel "
                             "one first"},
        422: {"description": "The key was already used with a different shipping_address_id, "
                             "or the order total is above 21,474,836.47 or below the payment "
                             "provider's minimum charge for the store currency (MINIMUM_CHARGE: "
                             "0.50 USD, 0.50 EUR, 0.30 GBP and others)"},
        503: {"description": "The cart store is unavailable (Retry-After: 5), or the server is at capacity"},
    },
)
async def create_order(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    db: DbSession,
    redis: Cache,
    # Required, not optional. Placing an order is the one request in this API
    # that must never be applied twice, and the client is the only party that
    # knows whether two requests are one attempt retried or two purchases:
    # a timeout, a double click and a second browser tab all look the same
    # from here. While the header was optional, a request without it that was
    # retried after its response was lost created a second order from
    # whatever was still in the cart. Generate one key (a UUID) per checkout
    # attempt and resend the same key on every retry of that attempt.
    idempotency_key: Annotated[str, Header(
        alias="Idempotency-Key", min_length=8, max_length=255,
        description=order_key_header_description(),
    )],
    body: Annotated[OrderCreate, Body()],
):
    """Create an order from the current user's cart, decrementing stock atomically.

    Requires an `Idempotency-Key` header. Repeating a request with a key
    already used by this user returns the order created the first time;
    a key already used with a different `shipping_address_id` is answered 422.
    The answer to a replay is the first one, 201 and Location included.

    Rules: the account needs a verified email, a full name and a phone
    number. The shipping address must be an active address of the user. The
    order is built from the whole cart and takes its stock at once; the cart
    lines it was built from are removed. The total must not exceed
    21,474,836.47 and must reach the payment provider's minimum charge for
    the store currency (0.50 for USD, 0.50 EUR, 0.30 GBP and others). A user
    can hold at most MAX_PENDING_ORDERS_PER_USER (10 by default) unpaid
    orders. An unpaid order is cancelled, and its stock returned,
    UNPAID_ORDER_EXPIRE_MINUTES (30 by default) after it was placed. This
    route is limited per user by RATE_LIMIT_CHECKOUT (10/minute by default).
    """

    shipping_address_id = body.shipping_address_id

    if not current_user.is_email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email verification required before placing orders"
        )

    if not current_user.full_name or not current_user.phone:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Complete profile information (full name and phone) required for placing orders"
        )

    # A retried checkout request (e.g. client timeout + retry, double-click)
    # with the same key returns the order already created instead of
    # double-charging stock/placing a duplicate order.
    # What defines this request is the address: the cart is server state, and
    # a replay returns the order the first request built from it.
    fingerprint = request_fingerprint(
        operation="create_order", shipping_address_id=shipping_address_id
    )
    existing = await _order_by_idempotency_key(db, current_user.id, idempotency_key)
    if existing:
        ensure_same_request(existing.idempotency_fingerprint, fingerprint)
        return _created_order(request, response, existing)

    # One order placement per user at a time, until this transaction ends:
    # requests with different keys used to read the same cart and the same
    # count of unpaid orders, and each placed an order (eight orders from one
    # cart, past the cap). A short window stays between the first request's
    # commit and its emptying of the cart in Redis. FOR NO KEY UPDATE: the
    # foreign-key checks of rows referencing the user (FOR KEY SHARE) are
    # not held up.
    await db.execute(
        select(User.id).where(User.id == current_user.id).with_for_update(key_share=True)
    )
    try:
        cart = await redis.get_cart(current_user.id)
    except RedisError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cart is temporarily unavailable, please retry",
            headers={"Retry-After": "5"},
        ) from e
    if not cart or not cart.items:
        # A retry that overtook its first request: the key was not found
        # above because the first one had not committed yet, and it has since
        # committed and emptied the cart. The answer is its order, not 400.
        existing = await _order_by_idempotency_key(db, current_user.id, idempotency_key)
        if existing:
            ensure_same_request(existing.idempotency_fingerprint, fingerprint)
            return _created_order(request, response, existing)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cart is empty"
        )

    # Unpaid orders hold stock until paid, cancelled or expired; one account
    # cannot hold more than the cap at once.
    pending_orders = (await db.scalar(
        select(func.count()).select_from(Order).where(
            Order.user_id == current_user.id,
            Order.status == OrderStatus.NEW,
            Order.payment_status.in_([s.value for s in UNPAID_PAYMENT_STATUSES]),
        )
    )) or 0
    if pending_orders >= settings.MAX_PENDING_ORDERS_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Too many unpaid orders ({settings.MAX_PENDING_ORDERS_PER_USER} allowed): "
                "pay for or cancel one before placing another"
            ),
        )

    # Queried through this route's session, not read off
    # current_user.addresses: current_user is a detached snapshot from the
    # auth dependency (app/core/security.py) and cannot lazy-load a
    # relationship. Only an active address: deleting one only deactivates
    # it (kept for the orders already shipped there), and a deleted
    # address used to be accepted here like any other.
    address = (await db.execute(
        select(Address).where(
            Address.id == shipping_address_id,
            Address.user_id == current_user.id,
            Address.is_active,
        ).limit(1)
    )).scalars().first()
    if not address:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shipping address not found"
        )


    order = Order(
        user_id=current_user.id,
        status=OrderStatus.NEW,
        payment_status=PaymentStatus.PENDING,
        shipping_address_id=shipping_address_id,
        ship_to_street=address.street,
        ship_to_city=address.city,
        ship_to_state=address.state,
        ship_to_postal_code=address.postal_code,
        ship_to_country=address.country,
        ship_to_address_type=address.address_type,
        ship_to_delivery_phone=address.delivery_phone,
        ship_to_delivery_instructions=address.delivery_instructions,
        idempotency_key=idempotency_key,
        idempotency_fingerprint=fingerprint,
        total_amount=Decimal('0.00'),
        currency=settings.STORE_CURRENCY,
    )
    db.add(order)
    # Flush (not commit) to populate order.id from the DB's autoincrement
    # before it's used as OrderItem.order_id below - without this, order.id
    # is still None at this point (session autoflush is off) and every
    # OrderItem insert fails its NOT NULL constraint. Pre-existing bug,
    # never caught because no prior test exercised order creation
    # end-to-end; surfaced while adding payment tests that need real orders.
    #
    # This INSERT is also where two concurrent requests with the same key
    # meet: both passed the pre-check above, and the unique index
    # (uix_order_user_idempotency_key) fails the second one here, not at
    # the commit below. Only the commit used to be guarded, so the loser of
    # that race answered 500 instead of the order the winner created.
    try:
        await db.flush()
    except IntegrityError:
        # The rollback expires every loaded object (expire_on_commit does not
        # cover it), and an expired attribute cannot be read back on an
        # AsyncSession: only current_user (detached, so not expired) and the
        # header value are used below.
        await db.rollback()
        existing = await _order_by_idempotency_key(db, current_user.id, idempotency_key)
        if existing:
            ensure_same_request(existing.idempotency_fingerprint, fingerprint)
            return _created_order(request, response, existing)
        # Only the key's unique index can fail a bare order INSERT (its
        # foreign keys were just read and nothing deletes orders' users or
        # addresses mid-request); re-raised rather than mistaken for a
        # replay if that ever changes.
        raise  # pragma: no cover

    # Every stock row the lines below change is locked up front, in one
    # order, so that checkouts and cancellations touching the same products
    # in different orders cannot deadlock. The loop stays in cart order: the
    # line reported as short, and the totals, are the same as before.
    await db.run_sync(
        lock_stock_rows, [(item.product_id, item.variant_id) for item in cart.items.values()]
    )

    total_amount = Decimal('0.00')
    for item in cart.items.values():
        product = (await db.execute(
            select(Product).where(Product.id == item.product_id).limit(1)
        )).scalars().first()
        # is_active is re-checked here, for product and variant alike, and
        # not only when the line is added: either can be withdrawn from
        # sale while it sits in a cart, and used to be ordered regardless.
        if not product or not product.is_active:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Product {item.product_id} not available in requested quantity"
            )

        variant = None
        if item.variant_id is not None:
            variant = (await db.execute(
                select(ProductVariant).where(
                    ProductVariant.id == item.variant_id,
                    ProductVariant.product_id == item.product_id,
                ).limit(1)
            )).scalars().first()
            if not variant or not variant.is_active:
                await db.rollback()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Product {item.product_id} not available in requested quantity"
                )
        elif await db.scalar(product_has_active_variants(item.product_id)):
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Product {item.product_id} requires a variant"
            )

        price_at_time = (
            variant.price_override
            if variant and variant.price_override is not None
            else product.price
        )

        # Atomic conditional decrement instead of read-then-write: under
        # concurrent checkouts, two requests reading stock_quantity=1 and
        # both deciding "enough stock" would oversell. The WHERE clause
        # makes the check-and-decrement a single database operation, so
        # only one of two concurrent requests for the last unit can win.
        # A variant line decrements the variant's own stock_quantity, not
        # the parent Product's - the two are independent counters.
        if variant is not None:
            stock_update = await db.execute(
                update(ProductVariant).where(
                    ProductVariant.id == variant.id,
                    ProductVariant.stock_quantity >= item.quantity,
                ).values(
                    stock_quantity=ProductVariant.stock_quantity - item.quantity
                ).execution_options(synchronize_session=False)
            )
        else:
            stock_update = await db.execute(
                update(Product).where(
                    Product.id == item.product_id,
                    Product.stock_quantity >= item.quantity,
                ).values(
                    stock_quantity=Product.stock_quantity - item.quantity
                ).execution_options(synchronize_session=False)
            )
        if stock_update.rowcount == 0:  # type: ignore[attr-defined]  # execute() on UPDATE returns CursorResult at runtime
            await db.rollback()
            raise Problem(
                status.HTTP_409_CONFLICT,
                type=ProblemType.INSUFFICIENT_STOCK,
                detail=f"Product {item.product_id} not available in requested quantity",
            )

        order_item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            variant_id=variant.id if variant else None,
            quantity=item.quantity,
            price_at_time=price_at_time,
            product_name_snapshot=product.name,
            variant_sku_snapshot=variant.sku if variant else None,
            variant_attributes_snapshot=variant.attributes if variant else None,
        )
        db.add(order_item)

        total_amount += price_at_time * item.quantity

    if total_amount > ORDER_TOTAL_MAX:  # Order.total_amount is Numeric(10, 2)
        await db.rollback()
        raise Problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            type=ProblemType.VALIDATION_ERROR,
            detail="Order total exceeds the maximum amount",
        )
    # An order Stripe will not charge used to be placed anyway: it held its
    # stock until it expired, and every /pay was refused.
    minimum = MINIMUM_CHARGE.get(order.currency)
    if minimum is not None and total_amount < minimum:
        await db.rollback()
        raise Problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            type=ProblemType.VALIDATION_ERROR,
            detail=f"Order total is below the minimum charge of {minimum} {order.currency}",
        )
    order.total_amount = total_amount
    await db.commit()
    await _drop_product_details(redis, products_with_cached_stock(
        [(item.product_id, item.variant_id) for item in cart.items.values()]
    ))

    # Read back with get_order's eager options: OrderResponse reads the
    # address, the lines, their products and variants (and the columns the
    # INSERT left to the database), none of which can be lazy-loaded on an
    # AsyncSession. populate_existing refreshes the instance the session
    # already holds.
    order = await _load_order_for_response(db, order.id)

    # Take out of the cart only what this order was built from, not the
    # whole key: delete_cart() here also discarded anything added between
    # the read at the top and this point, without it having been ordered.
    # The order is already committed, so a Redis failure cannot fail the
    # request - but the ordered lines are then still in the cart. A retry of
    # this request carries the same Idempotency-Key and gets this order
    # back; a deliberate new checkout would order them again. Logged with
    # the order id so that case can be found.
    if not await redis.remove_ordered_items_atomic(current_user.id, cart):
        logger.error(
            "Order %s committed but its items could not be removed from user %s's cart",
            order.id, current_user.id,
        )


    background_tasks.add_task(
        send_order_confirmation_email,
        current_user.email,
        OrderResponse.model_validate(order)
    )

    return _created_order(request, response, order)
