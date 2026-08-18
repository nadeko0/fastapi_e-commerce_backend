"""
Tests for the mock Stripe payment integration: app/services/payment/*,
and the /orders/{id}/pay, /orders/{id}/refund and /orders/webhooks/stripe
endpoints in app/api/v1/orders.py.

Each test that specifically defends against a known class of real Stripe
integration bug says so in a comment, per the traceability the task asked
for. The bug classes (missing/skipped signature verification, missing
idempotency on PaymentIntent creation, missing idempotency on webhook
processing, the sync-confirm/async-webhook race, cents-vs-dollars amount
handling, and requires_action/3DS as a distinct non-terminal state) were
confirmed to still be current concerns via a web search done before writing
this integration (see PR description / task notes for sources).
"""
import json
import time
import uuid
from decimal import Decimal

import pytest

# tests/conftest.py owns the single SQLAlchemy engine/sessionmaker the app's
# get_db dependency is overridden to use (an in-memory SQLite DB shared for
# the whole test run via StaticPool). Reusing it here - rather than creating
# a second engine - is required to see the same data the API endpoints
# committed; pytest's default "prepend" import mode puts tests/ on
# sys.path (because conftest.py lives there with no __init__.py alongside
# it), which is what makes this plain top-level import resolve.
from conftest import TestingSessionLocal

from app.core.config import settings
from app.core.security import create_access_token, get_password_hash
from app.models.address import Address
from app.models.category import Category
from app.models.order import Order
from app.models.payment import Payment, WebhookEvent
from app.models.product import Product
from app.models.user import User
from app.services.payment.exceptions import PaymentProviderTimeoutError
from app.services.payment.stripe_provider import (
    TOKEN_DECLINE,
    TOKEN_REQUIRES_ACTION,
    TOKEN_SUCCESS_DEFAULT,
    StripePaymentProvider,
)

USERS_PREFIX = "/api/v1/users"
CART_PREFIX = "/api/v1/cart"
ORDERS_PREFIX = "/api/v1/orders"
WEBHOOK_URL = f"{ORDERS_PREFIX}/webhooks/stripe"


@pytest.fixture(autouse=True)
def _reset_stripe_mock_state():
    # StripePaymentProvider's idempotency-key cache is a *class* attribute
    # (mirrors idempotency dedup happening on Stripe's servers, not in our
    # process - see stripe_provider.py). Without this reset, a literal key
    # like "pay-key-1" reused across two different tests would return a
    # cached PaymentIntent from a previous, unrelated test.
    StripePaymentProvider.reset_mock_state()
    yield
    StripePaymentProvider.reset_mock_state()


@pytest.fixture(autouse=True)
def _stub_order_confirmation_email(monkeypatch):
    # Pre-existing, payment-unrelated bug: create_order's background task
    # calls send_order_confirmation_email(email, OrderResponse.from_orm(order))
    # but that function (app/services/email.py) formats item.product.name -
    # OrderResponse's items are OrderItemResponse (flat product_name field,
    # no `.product` relationship), so it raises AttributeError on every
    # order. Out of this task's scope (email.py/create_order's email step,
    # not the payment code), so it's stubbed out here rather than fixed, to
    # let these payment tests get past order setup.
    monkeypatch.setattr("app.api.v1.orders.send_order_confirmation_email", lambda *a, **k: True)
    yield


@pytest.fixture
def db_session():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_order(db_session, order_id: int) -> Order:
    db_session.expire_all()
    return db_session.query(Order).filter(Order.id == order_id).first()


def _get_payments(db_session, order_id: int):
    db_session.expire_all()
    return (
        db_session.query(Payment)
        .filter(Payment.order_id == order_id)
        .order_by(Payment.id)
        .all()
    )


def _create_verified_buyer_with_order(client, db_session, email="buyer@example.com"):
    """Registers a user, verifies them, gives them a valid checkout profile,
    an address, a product in stock, and places one order for it. Returns a
    dict with headers/order/user for use in payment tests."""
    payload = {
        "email": email,
        "password": "Str0ngPass1",
        "full_name": "Buyer One",
        "gdpr_consent": True,
        "privacy_policy_accepted": True,
        "marketing_consent": False,
    }
    client.post(f"{USERS_PREFIX}/register", json=payload)
    login = client.post(
        f"{USERS_PREFIX}/login",
        data={"username": email, "password": payload["password"]},
    )
    access_token = login.json()["data"]["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    user = db_session.query(User).filter(User.email == email).first()
    user.is_email_verified = True
    user.phone = "+15551234567"
    db_session.commit()

    address = Address(
        user_id=user.id,
        street="1 Main St",
        city="Metropolis",
        state="NY",
        postal_code="10001",
        country="US",
    )
    db_session.add(address)
    db_session.commit()
    db_session.refresh(address)

    category = Category(name="Widgets", path=[], level=0)
    db_session.add(category)
    db_session.commit()
    db_session.refresh(category)

    product = Product(
        name="Widget",
        description="A widget",
        price=Decimal("19.99"),
        stock_quantity=10,
        category_id=category.id,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)

    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 1},
        headers=headers,
    )
    order_resp = client.post(
        ORDERS_PREFIX,
        params={"shipping_address_id": address.id},
        headers=headers,
    )
    assert order_resp.status_code == 200, order_resp.text
    order = order_resp.json()["data"]

    return {"user": user, "headers": headers, "order": order, "product": product}


