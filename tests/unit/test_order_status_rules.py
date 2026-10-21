"""
app/services/orders.py checked exhaustively, below the HTTP routes: the
status transition table for every (from, to) pair, which orders count as
"new and unpaid" for every (status, payment status) pair, and
cancel_order_and_restock from every status - stock returned exactly once,
to the counter it was taken from (the variant's for a variant line, the
product's otherwise), and never for an order that cannot be cancelled.

The route-level walk through the same table (PUT /orders/{id}/status) is
tests/integration/test_order_status_machine.py.
"""
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import product as pairs

import pytest
from freezegun import freeze_time

from app.models.address import Address
from app.models.category import Category
from app.models.enums import PaymentStatus as StoredPaymentStatus
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.product import Product, ProductVariant
from app.models.user import User
from app.schemas.order import OrderStatus, PaymentStatus
from app.services.orders import (
    ORDER_STATUS_TRANSITIONS,
    UNPAID_PAYMENT_STATUSES,
    cancel_order_and_restock,
    is_unpaid_and_new,
    is_valid_status_transition,
)

N, C, P, S, D, X = (OrderStatus.NEW, OrderStatus.CONFIRMED, OrderStatus.PROCESSING,
                    OrderStatus.SENT, OrderStatus.DELIVERED, OrderStatus.CANCELLED)
# Forward one step at a time, or cancelled from anything not yet delivered.
ALLOWED = {(N, C), (C, P), (P, S), (S, D), (N, X), (C, X), (P, X), (S, X)}
ALL_PAIRS = list(pairs(OrderStatus, repeat=2))
CANCELLABLE = {N, C, P, S}
TERMINAL = {D, X}


def _pair_id(pair):
    return f"{pair[0].value}->{pair[1].value}"


# -- the transition table ---------------------------------------------------------

@pytest.mark.parametrize("old,new", ALL_PAIRS, ids=[_pair_id(p) for p in ALL_PAIRS])
def test_every_pair_of_statuses(old, new):
    expected = (old, new) in ALLOWED
    assert is_valid_status_transition(old, new) is expected
    # Order.status is a plain String column: a loaded order carries "new",
    # not OrderStatus.NEW. The answer must be the same.
    assert is_valid_status_transition(old.value, new.value) is expected
    assert is_valid_status_transition(old.value, new) is expected


def test_the_table_has_one_row_per_status():
    assert set(ORDER_STATUS_TRANSITIONS) == set(OrderStatus)
    assert {(old, new) for old, news in ORDER_STATUS_TRANSITIONS.items()
            for new in news} == ALLOWED


def test_terminal_statuses_lead_nowhere_and_nothing_loops():
    for status in TERMINAL:
        assert not ORDER_STATUS_TRANSITIONS[status]
    for status in OrderStatus:
        assert status not in ORDER_STATUS_TRANSITIONS[status]


def test_every_status_is_reachable_from_new_and_there_is_no_cycle():
    reached, frontier = {N}, [N]
    while frontier:
        for nxt in ORDER_STATUS_TRANSITIONS[frontier.pop()]:
            if nxt not in reached:
                reached.add(nxt)
                frontier.append(nxt)
    assert reached == set(OrderStatus)

    def depth(status, seen=()):
        assert status not in seen, f"cycle through {status}"
        return 1 + max((depth(n, seen + (status,)) for n in ORDER_STATUS_TRANSITIONS[status]),
                       default=0)

    assert depth(N) == 5  # new, confirmed, processing, sent, delivered


@pytest.mark.parametrize("old,new", [("shipped", "cancelled"), ("new", "shipped"),
                                     ("", "new"), (None, "cancelled")])
def test_an_unknown_status_goes_nowhere(old, new):
    assert is_valid_status_transition(old, new) is False


# -- new and unpaid ---------------------------------------------------------------------

@pytest.mark.parametrize("status", list(OrderStatus), ids=lambda s: s.value)
@pytest.mark.parametrize("payment_status", list(StoredPaymentStatus), ids=lambda s: s.value)
def test_new_and_unpaid_for_every_status_pair(status, payment_status):
    expected = status == N and payment_status.value in ("pending", "failed")
    # As loaded from the database (plain strings) and as enum members.
    assert is_unpaid_and_new(Order(status=status.value,
                                   payment_status=payment_status.value)) is expected
    assert is_unpaid_and_new(Order(status=status, payment_status=payment_status)) is expected


def test_the_unpaid_statuses_are_pending_and_failed_only():
    assert UNPAID_PAYMENT_STATUSES == {PaymentStatus.PENDING, PaymentStatus.FAILED}


# -- cancel and restock -------------------------------------------------------------------

PRODUCT_STOCK, VARIANT_STOCK, OTHER_STOCK = 7, 4, 9
PRODUCT_LINE, VARIANT_LINE = 3, 2


