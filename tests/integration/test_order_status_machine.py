"""
The order status state machine, every pair of statuses, through the admin
endpoint (PUT /orders/{id}/status): an allowed transition changes the
status (and, into cancelled, returns the stock exactly once); a forbidden
one is 409 and changes nothing.

new -> confirmed -> processing -> sent -> delivered, and any of the first
four -> cancelled. delivered and cancelled are terminal; nothing moves
backwards and no status "transitions" to itself.

Payment gates the way forward: the only payment methods are card payments
(stripe), so an order is confirmed by its payment, and an admin moves an
order into confirmed, processing, sent or delivered only while it is paid.
An unpaid or refunded order can still be cancelled.
"""
import uuid
from decimal import Decimal
from itertools import product as pairs

import pytest

from app.core.security import create_access_token, get_password_hash
from app.models.address import Address
from app.models.category import Category
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.product import Product
from app.models.user import User
from app.schemas.order import OrderStatus
from app.services.orders import ORDER_STATUS_TRANSITIONS, is_valid_status_transition

ORDERS = "/api/v1/orders"

ALLOWED = {
    ("new", "confirmed"), ("new", "cancelled"),
    ("confirmed", "processing"), ("confirmed", "cancelled"),
    ("processing", "sent"), ("processing", "cancelled"),
    ("sent", "delivered"), ("sent", "cancelled"),
}
ALL_PAIRS = list(pairs([s.value for s in OrderStatus], repeat=2))
FORWARD = ("confirmed", "processing", "sent", "delivered")
UNPAID_DETAIL = "Order is not paid: it cannot be confirmed, processed, sent or delivered"


@pytest.fixture(autouse=True)
def _no_email(monkeypatch):
    monkeypatch.setattr("app.api.v1.orders.send_order_status_update_email", lambda *a, **k: True)


def _user(db, role):
    user = User(email=f"{role}-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password=get_password_hash("Str0ngPass1"), full_name="Someone",
                role=role, is_active=True, is_email_verified=True,
                gdpr_consent=True, privacy_policy_accepted=True)
    db.add(user)
    db.commit()
    return user


def _order_in(db, status, quantity=3, stock_left=7, payment_status="paid"):
    """An order for `quantity` units, already taken from a product that has
    `stock_left` left, put directly into `status` and `payment_status`."""
    owner = _user(db, "client")
    address = Address(user_id=owner.id, street="1 Main St", city="City", state="ST",
                      postal_code="10001", country="US")
    category = Category(name=f"c-{uuid.uuid4().hex[:6]}", path=[], level=0)
    db.add_all([address, category])
    db.commit()
    item = Product(name="Widget", description="A widget", price=Decimal("2.50"),
                   stock_quantity=stock_left, category_id=category.id)
    db.add(item)
    db.commit()
    order = Order(user_id=owner.id, status=status, payment_status=payment_status,
                  shipping_address_id=address.id, total_amount=Decimal("2.50") * quantity)
    db.add(order)
    db.commit()
    db.add(OrderItem(order_id=order.id, product_id=item.id, quantity=quantity,
                     price_at_time=Decimal("2.50")))
    db.commit()
    return order.id, item.id


def test_transition_table_is_exactly_the_documented_one():
    table = {(old.value, new.value) for old, news in ORDER_STATUS_TRANSITIONS.items()
             for new in news}
    assert table == ALLOWED
    assert set(ORDER_STATUS_TRANSITIONS) == set(OrderStatus)


@pytest.mark.parametrize("old,new", ALL_PAIRS, ids=[f"{o}->{n}" for o, n in ALL_PAIRS])
def test_is_valid_status_transition_for_every_pair(old, new):
    assert is_valid_status_transition(OrderStatus(old), OrderStatus(new)) is ((old, new) in ALLOWED)


@pytest.mark.parametrize("old,new", ALL_PAIRS, ids=[f"{o}->{n}" for o, n in ALL_PAIRS])
def test_admin_status_change_for_every_pair(client, db_session, old, new):
    order_id, product_id = _order_in(db_session, old, quantity=3, stock_left=7)
    admin = _user(db_session, "admin")
    headers = {"Authorization": f"Bearer {create_access_token(admin.id)}"}

    response = client.put(f"{ORDERS}/{order_id}/status", json={"status": new}, headers=headers)

    db_session.expire_all()
    order = db_session.get(Order, order_id)
    stock = db_session.get(Product, product_id).stock_quantity
    if (old, new) in ALLOWED:
        assert response.status_code == 200, response.text
        assert response.json()["status"] == new
        assert order.status == new
        # Into cancelled returns the order's 3 units; nothing else touches stock.
        assert stock == (10 if new == "cancelled" else 7)
    else:
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "Invalid status transition"
        assert order.status == old
        assert stock == 7