def _create_admin(db_session):
    admin = User(
        email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password=get_password_hash("adminpass1"),
        full_name="Admin",
        role="admin",
        is_active=True,
        is_email_verified=True,
        gdpr_consent=True,
        privacy_policy_accepted=True,
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    token = create_access_token(admin.id)
    return {"user": admin, "headers": {"Authorization": f"Bearer {token}"}}


def _payment_payload(order, token=TOKEN_SUCCESS_DEFAULT, currency="USD"):
    return {
        "order_id": order["id"],
        "payment_method": "stripe",
        "amount": order["total_amount"],
        "currency": currency,
        "payment_method_token": token,
    }


def _sign(body: bytes, secret: str = None, timestamp: int = None) -> str:
    return StripePaymentProvider.sign_payload(
        body, secret or settings.STRIPE_WEBHOOK_SECRET, timestamp=timestamp
    )


def _webhook_body(event_type: str, payment_intent_id: str, event_id: str = None) -> bytes:
    event_id = event_id or f"evt_{uuid.uuid4().hex[:24]}"
    return json.dumps(
        {
            "id": event_id,
            "type": event_type,
            "created": int(time.time()),
            "data": {"object": {"id": payment_intent_id}},
        }
    ).encode()


def _post_webhook(client, body: bytes, signature: str):
    return client.post(
        WEBHOOK_URL,
        content=body,
        headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
    )


# -- Successful payment -----------------------------------------------------

def test_successful_payment_confirms_intent_and_advances_order(client, db_session):
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]

    response = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay",
        json=_payment_payload(order),
        headers=setup["headers"],
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == "paid"
    assert body["transaction_id"].startswith("pi_")
    assert body["requires_action"] is False

    db_order = _get_order(db_session, order["id"])
    assert db_order.payment_status == "paid"
    assert db_order.status == "confirmed"


# -- Cents vs. dollars -------------------------------------------------------

def test_payment_amount_stored_as_integer_cents_not_decimal_dollars(client, db_session):
    # Regression test for: Stripe amounts are integers in the smallest
    # currency unit. A Decimal("19.99") dollar amount must become exactly
    # 1999 cents, not 19.99, 1998, or 2000 (float rounding).
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]
    assert order["total_amount"] in ("19.99", 19.99)

    response = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay",
        json=_payment_payload(order),
        headers=setup["headers"],
    )
    assert response.status_code == 200

    payments = _get_payments(db_session, order["id"])
    assert len(payments) == 1
    assert payments[0].amount == 1999
    assert isinstance(payments[0].amount, int)


# -- Declined payment ---------------------------------------------------------

def test_declined_payment_marks_order_failed_not_paid(client, db_session):
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]

    response = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay",
        json=_payment_payload(order, token=TOKEN_DECLINE),
        headers=setup["headers"],
    )

    assert response.status_code == 402
    db_order = _get_order(db_session, order["id"])
    assert db_order.payment_status == "failed"
    # Declining a payment must not advance the order's fulfillment status.
    assert db_order.status == "new"


# -- Provider timeout / unavailable -------------------------------------------

def test_provider_timeout_leaves_order_state_unchanged(client, db_session, monkeypatch):
    # Regression test for: a transport-level failure (provider unreachable)
    # must not be conflated with a decline or a success, and must not leave
    # the order half-updated - the order's payment_status/status must be
    # byte-for-byte what they were before the attempt.
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]

    def _boom(self):
        raise PaymentProviderTimeoutError("simulated network timeout")

    monkeypatch.setattr(
        "app.services.payment.stripe_provider.StripePaymentProvider._simulate_network_call",
        _boom,
    )

    response = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay",
        json=_payment_payload(order),
        headers=setup["headers"],
    )

    assert response.status_code == 503
    db_order = _get_order(db_session, order["id"])
    assert db_order.payment_status == "pending"
    assert db_order.status == "new"

    payments = _get_payments(db_session, order["id"])
    assert len(payments) == 1
    assert payments[0].status == "failed"