def _shop(db):
    """A customer, two products (one with a variant), and nothing ordered yet."""
    user = User(email=f"buyer-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x",
                full_name="Buyer", role="client", is_active=True, is_email_verified=True,
                gdpr_consent=True, privacy_policy_accepted=True)
    category = Category(name=f"c-{uuid.uuid4().hex[:6]}", path=[], level=0)
    db.add_all([user, category])
    db.commit()
    address = Address(user_id=user.id, street="1 Main St", city="City", state="ST",
                      postal_code="10001", country="US")
    product = Product(name="Widget", description="w", price=Decimal("2.50"),
                      stock_quantity=PRODUCT_STOCK, category_id=category.id)
    other = Product(name="Gadget", description="g", price=Decimal("1.00"),
                    stock_quantity=OTHER_STOCK, category_id=category.id)
    db.add_all([address, product, other])
    db.commit()
    variant = ProductVariant(product_id=product.id, sku=f"W-{uuid.uuid4().hex[:6]}",
                             stock_quantity=VARIANT_STOCK)
    db.add(variant)
    db.commit()
    return user, address, product, variant, other


def _order(db, status, payment_status="pending"):
    """An order in `status` for 3 of the product and 2 of its variant, both
    already taken from stock (PRODUCT_STOCK and VARIANT_STOCK are what is left)."""
    user, address, product, variant, other = _shop(db)
    order = Order(user_id=user.id, status=status.value, payment_status=payment_status,
                  shipping_address_id=address.id, total_amount=Decimal("12.50"))
    db.add(order)
    db.commit()
    db.add_all([
        OrderItem(order_id=order.id, product_id=product.id, quantity=PRODUCT_LINE,
                  price_at_time=Decimal("2.50")),
        OrderItem(order_id=order.id, product_id=product.id, variant_id=variant.id,
                  quantity=VARIANT_LINE, price_at_time=Decimal("2.50")),
    ])
    db.commit()
    return order, product, variant, other


def _stock(db, *rows):
    db.expire_all()
    return tuple(db.get(type(row), row.id).stock_quantity for row in rows)


@pytest.mark.parametrize("status", list(OrderStatus), ids=lambda s: s.value)
def test_cancel_and_restock_from_every_status(db_session, status):
    order, product, variant, other = _order(db_session, status)
    placed_at = order.updated_at

    with freeze_time(placed_at + timedelta(minutes=5)):
        cancelled = cancel_order_and_restock(db_session, order)
        db_session.commit()

    db_session.expire_all()
    order = db_session.get(Order, order.id)
    if status in CANCELLABLE:
        assert cancelled is True
        assert order.status == X
        assert order.updated_at == placed_at + timedelta(minutes=5)
        assert _stock(db_session, product, variant, other) == (
            PRODUCT_STOCK + PRODUCT_LINE, VARIANT_STOCK + VARIANT_LINE, OTHER_STOCK
        )
    else:
        assert cancelled is False
        assert order.status == status
        assert order.updated_at == placed_at
        assert _stock(db_session, product, variant, other) == (
            PRODUCT_STOCK, VARIANT_STOCK, OTHER_STOCK
        )


@pytest.mark.parametrize("status", sorted(CANCELLABLE, key=list(OrderStatus).index),
                         ids=lambda s: s.value)
def test_stock_comes_back_once_however_often_the_order_is_cancelled(db_session, status):
    order, product, variant, other = _order(db_session, status)

    outcomes = []
    for _ in range(3):
        outcomes.append(cancel_order_and_restock(db_session, order))
        db_session.commit()

    assert outcomes == [True, False, False]
    assert _stock(db_session, product, variant, other) == (
        PRODUCT_STOCK + PRODUCT_LINE, VARIANT_STOCK + VARIANT_LINE, OTHER_STOCK
    )


@pytest.mark.parametrize("payment_status", list(StoredPaymentStatus), ids=lambda s: s.value)
def test_the_payment_status_does_not_decide_whether_stock_comes_back(db_session, payment_status):
    # cancel_order_and_restock looks at the order status only: whether a
    # paid order may be cancelled at all is the caller's rule (a customer
    # may cancel only a new unpaid one; an admin any cancellable one).
    order, product, variant, _ = _order(db_session, N, payment_status.value)

    assert cancel_order_and_restock(db_session, order) is True
    db_session.commit()

    assert _stock(db_session, product, variant) == (
        PRODUCT_STOCK + PRODUCT_LINE, VARIANT_STOCK + VARIANT_LINE
    )
    assert db_session.get(Order, order.id).payment_status == payment_status.value


def test_an_order_without_lines_is_cancelled_with_nothing_to_return(db_session):
    user, address, product, variant, other = _shop(db_session)
    order = Order(user_id=user.id, status="new", payment_status="pending",
                  shipping_address_id=address.id, total_amount=Decimal("0"),
                  created_at=datetime(2026, 1, 1))
    db_session.add(order)
    db_session.commit()

    assert cancel_order_and_restock(db_session, order) is True
    db_session.commit()
    assert _stock(db_session, product, variant, other) == (
        PRODUCT_STOCK, VARIANT_STOCK, OTHER_STOCK
    )
