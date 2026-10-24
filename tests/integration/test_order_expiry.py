"""
Unpaid orders give their stock back: a customer can cancel their own
unpaid order (POST /orders/{id}/cancel), and a periodic task cancels every
order still unpaid UNPAID_ORDER_EXPIRE_MINUTES after it was placed.

Stock is taken when the order is placed (create_order), not when it is
paid. Before this, only an admin could cancel, so an order nobody paid for
kept its items out of sale indefinitely - and a few accounts placing
orders they never pay for could take a product off sale entirely.

Every cancellation path (admin, customer, expiry) goes through the same
restock code, under a lock on the order row, and only from a state that
can still be cancelled, so stock is returned exactly once.
"""
from datetime import timedelta

import pytest
from conftest import TestingSessionLocal, engine
from sqlalchemy.orm import Session, sessionmaker
from test_orders import (
    CART_PREFIX,
    ORDERS_PREFIX,
    _auth_headers,
    _checkout_ready_buyer,
    _login,
    _make_admin,
    _make_category,
    _make_product,
    _make_variant,
    _with_idempotency_key,
)

from app import tasks
from app.core.clock import utc_now
from app.core.config import settings
from app.models.order import Order
from app.models.payment import Payment
from app.models.product import Product, ProductVariant
from app.services.orders import cancel_order_and_restock
from app.services.payment.stripe_provider import TOKEN_SUCCESS_DEFAULT, StripePaymentProvider


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    StripePaymentProvider.reset_mock_state()
    monkeypatch.setattr("app.api.v1.orders.send_order_confirmation_email", lambda *a, **k: True)
    monkeypatch.setattr("app.api.v1.orders.send_order_status_update_email", lambda *a, **k: True)
    yield
    StripePaymentProvider.reset_mock_state()


def _place(client, db_session, email="buyer@example.com", quantity=3, variant_quantity=0):
    """An order for `quantity` of a 10-in-stock product (and optionally
    `variant_quantity` of a 5-in-stock variant of it). Returns
    (token, order dict, product, variant or None)."""
    token, address_id = _checkout_ready_buyer(client, db_session, email=email)
    product = _make_product(db_session, _make_category(db_session).id, stock=10)
    variant = None
    client.post(
        f"{CART_PREFIX}/items",
        json={"product_id": product.id, "quantity": quantity},
        headers=_auth_headers(token),
    )
    if variant_quantity:
        variant = _make_variant(db_session, product.id, stock=5)
        client.post(
            f"{CART_PREFIX}/items",
            json={"product_id": product.id, "quantity": variant_quantity, "variant_id": variant.id},
            headers=_auth_headers(token),
        )
    response = client.post(
        ORDERS_PREFIX,
        json={"shipping_address_id": address_id},
        headers=_with_idempotency_key(_auth_headers(token)),
    )
    assert response.status_code == 200, response.text
    return token, response.json()["data"], product, variant


def _stock(db_session, product, variant=None):
    db_session.expire_all()
    product_stock = db_session.get(Product, product.id).stock_quantity
    if variant is None:
        return product_stock
    return product_stock, db_session.get(ProductVariant, variant.id).stock_quantity


def _order(db_session, order_id):
    db_session.expire_all()
    return db_session.get(Order, order_id)


def _cancel(client, token, order_id):
    return client.post(f"{ORDERS_PREFIX}/{order_id}/cancel", headers=_auth_headers(token))


def _pay(client, token, order):
    return client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay",
        json={"order_id": order["id"], "payment_method": "stripe",
              "amount": order["total_amount"], "payment_method_token": TOKEN_SUCCESS_DEFAULT},
        headers=_with_idempotency_key(_auth_headers(token)),
    )


def _age(db_session, order_id, minutes):
    order = db_session.get(Order, order_id)
    order.created_at = utc_now() - timedelta(minutes=minutes)
    db_session.commit()


# -- The customer cancels their own unpaid order ------------------------------

def test_customer_cancels_own_unpaid_order_and_stock_comes_back(client, db_session):
    token, order, product, variant = _place(client, db_session, quantity=3, variant_quantity=2)
    assert _stock(db_session, product, variant) == (7, 3)

    response = _cancel(client, token, order["id"])

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "cancelled"
    assert _stock(db_session, product, variant) == (10, 5)


def test_customer_cancels_an_order_whose_payment_failed(client, db_session):
    token, order, product, _ = _place(client, db_session)
    db_order = db_session.get(Order, order["id"])
    db_order.payment_status = "failed"
    db_session.commit()

    assert _cancel(client, token, order["id"]).status_code == 200
    assert _stock(db_session, product) == 10