def test_retry_after_timeout_with_new_key_succeeds(client, db_session, monkeypatch):
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]

    def _boom(self):
        raise PaymentProviderTimeoutError("simulated network timeout")

    monkeypatch.setattr(
        "app.services.payment.stripe_provider.StripePaymentProvider._simulate_network_call",
        _boom,
    )
    failed_response = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay",
        json=_payment_payload(order),
        headers={**setup["headers"], "Idempotency-Key": "attempt-1"},
    )
    assert failed_response.status_code == 503

    monkeypatch.undo()

    retry_response = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay",
        json=_payment_payload(order),
        headers={**setup["headers"], "Idempotency-Key": "attempt-2"},
    )
    assert retry_response.status_code == 200
    assert retry_response.json()["data"]["status"] == "paid"

    db_order = _get_order(db_session, order["id"])
    assert db_order.payment_status == "paid"


# -- Retry after decline -------------------------------------------------------

def test_retry_after_decline_succeeds_without_side_effects_from_failed_attempt(
    client, db_session
):
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]

    declined = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay",
        json=_payment_payload(order, token=TOKEN_DECLINE),
        headers={**setup["headers"], "Idempotency-Key": "attempt-1"},
    )
    assert declined.status_code == 402

    retried = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay",
        json=_payment_payload(order, token=TOKEN_SUCCESS_DEFAULT),
        headers={**setup["headers"], "Idempotency-Key": "attempt-2"},
    )
    assert retried.status_code == 200
    assert retried.json()["data"]["status"] == "paid"

    db_order = _get_order(db_session, order["id"])
    assert db_order.payment_status == "paid"
    assert db_order.status == "confirmed"

    payments = _get_payments(db_session, order["id"])
    assert len(payments) == 2
    assert payments[0].status == "failed"
    assert payments[1].status == "succeeded"


# -- Idempotent PaymentIntent creation ----------------------------------------

def test_duplicate_payment_request_same_idempotency_key_returns_same_intent(
    client, db_session
):
    # Regression test for: missing idempotency on PaymentIntent creation
    # leads to double-charging when a client retries a checkout request
    # (timeout, double-click). The second request with the same
    # Idempotency-Key must return the original PaymentIntent, not create a
    # second one / charge the card twice.
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]
    headers = {**setup["headers"], "Idempotency-Key": "checkout-pay-1"}

    first = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay", json=_payment_payload(order), headers=headers
    )
    second = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay", json=_payment_payload(order), headers=headers
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["data"]["transaction_id"] == second.json()["data"]["transaction_id"]
    assert first.json()["data"]["id"] == second.json()["data"]["id"]

    payments = _get_payments(db_session, order["id"])
    assert len(payments) == 1


# -- requires_action / 3D Secure ----------------------------------------------

def test_requires_action_is_a_distinct_non_terminal_state(client, db_session):
    # Regression test for: conflating requires_action (3D Secure/SCA
    # pending) with either success or failure. The order must stay exactly
    # as it was (still awaiting payment) and the response must surface a
    # client_secret so the frontend can complete authentication.
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]

    response = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay",
        json=_payment_payload(order, token=TOKEN_REQUIRES_ACTION),
        headers=setup["headers"],
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == "requires_action"
    assert body["requires_action"] is True
    assert body["client_secret"]

    db_order = _get_order(db_session, order["id"])
    assert db_order.payment_status == "pending"
    assert db_order.status == "new"

    payments = _get_payments(db_session, order["id"])
    assert payments[0].status == "requires_action"


# -- Webhook signature verification -------------------------------------------

def test_webhook_missing_signature_is_rejected(client, db_session):
    # Regression test for: missing/skipped webhook signature verification -
    # a classic, still-current Stripe integration mistake per current
    # Stripe docs and community guidance.
    body = _webhook_body("payment_intent.succeeded", "pi_doesnotmatter")

    response = client.post(
        WEBHOOK_URL, content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 400
    assert db_session.query(WebhookEvent).count() == 0


def test_webhook_invalid_signature_is_rejected(client, db_session):
    body = _webhook_body("payment_intent.succeeded", "pi_doesnotmatter")
    bad_signature = _sign(body, secret="wrong-secret")

    response = _post_webhook(client, body, bad_signature)

    assert response.status_code == 400
    assert db_session.query(WebhookEvent).count() == 0


def test_webhook_success_finalizes_a_requires_action_payment(client, db_session):
    # This is the async counterpart of the requires_action test above: the
    # customer completes 3DS out-of-band and Stripe later confirms via
    # webhook - this must apply the same success path as a synchronous pay.
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]

    pay_response = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay",
        json=_payment_payload(order, token=TOKEN_REQUIRES_ACTION),
        headers=setup["headers"],
    )
    intent_id = pay_response.json()["data"]["transaction_id"]

    body = _webhook_body("payment_intent.succeeded", intent_id)
    response = _post_webhook(client, body, _sign(body))

    assert response.status_code == 200
    db_order = _get_order(db_session, order["id"])
    assert db_order.payment_status == "paid"
    assert db_order.status == "confirmed"


