"""
A disputed order is visible as such: `dispute_status` on the order (the
customer's GET /orders/{id} and history, the admin order list) is the status
of the latest dispute on the order's payments, null when there is none, and
GET /admin/orders?has_dispute= selects the orders whose dispute is open or
was lost.

The decision behind it (2026-11-03): a lost dispute only flags the order.
Its status stays as it was, it is not cancelled, and no stock is returned;
the manager returns stock by hand through the admin product/variant update
when the goods are physically back. payment_status becomes refunded, as
before.
"""
import logging
import uuid

import pytest
from payment_support import Shop, order_state, reset_provider

ADMIN_ORDERS = "/api/v1/admin/orders"


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def shop(client, db_session, monkeypatch):
    return Shop(client, db_session)


@pytest.fixture
def paid(shop):
    product = shop.product(price="19.99", stock=4)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    assert shop.pay(buyer, order["id"]).status_code == 200
    return {"order": order, "buyer": buyer, "admin": shop.admin(), "product": product,
            "payment": shop.payments(order["id"])[0]}


def _dispute(payment, status, dispute_id="du_status_1"):
    return {"id": dispute_id, "object": "dispute", "amount": payment.amount,
            "currency": payment.currency.lower(), "charge": f"ch_{uuid.uuid4().hex[:24]}",
            "payment_intent": payment.payment_intent_id, "status": status,
            "reason": "fraudulent"}


def _customer_view(shop, paid):
    order_id = paid["order"]["id"]
    detail = shop.get_order(paid["buyer"], order_id)
    assert detail.status_code == 200, detail.text
    history = shop.list_orders(paid["buyer"]).json()["items"]
    (in_history,) = [o for o in history if o["id"] == order_id]
    assert in_history["dispute_status"] == detail.json()["dispute_status"]
    return detail.json()["dispute_status"]


def _admin_list(shop, paid, **params):
    response = shop.client.get(ADMIN_ORDERS, params=params, headers=paid["admin"])
    assert response.status_code == 200, response.text
    return {o["id"]: o["dispute_status"] for o in response.json()["items"]}


def test_a_dispute_is_logged_once_per_status_not_once_per_event(shop, paid, caplog):
    # created and funds_withdrawn both report needs_response.
    with caplog.at_level(logging.WARNING, logger="app.services.stripe_events"):
        for event in ("charge.dispute.created", "charge.dispute.funds_withdrawn"):
            shop.webhook(event, _dispute(paid["payment"], "needs_response"))
        shop.webhook("charge.dispute.closed", _dispute(paid["payment"], "won"))

    lines = [r.getMessage() for r in caplog.records if "Dispute du_status_1" in r.getMessage()]
    assert len(lines) == 2
    assert "needs_response" in lines[0] and "won" in lines[1]


def test_an_order_without_a_dispute_shows_none(shop, paid):
    order_id = paid["order"]["id"]

    assert _customer_view(shop, paid) is None
    assert _admin_list(shop, paid)[order_id] is None
    assert order_id in _admin_list(shop, paid, has_dispute="false")
    assert order_id not in _admin_list(shop, paid, has_dispute="true")


def test_an_open_dispute_is_shown_and_selected(shop, paid):
    order_id = paid["order"]["id"]

    shop.webhook("charge.dispute.created", _dispute(paid["payment"], "needs_response"))

    assert _customer_view(shop, paid) == "needs_response"
    assert _admin_list(shop, paid)[order_id] == "needs_response"
    assert order_id in _admin_list(shop, paid, has_dispute="true")
    assert order_id not in _admin_list(shop, paid, has_dispute="false")
    assert order_state(shop, order_id) == ("confirmed", "paid")


def test_a_won_dispute_is_shown_but_needs_no_attention(shop, paid):
    order_id = paid["order"]["id"]
    shop.webhook("charge.dispute.created", _dispute(paid["payment"], "needs_response"))

    shop.webhook("charge.dispute.closed", _dispute(paid["payment"], "won"))

    assert _customer_view(shop, paid) == "won"
    assert order_id not in _admin_list(shop, paid, has_dispute="true")
    assert order_id in _admin_list(shop, paid, has_dispute="false")
    assert order_state(shop, order_id) == ("confirmed", "paid")


def test_a_lost_dispute_only_flags_the_order(shop, paid):
    order_id = paid["order"]["id"]
    shop.webhook("charge.dispute.created", _dispute(paid["payment"], "needs_response"))

    shop.webhook("charge.dispute.closed", _dispute(paid["payment"], "lost"))

    assert _customer_view(shop, paid) == "lost"
    assert _admin_list(shop, paid)[order_id] == "lost"
    assert order_id in _admin_list(shop, paid, has_dispute="true")
    # Status unchanged (not cancelled), payment_status refunded as before, and
    # the stock the order took is not returned automatically.
    assert order_state(shop, order_id) == ("confirmed", "refunded")
    assert shop.stock(paid["product"]) == 3


def _second_paid_order(shop, paid):
    """A paid order of another customer, for the same product."""
    buyer = shop.buyer()
    order = shop.order_of(buyer, paid["product"])
    assert shop.pay(buyer, order["id"]).status_code == 200
    return {"order": order, "buyer": buyer, "admin": paid["admin"],
            "payment": shop.payments(order["id"])[0]}


def test_a_dispute_shows_only_on_its_own_order(shop, paid):
    # The property used to read the newest dispute of any order: one dispute
    # showed on every order of every customer (load audit 2026-11).
    other = _second_paid_order(shop, paid)

    shop.webhook("charge.dispute.created", _dispute(paid["payment"], "needs_response"))

    assert _customer_view(shop, paid) == "needs_response"
    assert _customer_view(shop, other) is None
    admin_view = _admin_list(shop, paid)
    assert admin_view[paid["order"]["id"]] == "needs_response"
    assert admin_view[other["order"]["id"]] is None


def test_two_disputed_orders_each_show_their_own_dispute(shop, paid):
    other = _second_paid_order(shop, paid)
    shop.webhook("charge.dispute.created", _dispute(paid["payment"], "needs_response"))
    # The newer dispute (higher id) is the other order's.
    shop.webhook("charge.dispute.created",
                 _dispute(other["payment"], "warning_needs_response", dispute_id="du_status_2"))

    assert _customer_view(shop, paid) == "needs_response"
    assert _customer_view(shop, other) == "warning_needs_response"
    admin_view = _admin_list(shop, paid)
    assert admin_view[paid["order"]["id"]] == "needs_response"
    assert admin_view[other["order"]["id"]] == "warning_needs_response"