def test_cancelling_twice_returns_the_stock_once(client, db_session):
    token, order, product, _ = _place(client, db_session)
    assert _cancel(client, token, order["id"]).status_code == 200

    again = _cancel(client, token, order["id"])

    assert again.status_code == 400
    assert _stock(db_session, product) == 10


def test_customer_cannot_cancel_a_paid_order(client, db_session):
    # A paid order is cancelled by support, with a refund; the customer's
    # own cancel only releases stock nobody has paid for.
    token, order, product, _ = _place(client, db_session)
    assert _pay(client, token, order).status_code == 200

    response = _cancel(client, token, order["id"])

    assert response.status_code == 400
    assert _order(db_session, order["id"]).status == "confirmed"
    assert _stock(db_session, product) == 7


def test_customer_cannot_cancel_someone_elses_order(client, db_session):
    _, order, product, _ = _place(client, db_session, email="owner@example.com")
    other_token, _ = _checkout_ready_buyer(client, db_session, email="other@example.com")

    response = _cancel(client, other_token, order["id"])

    assert response.status_code == 404
    assert _order(db_session, order["id"]).status == "new"
    assert _stock(db_session, product) == 7


def test_cancel_requires_authentication(client, db_session):
    _, order, _, _ = _place(client, db_session)
    assert client.post(f"{ORDERS_PREFIX}/{order['id']}/cancel").status_code == 401


def test_a_cancelled_order_cannot_be_paid(client, db_session):
    token, order, _, _ = _place(client, db_session)
    assert _cancel(client, token, order["id"]).status_code == 200

    response = _pay(client, token, order)

    assert response.status_code == 400
    assert _order(db_session, order["id"]).payment_status == "pending"


def test_admin_cancel_after_customer_cancel_does_not_restock_again(client, db_session):
    token, order, product, _ = _place(client, db_session)
    assert _cancel(client, token, order["id"]).status_code == 200
    admin = _make_admin(db_session)
    admin_token = _login(client, admin.email, "AdminPass1")

    response = client.put(
        f"{ORDERS_PREFIX}/{order['id']}/status",
        json={"status": "cancelled"},
        headers=_auth_headers(admin_token),
    )

    assert response.status_code == 400
    assert _stock(db_session, product) == 10


# -- Unpaid orders expire --------------------------------------------------------

def test_expiry_window_is_a_setting_of_thirty_minutes():
    assert settings.UNPAID_ORDER_EXPIRE_MINUTES == 30


def test_beat_runs_the_expiry_task_every_five_minutes():
    entry = tasks.celery.conf.beat_schedule["cancel-expired-unpaid-orders"]
    assert entry["task"] == "cancel_expired_unpaid_orders"
    assert entry["task"] in tasks.celery.tasks
    assert entry["schedule"] == timedelta(minutes=5)


def test_expired_unpaid_order_is_cancelled_and_restocked(client, db_session, tasks_db):
    _, order, product, variant = _place(client, db_session, quantity=3, variant_quantity=2)
    _age(db_session, order["id"], settings.UNPAID_ORDER_EXPIRE_MINUTES + 1)

    cancelled = tasks.cancel_expired_unpaid_orders()

    assert cancelled == 1
    assert _order(db_session, order["id"]).status == "cancelled"
    assert _stock(db_session, product, variant) == (10, 5)


def test_expiry_leaves_recent_paid_and_already_cancelled_orders_alone(
    client, db_session, tasks_db
):
    window = settings.UNPAID_ORDER_EXPIRE_MINUTES
    _, recent, recent_product, _ = _place(client, db_session, email="recent@example.com")
    _age(db_session, recent["id"], window - 1)

    paid_token, paid, paid_product, _ = _place(client, db_session, email="paid@example.com")
    assert _pay(client, paid_token, paid).status_code == 200
    _age(db_session, paid["id"], window * 10)

    gone_token, gone, gone_product, _ = _place(client, db_session, email="gone@example.com")
    assert _cancel(client, gone_token, gone["id"]).status_code == 200
    _age(db_session, gone["id"], window * 10)

    assert tasks.cancel_expired_unpaid_orders() == 0

    assert _order(db_session, recent["id"]).status == "new"
    assert _order(db_session, paid["id"]).status == "confirmed"
    assert _order(db_session, gone["id"]).status == "cancelled"
    assert _stock(db_session, recent_product) == 7
    assert _stock(db_session, paid_product) == 7
    assert _stock(db_session, gone_product) == 10  # restocked once, by the customer


