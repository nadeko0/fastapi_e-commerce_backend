"""
Order state rules shared by the HTTP routes (app/api/v1/orders.py) and the
Celery worker (app/tasks.py): which status transitions are allowed, and how
an order is cancelled with its stock returned.

Kept out of the router module so the worker can use the same code without
importing FastAPI routes, and so there is exactly one restock path: admin
cancellation, a customer cancelling their own unpaid order, and the expiry
of unpaid orders all go through cancel_order_and_restock.
"""

import logging
from datetime import timedelta

from sqlalchemy import case, exists, select, update
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.core.config import settings
from app.models.order import Order
from app.models.payment import Payment
from app.models.product import Product, ProductVariant
from app.schemas.common import INT32_MAX
from app.schemas.order import OrderStatus, PaymentStatus
from app.services.payment.exceptions import (
    CardError,
    InvalidRequestError,
    PaymentProviderError,
)
from app.services.reconciliation import PENDING_INTENT_ID_PREFIX

logger = logging.getLogger(__name__)

ORDER_STATUS_TRANSITIONS: dict[str, set[OrderStatus]] = {
    OrderStatus.NEW: {OrderStatus.CONFIRMED, OrderStatus.CANCELLED},
    OrderStatus.CONFIRMED: {OrderStatus.PROCESSING, OrderStatus.CANCELLED},
    OrderStatus.PROCESSING: {OrderStatus.SENT, OrderStatus.CANCELLED},
    OrderStatus.SENT: {OrderStatus.DELIVERED, OrderStatus.CANCELLED},
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
}

# The statuses an order reaches only through payment (the payment code sets
# CONFIRMED) and that only a paid order is moved into by an admin: the only
# payment methods are card payments, there is no way to ship against an invoice
# or cash on delivery. Cancelling stays possible for an order in any payment
# state.
PAID_ONLY_STATUSES = frozenset({
    OrderStatus.CONFIRMED, OrderStatus.PROCESSING, OrderStatus.SENT, OrderStatus.DELIVERED,
})

# Order.payment_status values of an order nobody has paid for: what a
# customer may cancel themselves and what expires.
UNPAID_PAYMENT_STATUSES = frozenset({PaymentStatus.PENDING, PaymentStatus.FAILED})


def is_anonymised_email(email: str | None) -> bool:
    """True if email is an anonymised placeholder created by GDPR erasure."""
    if not email:
        return False
    return email.endswith("@deleted.invalid")


def product_has_active_variants(product_id: int):
    """A scalar select: true when the product has an active variant. Such a
    product is sold by its variants only (a line without variant_id is
    refused, in the cart and at checkout); the parent's own price and stock
    are for a product without variants."""
    return select(exists().where(ProductVariant.product_id == product_id, ProductVariant.is_active))


def products_with_cached_stock(lines) -> list[int]:
    """
    Ids of the products whose detail (GET /products/{id}, cached in Redis)
    shows a stock count that a change to `lines` (pairs of product id and
    variant id, or None) moves: the plain lines. A variant line changes the
    variant's own stock, which the product detail does not show. Callers drop
    these cache entries after the transaction that changed the stock commits.

    Stock moved by checkout does not change the product ETag on purpose:
    inventory changes invalidate Redis cache so the next GET shows current
    counts, but do not bump updated_at.
    """
    return sorted({product_id for product_id, variant_id in lines if variant_id is None})


def is_valid_status_transition(old_status: str, new_status: OrderStatus) -> bool:
    return new_status in ORDER_STATUS_TRANSITIONS.get(old_status, set())


def is_unpaid_and_new(order: Order) -> bool:
    """Placed and not paid for: the only orders a customer may cancel
    themselves, and the only ones that expire."""
    return order.status == OrderStatus.NEW and order.payment_status in UNPAID_PAYMENT_STATUSES


# A payment attempt whose outcome is not known yet: it may still charge.
LIVE_ATTEMPT_STATUSES = ("processing", "requires_action", "unknown")


def has_live_payment_attempt(db: Session, order: Order) -> bool:
    """True while one of the order's payment attempts can still be paid
    (an open Checkout Session, 3D Secure, a request whose outcome is
    unknown) and is younger than CHECKOUT_SESSION_EXPIRE_MINUTES, the
    lifetime given to the Checkout Session at Stripe. Cancelling such an
    order would let the customer pay for an order already restocked."""
    started_after = utc_now() - timedelta(minutes=settings.CHECKOUT_SESSION_EXPIRE_MINUTES)
    return db.scalars(
        select(Payment.id).where(
            Payment.order_id == order.id,
            Payment.status.in_(LIVE_ATTEMPT_STATUSES),
            Payment.created_at > started_after,
        ).limit(1)
    ).first() is not None


def open_payment_attempts(
    db: Session, order_id: int, except_payment_id: int | None = None
) -> list[tuple[str | None, str]]:
    """(checkout_session_id, payment_intent_id) of the order's attempts whose
    outcome is not known yet, for close_attempts_at_provider."""
    query = select(Payment.checkout_session_id, Payment.payment_intent_id).where(
        Payment.order_id == order_id,
        Payment.status.in_(LIVE_ATTEMPT_STATUSES),
    )
    if except_payment_id is not None:
        query = query.where(Payment.id != except_payment_id)
    return [(row[0], row[1]) for row in db.execute(query)]