# -- Idempotent webhook processing --------------------------------------------

def test_duplicate_webhook_delivery_does_not_double_apply(client, db_session, monkeypatch):
    # Regression test for: Stripe guarantees at-least-once webhook delivery
    # and retries for up to 72 hours, so the same event.id can arrive more
    # than once. A duplicate delivery must not re-mark the order paid (it
    # already is) and must not re-send the confirmation email.
    email_calls = []
    monkeypatch.setattr(
        "app.api.v1.orders.send_order_status_update_email",
        lambda *a, **k: email_calls.append(a) or True,
    )

    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]
    pay_response = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay",
        json=_payment_payload(order, token=TOKEN_REQUIRES_ACTION),
        headers=setup["headers"],
    )
    intent_id = pay_response.json()["data"]["transaction_id"]

    event_id = f"evt_{uuid.uuid4().hex[:24]}"
    body = _webhook_body("payment_intent.succeeded", intent_id, event_id=event_id)
    signature = _sign(body)

    first = _post_webhook(client, body, signature)
    second = _post_webhook(client, body, signature)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["data"]["status"] == "already_processed"

    db_order = _get_order(db_session, order["id"])
    assert db_order.payment_status == "paid"
    assert len(email_calls) == 1
    assert db_session.query(WebhookEvent).filter(WebhookEvent.event_id == event_id).count() == 1


# -- Conflict: webhook succeeded for an already-cancelled order --------------

def test_webhook_success_for_cancelled_order_is_flagged_not_applied(client, db_session):
    # Decision documented in app/api/v1/orders.py's stripe_webhook: a
    # payment_intent.succeeded event for an order that was cancelled after
    # the attempt started is a genuine race, not a bug. We must not
    # silently mark the cancelled order paid - instead the Payment row is
    # flagged requires_manual_review and the order is left untouched.
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]
    admin = _create_admin(db_session)

    pay_response = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay",
        json=_payment_payload(order, token=TOKEN_REQUIRES_ACTION),
        headers=setup["headers"],
    )
    intent_id = pay_response.json()["data"]["transaction_id"]

    cancel_response = client.put(
        f"{ORDERS_PREFIX}/{order['id']}/status",
        params={"status": "cancelled"},
        headers=admin["headers"],
    )
    assert cancel_response.status_code == 200

    body = _webhook_body("payment_intent.succeeded", intent_id)
    response = _post_webhook(client, body, _sign(body))

    assert response.status_code == 200
    assert response.json()["data"]["requires_manual_review"] is True
    assert response.json()["data"]["applied"] is False

    db_order = _get_order(db_session, order["id"])
    assert db_order.status == "cancelled"
    assert db_order.payment_status == "pending"

    payments = _get_payments(db_session, order["id"])
    assert payments[0].requires_manual_review is True


def test_cannot_start_a_payment_on_an_already_cancelled_order(client, db_session):
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]
    admin = _create_admin(db_session)

    client.put(
        f"{ORDERS_PREFIX}/{order['id']}/status",
        params={"status": "cancelled"},
        headers=admin["headers"],
    )

    response = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay",
        json=_payment_payload(order),
        headers=setup["headers"],
    )
    assert response.status_code == 400


# -- Refunds --------------------------------------------------------------

def test_partial_refund_keeps_order_paid(client, db_session):
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]
    admin = _create_admin(db_session)

    client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay",
        json=_payment_payload(order),
        headers=setup["headers"],
    )

    response = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/refund",
        json={"amount": "5.00"},
        headers=admin["headers"],
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["amount"] in ("5.00", 5.00)
    assert body["payment_status"] == "paid"

    db_order = _get_order(db_session, order["id"])
    assert db_order.payment_status == "paid"

    payments = _get_payments(db_session, order["id"])
    assert payments[0].amount_refunded == 500


def test_full_refund_marks_order_refunded(client, db_session):
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]
    admin = _create_admin(db_session)

    client.post(
        f"{ORDERS_PREFIX}/{order['id']}/pay",
        json=_payment_payload(order),
        headers=setup["headers"],
    )

    response = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/refund",
        json={},
        headers=admin["headers"],
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["payment_status"] == "refunded"

    db_order = _get_order(db_session, order["id"])
    assert db_order.payment_status == "refunded"

    payments = _get_payments(db_session, order["id"])
    assert payments[0].amount_refunded == payments[0].amount


def test_refund_requires_a_paid_order(client, db_session):
    setup = _create_verified_buyer_with_order(client, db_session)
    order = setup["order"]
    admin = _create_admin(db_session)

    response = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/refund",
        json={},
        headers=admin["headers"],
    )

    assert response.status_code == 400
