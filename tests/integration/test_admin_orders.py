"""Admin order management: the cross-customer order list (filters, sorting,
pagination), the statistics endpoint, status transitions and refunds as
the admin performs them."""
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from freezegun import freeze_time
from hypothesis import HealthCheck, given
from hypothesis import settings as settings_hypothesis
from hypothesis import strategies as st
from test_payment import _with_idempotency_key

from app.core.clock import utc_now
from app.core.security import get_password_hash
from app.models.address import Address
from app.models.category import Category
from app.models.enums import UserRole
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.payment import Payment
from app.models.product import Product
from app.models.user import User

ADMIN = "/api/v1/admin"
ORDERS = "/api/v1/orders"
NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)


def _make_user(db_session, email, role=UserRole.CLIENT):
    user = User(
        email=email, hashed_password=get_password_hash("Str0ngPass1"), full_name="Someone",
        role=role, gdpr_consent=True, privacy_policy_accepted=True, consent_history=[],
        is_active=True, is_email_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _headers(client, email):
    login = client.post("/api/v1/users/login", data={"username": email, "password": "Str0ngPass1"})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _admin_headers(client, db_session):
    if not db_session.query(User).filter(User.email == "admin@example.com").first():
        _make_user(db_session, "admin@example.com", role=UserRole.ADMIN)
    return _headers(client, "admin@example.com")


def _customer(db_session, email="buyer@example.com"):
    user = _make_user(db_session, email)
    address = Address(
        user_id=user.id, street="1 Main St", city="Town", state="ST",
        postal_code="10001", country="US",
    )
    db_session.add(address)
    db_session.commit()
    return user, address


def _product(db_session, name="Widget", price="10.00"):
    category = db_session.query(Category).first()
    if category is None:
        category = Category(name="Shelf", level=0, path=[])
        db_session.add(category)
        db_session.commit()
    product = Product(
        name=name, description="A product used by the admin order tests",
        price=Decimal(price), stock_quantity=100, category_id=category.id, images=[],
    )
    db_session.add(product)
    db_session.commit()
    return product


def _order(
    db_session, customer, *, total="10.00", status="new", payment_status="pending",
    created_at=None, lines=(),
):
    user, address = customer
    order = Order(
        user_id=user.id, shipping_address_id=address.id, total_amount=Decimal(total),
        status=status, payment_status=payment_status,
        created_at=created_at or utc_now(),
    )
    db_session.add(order)
    db_session.commit()
    for product, quantity, price in lines:
        db_session.add(OrderItem(
            order_id=order.id, product_id=product.id, quantity=quantity,
            price_at_time=Decimal(price),
        ))
    db_session.commit()
    return order


def _ids(response):
    assert response.status_code == 200, response.text
    return [o["id"] for o in response.json()["items"]]


# --- Listing: filters --------------------------------------------------------


def test_admin_order_list_spans_every_customer(client, db_session):
    a = _order(db_session, _customer(db_session, "a@example.com"))
    b = _order(db_session, _customer(db_session, "b@example.com"))

    response = client.get(f"{ADMIN}/orders", headers=_admin_headers(client, db_session))

    assert sorted(_ids(response)) == sorted([a.id, b.id])


def test_admin_order_list_filters_by_status_case_insensitively(client, db_session):
    customer = _customer(db_session)
    confirmed = _order(db_session, customer, status="confirmed")
    _order(db_session, customer, status="new")
    _order(db_session, customer, status="cancelled")

    response = client.get(
        f"{ADMIN}/orders", params={"status": "CONFIRMED"},
        headers=_admin_headers(client, db_session),
    )

    assert _ids(response) == [confirmed.id]


def test_admin_order_list_filters_by_payment_status(client, db_session):
    customer = _customer(db_session)
    paid = _order(db_session, customer, payment_status="paid")
    _order(db_session, customer, payment_status="pending")
    refunded = _order(db_session, customer, payment_status="refunded")
    headers = _admin_headers(client, db_session)

    assert _ids(client.get(
        f"{ADMIN}/orders", params={"payment_status": "paid"}, headers=headers,
    )) == [paid.id]
    assert _ids(client.get(
        f"{ADMIN}/orders", params={"payment_status": "refunded"}, headers=headers,
    )) == [refunded.id]


def test_admin_order_list_combines_filters(client, db_session):
    customer = _customer(db_session)
    wanted = _order(db_session, customer, status="sent", payment_status="paid")
    _order(db_session, customer, status="sent", payment_status="pending")
    _order(db_session, customer, status="new", payment_status="paid")

    response = client.get(
        f"{ADMIN}/orders", params={"status": "sent", "payment_status": "paid"},
        headers=_admin_headers(client, db_session),
    )

    assert _ids(response) == [wanted.id]


@pytest.mark.parametrize(
    "params",
    [{"status": "shipped"}, {"payment_status": "free"}, {"payment_status": "requires_action"}],
)
def test_admin_order_list_rejects_unknown_filter_values(client, db_session, params):
    response = client.get(
        f"{ADMIN}/orders", params=params, headers=_admin_headers(client, db_session)
    )

    # requires_action is a PaymentStatus of a payment attempt, which the
    # orders table never stores: it filters to nothing rather than failing.
    if params.get("payment_status") == "requires_action":
        assert _ids(response) == []
    else:
        assert response.status_code == 422


def test_admin_order_list_date_range_is_inclusive_on_both_ends(client, db_session):
    customer = _customer(db_session)
    before = _order(db_session, customer, created_at=NOW - timedelta(days=2, seconds=1))
    first = _order(db_session, customer, created_at=NOW - timedelta(days=2))
    middle = _order(db_session, customer, created_at=NOW - timedelta(days=1))
    last = _order(db_session, customer, created_at=NOW)
    after = _order(db_session, customer, created_at=NOW + timedelta(seconds=1))

    response = client.get(
        f"{ADMIN}/orders",
        params={
            "start_date": (NOW - timedelta(days=2)).isoformat(),
            "end_date": NOW.isoformat(),
        },
        headers=_admin_headers(client, db_session),
    )

    ids = _ids(response)
    assert sorted(ids) == sorted([first.id, middle.id, last.id])
    assert before.id not in ids and after.id not in ids


def test_admin_order_list_date_range_converts_an_offset_to_utc(client, db_session):
    customer = _customer(db_session)
    kathmandu = timezone(timedelta(hours=5, minutes=45))
    inside = _order(db_session, customer, created_at=NOW)
    outside = _order(db_session, customer, created_at=NOW + timedelta(seconds=1))

    response = client.get(
        f"{ADMIN}/orders",
        params={
            "start_date": (NOW - timedelta(days=1)).astimezone(kathmandu).isoformat(),
            "end_date": NOW.astimezone(kathmandu).isoformat(),
        },
        headers=_admin_headers(client, db_session),
    )

    assert _ids(response) == [inside.id]
    assert outside.id not in _ids(response)


# --- Listing: sorting and pagination ----------------------------------------


def test_admin_order_list_defaults_to_newest_first(client, db_session):
    customer = _customer(db_session)
    old = _order(db_session, customer, created_at=NOW - timedelta(days=3))
    new = _order(db_session, customer, created_at=NOW)
    mid = _order(db_session, customer, created_at=NOW - timedelta(days=1))

    response = client.get(f"{ADMIN}/orders", headers=_admin_headers(client, db_session))

    assert _ids(response) == [new.id, mid.id, old.id]


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_admin_order_list_sorts_by_total_amount(client, db_session, direction):
    customer = _customer(db_session)
    orders = [_order(db_session, customer, total=t) for t in ("50.00", "5.00", "500.00")]
    by_total = sorted(orders, key=lambda o: o.total_amount, reverse=direction == "desc")

    response = client.get(
        f"{ADMIN}/orders", params={"sort_by": f"total_amount_{direction}"},
        headers=_admin_headers(client, db_session),
    )

    assert _ids(response) == [o.id for o in by_total]


def test_admin_order_list_sorts_by_created_at_ascending(client, db_session):
    customer = _customer(db_session)
    newer = _order(db_session, customer, created_at=NOW)
    older = _order(db_session, customer, created_at=NOW - timedelta(hours=1))

    response = client.get(
        f"{ADMIN}/orders", params={"sort_by": "created_at_asc"},
        headers=_admin_headers(client, db_session),
    )

    assert _ids(response) == [older.id, newer.id]


ORDER_COUNT = 7


@settings_hypothesis(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(page=st.integers(min_value=1, max_value=10), size=st.integers(min_value=1, max_value=10))
def test_admin_order_list_pages_partition_the_newest_first_list(client, db_session, page, size):
    """Any page/size: the page is exactly that slice of the full newest-first
    list, total counts every order, has_more says whether anything follows."""
    if db_session.query(Order).count() == 0:
        customer = _customer(db_session)
        for i in range(ORDER_COUNT):
            _order(db_session, customer, created_at=NOW - timedelta(hours=i))
    newest_first = [
        o.id for o in db_session.query(Order).order_by(Order.created_at.desc()).all()
    ]

    response = client.get(
        f"{ADMIN}/orders", params={"page": page, "size": size},
        headers=_admin_headers(client, db_session),
    )

    data = response.json()
    assert _ids(response) == newest_first[(page - 1) * size: page * size]
    assert data["total"] == ORDER_COUNT
    assert (data["page"], data["size"]) == (page, size)
    assert data["has_more"] is (page * size < ORDER_COUNT)


@pytest.mark.parametrize("params", [{"page": 0}, {"size": 0}, {"size": 101}])
def test_admin_order_list_rejects_out_of_range_pagination(client, db_session, params):
    response = client.get(
        f"{ADMIN}/orders", params=params, headers=_admin_headers(client, db_session)
    )

    assert response.status_code == 422


# --- Statistics --------------------------------------------------------------


def _stats(client, headers, period):
    response = client.get(f"{ADMIN}/stats", params={"period": period}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_admin_stats_count_only_paid_live_orders_inside_each_window(client, db_session):
    """Each window starts exactly `period` before now and includes that
    instant; cancelled or unpaid orders never count, whatever their age."""
    customer = _customer(db_session)
    paid = {"payment_status": "paid", "status": "confirmed"}
    with freeze_time(NOW):
        _order(db_session, customer, total="10.00", created_at=NOW - timedelta(hours=1), **paid)
        _order(db_session, customer, total="20.00", created_at=NOW - timedelta(hours=24), **paid)
        _order(db_session, customer, total="40.00",
               created_at=NOW - timedelta(hours=24, seconds=1), **paid)
        _order(db_session, customer, total="80.00", created_at=NOW - timedelta(days=7), **paid)
        _order(db_session, customer, total="160.00",
               created_at=NOW - timedelta(days=30), **paid)
        _order(db_session, customer, total="320.00",
               created_at=NOW - timedelta(days=30, seconds=1), **paid)
        # Never counted:
        _order(db_session, customer, total="1000.00", created_at=NOW,
               status="cancelled", payment_status="paid")
        _order(db_session, customer, total="2000.00", created_at=NOW,
               status="confirmed", payment_status="pending")
        _order(db_session, customer, total="4000.00", created_at=NOW,
               status="delivered", payment_status="refunded")
        headers = _admin_headers(client, db_session)

        day, week, month = (_stats(client, headers, p) for p in ("24h", "7d", "30d"))

    assert (day["order_count"], day["total_revenue"]) == (2, "30.00")
    assert day["average_order_value"] == "15.00"
    assert (week["order_count"], week["total_revenue"]) == (4, "150.00")
    assert (month["order_count"], month["total_revenue"]) == (5, "310.00")
    assert month["average_order_value"] == "62.00"
    assert [s["period"] for s in (day, week, month)] == ["24h", "7d", "30d"]


def test_admin_stats_popular_products_sum_quantity_and_revenue(client, db_session):
    customer = _customer(db_session)
    widget = _product(db_session, "Widget")
    gadget = _product(db_session, "Gadget")
    gizmo = _product(db_session, "Gizmo")
    paid = {"payment_status": "paid", "status": "confirmed"}
    _order(db_session, customer, lines=[(widget, 2, "10.00"), (gadget, 5, "3.00")], **paid)
    _order(db_session, customer, lines=[(widget, 1, "12.00")], **paid)
    # Bought, then cancelled: not popular.
    _order(db_session, customer, lines=[(gizmo, 50, "1.00")],
           status="cancelled", payment_status="paid")

    popular = _stats(client, _admin_headers(client, db_session), "24h")["popular_products"]

    assert popular == [
        {"id": gadget.id, "name": "Gadget", "total_quantity": 5, "total_revenue": "15.00",
         "currency": "USD"},
        {"id": widget.id, "name": "Widget", "total_quantity": 3, "total_revenue": "32.00",
         "currency": "USD"},
    ]


def test_admin_stats_are_cached_per_period(client, db_session, fake_redis):
    customer = _customer(db_session)
    _order(db_session, customer, total="10.00", payment_status="paid", status="confirmed")
    headers = _admin_headers(client, db_session)
    first = _stats(client, headers, "24h")

    _order(db_session, customer, total="90.00", payment_status="paid", status="confirmed")

    # Served from the 5-minute cache: the new order is not in it yet...
    assert _stats(client, headers, "24h") == first
    assert 0 < fake_redis.ttl("admin_stats_24h") <= 300
    # ...but the cache is per period, so another period sees it.
    assert _stats(client, headers, "7d")["total_revenue"] == "100.00"


def test_admin_stats_reject_an_unknown_period(client, db_session):
    response = client.get(
        f"{ADMIN}/stats", params={"period": "1y"}, headers=_admin_headers(client, db_session)
    )

    assert response.status_code == 422


# --- Status transitions --------------------------------------------------------

STATUSES = ["new", "confirmed", "processing", "sent", "delivered", "cancelled"]
# The order lifecycle, written out independently of the code under test:
# forward one step at a time, cancellable until delivered, nothing after
# delivered or cancelled.
ALLOWED = {
    "new": {"confirmed", "cancelled"},
    "confirmed": {"processing", "cancelled"},
    "processing": {"sent", "cancelled"},
    "sent": {"delivered", "cancelled"},
    "delivered": set(),
    "cancelled": set(),
}


@pytest.mark.parametrize("old", STATUSES)
@pytest.mark.parametrize("new", STATUSES)
def test_admin_status_change_follows_the_order_lifecycle(client, db_session, old, new):
    order = _order(db_session, _customer(db_session), status=old)

    response = client.put(
        f"{ORDERS}/{order.id}/status", json={"status": new},
        headers=_admin_headers(client, db_session),
    )

    db_session.expire_all()
    stored = db_session.get(Order, order.id).status
    if new in ALLOWED[old]:
        assert response.status_code == 200, response.text
        assert response.json()["status"] == new
        assert stored == new
    else:
        assert response.status_code == 409
        assert stored == old


def test_admin_walks_an_order_through_its_whole_lifecycle(client, db_session):
    order = _order(db_session, _customer(db_session))
    headers = _admin_headers(client, db_session)

    for status in ("confirmed", "processing", "sent", "delivered"):
        response = client.put(
            f"{ORDERS}/{order.id}/status", json={"status": status}, headers=headers
        )
        assert response.status_code == 200, (status, response.text)

    db_session.expire_all()
    assert db_session.get(Order, order.id).status == "delivered"


def test_non_admin_cannot_change_an_order_status(client, db_session):
    customer = _customer(db_session)
    order = _order(db_session, customer)

    response = client.put(
        f"{ORDERS}/{order.id}/status", json={"status": "confirmed"},
        headers=_headers(client, "buyer@example.com"),
    )

    assert response.status_code == 403
    db_session.expire_all()
    assert db_session.get(Order, order.id).status == "new"


# --- Refunds through the admin -------------------------------------------------


def test_non_admin_cannot_refund_even_their_own_order(client, db_session):
    order = _order(db_session, _customer(db_session), payment_status="paid")

    response = client.post(
        f"{ORDERS}/{order.id}/refund", json={}, headers=_with_idempotency_key(_headers(client, "buyer@example.com"))
    )

    assert response.status_code == 403


def test_admin_refund_of_a_missing_order_is_404(client, db_session):
    response = client.post(
        f"{ORDERS}/999999/refund", json={}, headers=_with_idempotency_key(_admin_headers(client, db_session))
    )

    assert response.status_code == 404


def test_admin_refund_needs_a_succeeded_payment(client, db_session):
    # Marked paid, but the only payment attempt on record failed: there is
    # nothing at the provider to refund against.
    order = _order(db_session, _customer(db_session), payment_status="paid")
    db_session.add(Payment(
        order_id=order.id, payment_intent_id="pi_failed_attempt", amount=1000,
        currency="usd", status="failed",
    ))
    db_session.commit()

    response = client.post(
        f"{ORDERS}/{order.id}/refund", json={}, headers=_with_idempotency_key(_admin_headers(client, db_session))
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "No successful payment found for this order"
    db_session.expire_all()
    assert db_session.get(Order, order.id).payment_status == "paid"


@pytest.mark.parametrize("amount", ["0", "-5.00"])
def test_admin_refund_amount_must_be_positive(client, db_session, amount):
    order = _order(db_session, _customer(db_session), payment_status="paid")

    response = client.post(
        f"{ORDERS}/{order.id}/refund", json={"amount": amount},
        headers=_with_idempotency_key(_admin_headers(client, db_session)),
    )

    assert response.status_code == 422
