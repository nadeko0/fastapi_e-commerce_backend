"""
The order status state machine, every pair of statuses, through the admin
endpoint (PUT /orders/{id}/status): an allowed transition changes the
status (and, into cancelled, returns the stock exactly once); a forbidden
one is 400 and changes nothing.

new -> confirmed -> processing -> sent -> delivered, and any of the first
four -> cancelled. delivered and cancelled are terminal; nothing moves
backwards and no status "transitions" to itself.
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


def _order_in(db, status, quantity=3, stock_left=7):
    """An order for `quantity` units, already taken from a product that has
    `stock_left` left, put directly into `status`."""
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
    order = Order(user_id=owner.id, status=status, payment_status="pending",
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

    response = client.put(f"{ORDERS}/{order_id}/status", params={"status": new}, headers=headers)

    db_session.expire_all()
    order = db_session.get(Order, order_id)
    stock = db_session.get(Product, product_id).stock_quantity
    if (old, new) in ALLOWED:
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == new
        assert order.status == new
        # Into cancelled returns the order's 3 units; nothing else touches stock.
        assert stock == (10 if new == "cancelled" else 7)
    else:
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "Invalid status transition"
        assert order.status == old
        assert stock == 7


def test_status_change_requires_an_admin(client, db_session):
    order_id, _ = _order_in(db_session, "new")
    customer = _user(db_session, "client")
    headers = {"Authorization": f"Bearer {create_access_token(customer.id)}"}

    response = client.put(f"{ORDERS}/{order_id}/status", params={"status": "cancelled"},
                          headers=headers)

    assert response.status_code == 403
    db_session.expire_all()
    assert db_session.get(Order, order_id).status == "new"


def test_unknown_status_is_a_validation_error(client, db_session):
    order_id, _ = _order_in(db_session, "new")
    admin = _user(db_session, "admin")
    headers = {"Authorization": f"Bearer {create_access_token(admin.id)}"}

    response = client.put(f"{ORDERS}/{order_id}/status", params={"status": "shipped"},
                          headers=headers)

    assert response.status_code == 422