def close_attempts_at_provider(provider, attempts) -> bool:
    """Make these attempts unpayable: a Checkout Session is expired, a
    PaymentIntent cancelled. False when the provider could not be asked. A
    refusal is an answer (the attempt was paid or closed meanwhile), and a
    charge that got through is flagged for manual review by its webhook."""
    closed = True
    for session_id, intent_id in attempts:
        try:
            if session_id:
                provider.expire_checkout_session(session_id)
            elif not intent_id.startswith(PENDING_INTENT_ID_PREFIX):
                provider.cancel_payment_intent(intent_id)
        except (CardError, InvalidRequestError) as e:
            logger.info("Attempt %s was not closed: %s", session_id or intent_id, e)
        except PaymentProviderError as e:
            logger.warning("Attempt %s could not be closed: %s", session_id or intent_id, e)
            closed = False
    return closed


def lock_stock_rows(db: Session, lines) -> None:
    """
    Take the row locks of every stock row the `lines` (pairs of product id
    and variant id, or None) are about to change, in one fixed order: the
    products by id, then the variants by id. Two transactions that change
    the same rows in different orders (carts {A, B} and {B, A}) would
    otherwise each hold one row and wait for the other's, and PostgreSQL
    aborts one of them with a deadlock (SQLSTATE 40P01). PostgreSQL docs,
    "Explicit Locking", Deadlocks: "the best defense against deadlocks is
    generally to avoid them by being certain that all applications using a
    database acquire locks on multiple objects in a consistent order."

    A variant line changes the variant's own stock only, a plain line the
    product's. Call it before the first stock UPDATE of the transaction;
    the UPDATEs themselves may then run in any order. SQLite ignores the
    lock, and with it the order.

    FOR NO KEY UPDATE (key_share=True), not FOR UPDATE: it still serialises
    the stock writers, but does not conflict with the FOR KEY SHARE that the
    order_items foreign-key check takes on a variant line's product at
    commit. With FOR UPDATE that implicit lock came after the variant locks
    (products -> variants -> products) and deadlocked a checkout of a
    variant against a cart or cancel holding the same product as a plain
    line. The stock UPDATEs never change a key column.
    """
    product_ids = sorted({product_id for product_id, variant_id in lines if variant_id is None})
    variant_ids = sorted({variant_id for _, variant_id in lines if variant_id is not None})
    db.execute(
        select(Product.id)
        .where(Product.id.in_(product_ids))
        .order_by(Product.id)
        .with_for_update(key_share=True)
    ).all()
    db.execute(
        select(ProductVariant.id)
        .where(ProductVariant.id.in_(variant_ids))
        .order_by(ProductVariant.id)
        .with_for_update(key_share=True)
    ).all()


def _restocked(column, quantity: int):
    """`column + quantity`, capped at the largest stock the API accepts
    (Int32Max). An admin may set a stock to 2147483647 while orders still
    hold units of it; adding them back unchecked overflowed the INTEGER
    column on PostgreSQL (NumericValueOutOfRange: the cancel answered 500 and
    the expiry sweep stopped) and left an unreadable value on SQLite. The
    comparison is written so that nothing overflows while it is computed."""
    return case((column > INT32_MAX - quantity, INT32_MAX), else_=column + quantity)


def cancel_order_and_restock(db: Session, order: Order) -> bool:
    """
    Move `order` to cancelled and put every line's quantity back into the
    stock create_order took it from: the variant's own stock_quantity for
    a variant line, the product's otherwise (the two are independent
    counters). Without this, every cancelled order would leak its quantity
    out of stock_quantity forever.

    Returns False, changing nothing, when the order cannot be cancelled
    from its current status - in particular when it already is cancelled,
    which is what keeps stock from being returned twice. That check is only
    sound if the caller has locked the order row (SELECT ... FOR UPDATE)
    before reading its status: two cancellations reading "new" at the same
    time would otherwise both restock. The caller commits.
    """
    if not is_valid_status_transition(order.status, OrderStatus.CANCELLED):
        return False

    order.status = OrderStatus.CANCELLED
    order.updated_at = utc_now()
    # Relative UPDATEs (stock = stock + n), not read-modify-write on the ORM
    # objects: concurrent checkouts decrement the same rows. The rows are
    # locked in the order create_order locks them first.
    lock_stock_rows(db, [(item.product_id, item.variant_id) for item in order.items])
    for item in order.items:
        if item.variant_id is not None:
            db.execute(
                update(ProductVariant)
                .where(ProductVariant.id == item.variant_id)
                .values({ProductVariant.stock_quantity: _restocked(ProductVariant.stock_quantity, item.quantity)})
                .execution_options(synchronize_session=False)
            )
        else:
            db.execute(
                update(Product)
                .where(Product.id == item.product_id)
                .values({Product.stock_quantity: _restocked(Product.stock_quantity, item.quantity)})
                .execution_options(synchronize_session=False)
            )
    return True