UNPAID = ("pending", "failed", "refunded")


@pytest.mark.parametrize("payment_status", UNPAID)
@pytest.mark.parametrize("old,new", [pair for pair in sorted(ALLOWED) if pair[1] in FORWARD],
                         ids=lambda v: v)
def test_an_unpaid_order_is_not_moved_forward(client, db_session, old, new, payment_status):
    order_id, product_id = _order_in(db_session, old, payment_status=payment_status)
    admin = _user(db_session, "admin")
    headers = {"Authorization": f"Bearer {create_access_token(admin.id)}"}

    response = client.put(f"{ORDERS}/{order_id}/status", json={"status": new}, headers=headers)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == UNPAID_DETAIL
    db_session.expire_all()
    assert db_session.get(Order, order_id).status == old
    assert db_session.get(Product, product_id).stock_quantity == 7


@pytest.mark.parametrize("payment_status", UNPAID)
@pytest.mark.parametrize("old", ["new", "confirmed", "processing", "sent"])
def test_an_unpaid_order_can_still_be_cancelled(client, db_session, old, payment_status):
    order_id, product_id = _order_in(db_session, old, payment_status=payment_status)
    admin = _user(db_session, "admin")
    headers = {"Authorization": f"Bearer {create_access_token(admin.id)}"}

    response = client.put(f"{ORDERS}/{order_id}/status", json={"status": "cancelled"}, headers=headers)

    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert db_session.get(Order, order_id).status == "cancelled"
    assert db_session.get(Product, product_id).stock_quantity == 10


@pytest.mark.parametrize("old,new", [("new", "sent"), ("new", "delivered"), ("confirmed", "delivered")])
def test_a_forbidden_jump_of_an_unpaid_order_is_the_transition_error(client, db_session, old, new):
    # The transition table is checked before payment: the answer names
    # the first rule the request breaks.
    order_id, _ = _order_in(db_session, old, payment_status="pending")
    admin = _user(db_session, "admin")
    headers = {"Authorization": f"Bearer {create_access_token(admin.id)}"}

    response = client.put(f"{ORDERS}/{order_id}/status", json={"status": new}, headers=headers)

    assert response.status_code == 409
    assert response.json()["detail"] == "Invalid status transition"


def test_status_change_requires_an_admin(client, db_session):
    order_id, _ = _order_in(db_session, "new")
    customer = _user(db_session, "client")
    headers = {"Authorization": f"Bearer {create_access_token(customer.id)}"}

    response = client.put(f"{ORDERS}/{order_id}/status", json={"status": "cancelled"},
                          headers=headers)

    assert response.status_code == 403
    db_session.expire_all()
    assert db_session.get(Order, order_id).status == "new"


def test_unknown_status_is_a_validation_error(client, db_session):
    order_id, _ = _order_in(db_session, "new")
    admin = _user(db_session, "admin")
    headers = {"Authorization": f"Bearer {create_access_token(admin.id)}"}

    response = client.put(f"{ORDERS}/{order_id}/status", json={"status": "shipped"},
                          headers=headers)

    assert response.status_code == 422


def test_an_order_is_moved_on_once_its_payment_has_gone_through(client, db_session, monkeypatch):
    # End to end: placed, refused while unpaid, paid through /pay (which
    # confirms it), then moved on by the admin.
    from test_order_expiry import _pay, _place
    from test_orders import _login, _make_admin

    from app.services.payment.stripe_provider import StripePaymentProvider

    StripePaymentProvider.reset_mock_state()
    monkeypatch.setattr("app.api.v1.orders.send_order_confirmation_email", lambda *a, **k: True)
    token, order, _, _ = _place(client, db_session)
    admin = _make_admin(db_session)
    headers = {"Authorization": f"Bearer {_login(client, admin.email, 'AdminPass1')}"}

    refused = client.put(f"{ORDERS}/{order['id']}/status", json={"status": "confirmed"}, headers=headers)
    assert refused.status_code == 409
    assert refused.json()["detail"] == UNPAID_DETAIL

    assert _pay(client, token, order).status_code == 200
    for status in ("processing", "sent", "delivered"):
        moved = client.put(f"{ORDERS}/{order['id']}/status", json={"status": status}, headers=headers)
        assert moved.status_code == 200, (status, moved.text)
    StripePaymentProvider.reset_mock_state()
