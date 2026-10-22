"""
Order state rules shared by the HTTP routes (app/api/v1/orders.py) and the
Celery worker (app/tasks.py): which status transitions are allowed, and how
an order is cancelled with its stock returned.

Kept out of the router module so the worker can use the same code without
importing FastAPI routes, and so there is exactly one restock path: admin
cancellation, a customer cancelling their own unpaid order, and the expiry
of unpaid orders all go through cancel_order_and_restock.
"""

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

# Order.payment_status values of an order nobody has paid for: what a
# customer may cancel themselves and what expires.
UNPAID_PAYMENT_STATUSES = frozenset({PaymentStatus.PENDING, PaymentStatus.FAILED})


def is_valid_status_transition(old_status: OrderStatus, new_status: OrderStatus) -> bool:
    return new_status in ORDER_STATUS_TRANSITIONS.get(old_status, set())


def is_unpaid_and_new(order: Order) -> bool:
    """Placed and not paid for: the only orders a customer may cancel
    themselves, and the only ones that expire."""
    return order.status == OrderStatus.NEW and order.payment_status in UNPAID_PAYMENT_STATUSES


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
    # objects: concurrent checkouts decrement the same rows.
    for item in order.items:
        if item.variant_id is not None:
            db.query(ProductVariant).filter(ProductVariant.id == item.variant_id).update(
                {ProductVariant.stock_quantity: ProductVariant.stock_quantity + item.quantity},
                synchronize_session=False,
            )
        else:
            db.query(Product).filter(Product.id == item.product_id).update(
                {Product.stock_quantity: Product.stock_quantity + item.quantity},
                synchronize_session=False,
            )
    return True
