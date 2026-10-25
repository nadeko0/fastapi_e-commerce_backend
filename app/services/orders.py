"""
Order state rules shared by the HTTP routes (app/api/v1/orders.py) and the
Celery worker (app/tasks.py): which status transitions are allowed, and how
an order is cancelled with its stock returned.

Kept out of the router module so the worker can use the same code without
importing FastAPI routes, and so there is exactly one restock path: admin
cancellation, a customer cancelling their own unpaid order, and the expiry
of unpaid orders all go through cancel_order_and_restock.
"""

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.models.order import Order
from app.models.product import Product, ProductVariant
from app.schemas.order import OrderStatus, PaymentStatus

ORDER_STATUS_TRANSITIONS = {
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


def products_with_cached_stock(lines) -> list[int]:
    """
    Ids of the products whose detail (GET /products/{id}, cached in Redis)
    shows a stock count that a change to `lines` (pairs of product id and
    variant id, or None) moves: the plain lines. A variant line changes the
    variant's own stock, which the product detail does not show. Callers drop
    these cache entries after the transaction that changed the stock commits.
    """
    return sorted({product_id for product_id, variant_id in lines if variant_id is None})


def is_valid_status_transition(old_status: OrderStatus, new_status: OrderStatus) -> bool:
    return new_status in ORDER_STATUS_TRANSITIONS.get(old_status, set())


def is_unpaid_and_new(order: Order) -> bool:
    """Placed and not paid for: the only orders a customer may cancel
    themselves, and the only ones that expire."""
    return order.status == OrderStatus.NEW and order.payment_status in UNPAID_PAYMENT_STATUSES


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
    """
    product_ids = sorted({product_id for product_id, variant_id in lines if variant_id is None})
    variant_ids = sorted({variant_id for _, variant_id in lines if variant_id is not None})
    db.execute(
        select(Product.id).where(Product.id.in_(product_ids)).order_by(Product.id).with_for_update()
    ).all()
    db.execute(
        select(ProductVariant.id)
        .where(ProductVariant.id.in_(variant_ids))
        .order_by(ProductVariant.id)
        .with_for_update()
    ).all()


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
                .values({ProductVariant.stock_quantity: ProductVariant.stock_quantity + item.quantity})
                .execution_options(synchronize_session=False)
            )
        else:
            db.execute(
                update(Product)
                .where(Product.id == item.product_id)
                .values({Product.stock_quantity: Product.stock_quantity + item.quantity})
                .execution_options(synchronize_session=False)
            )
    return True