def test_expiry_cancels_an_order_whose_payment_failed(client, db_session, tasks_db):
    _, order, product, _ = _place(client, db_session)
    db_order = db_session.get(Order, order["id"])
    db_order.payment_status = "failed"
    db_session.commit()
    _age(db_session, order["id"], settings.UNPAID_ORDER_EXPIRE_MINUTES + 1)

    assert tasks.cancel_expired_unpaid_orders() == 1
    assert _stock(db_session, product) == 10


def test_expiry_rechecks_an_order_paid_after_it_was_listed(
    client, db_session, tasks_db, monkeypatch
):
    # The task lists candidates first and locks them one by one; an order
    # paid in between is seen as paid under the lock and left alone. "In
    # between" is the commit that ends the listing: the payment is committed
    # from another session right after it, before the task's locking read.
    # (Not from inside the locked section: on PostgreSQL that write would wait
    # for the lock the task holds, which is the point of the lock.)
    token, order, product, _ = _place(client, db_session)
    _age(db_session, order["id"], settings.UNPAID_ORDER_EXPIRE_MINUTES + 1)

    class _PaidAfterListing(Session):
        paid = False

        def commit(self):
            super().commit()
            if not _PaidAfterListing.paid:
                _PaidAfterListing.paid = True
                other = TestingSessionLocal()
                try:
                    db_order = other.get(Order, order["id"])
                    db_order.payment_status = "paid"
                    db_order.status = "confirmed"
                    other.commit()
                finally:
                    other.close()

    monkeypatch.setattr(
        tasks, "SessionLocal",
        sessionmaker(class_=_PaidAfterListing, autoflush=False, bind=engine),
    )

    assert tasks.cancel_expired_unpaid_orders() == 0
    assert _order(db_session, order["id"]).status == "confirmed"
    assert _stock(db_session, product) == 7


def test_cancel_and_restock_refuses_an_order_that_cannot_be_cancelled(client, db_session):
    _, order, product, _ = _place(client, db_session)
    db_order = db_session.get(Order, order["id"])
    db_order.status = "delivered"
    db_session.commit()

    assert cancel_order_and_restock(db_session, db_order) is False
    db_session.commit()
    assert _order(db_session, order["id"]).status == "delivered"
    assert _stock(db_session, product) == 7


def test_running_the_expiry_twice_restocks_once(client, db_session, tasks_db):
    _, order, product, _ = _place(client, db_session)
    _age(db_session, order["id"], settings.UNPAID_ORDER_EXPIRE_MINUTES + 1)

    assert tasks.cancel_expired_unpaid_orders() == 1
    assert tasks.cancel_expired_unpaid_orders() == 0
    assert _stock(db_session, product) == 10


def test_expiry_uses_the_configured_window(client, db_session, tasks_db, monkeypatch):
    _, order, _, _ = _place(client, db_session)
    _age(db_session, order["id"], 6)
    with monkeypatch.context() as m:
        m.setattr(settings, "UNPAID_ORDER_EXPIRE_MINUTES", 5)
        assert tasks.cancel_expired_unpaid_orders() == 1


def test_payment_completing_after_the_order_expired_is_flagged_not_applied(
    client, db_session, tasks_db, monkeypatch
):
    # The order expires (and its stock is sold again) while a /pay for it
    # is waiting on the provider. The charge that then succeeds must not
    # turn the cancelled order into a paid one; it is recorded and flagged
    # for a refund, the same as a late webhook for a cancelled order.
    token, order, product, _ = _place(client, db_session)
    real_confirm = StripePaymentProvider.confirm_payment_intent

    def _confirm_after_expiry(self, payment_intent_id, **kwargs):
        _age(db_session, order["id"], settings.UNPAID_ORDER_EXPIRE_MINUTES + 1)
        assert tasks.cancel_expired_unpaid_orders() == 1
        return real_confirm(self, payment_intent_id, **kwargs)

    monkeypatch.setattr(StripePaymentProvider, "confirm_payment_intent", _confirm_after_expiry)

    response = _pay(client, token, order)

    assert response.status_code == 200, response.text
    db_order = _order(db_session, order["id"])
    assert db_order.status == "cancelled"
    assert db_order.payment_status == "pending"
    payment = db_session.query(Payment).filter(Payment.order_id == order["id"]).one()
    assert (payment.status, payment.requires_manual_review) == ("succeeded", True)
    assert _stock(db_session, product) == 10
