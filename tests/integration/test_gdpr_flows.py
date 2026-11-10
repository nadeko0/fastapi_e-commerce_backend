"""GDPR flows end to end through /legal: consent changes and erasure."""

import pytest
from test_order_expiry import _order, _pay, _place, _stock
from test_orders import ORDERS_PREFIX, _auth_headers, _with_idempotency_key

from app.models.user import User
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.payment.types import CheckoutSessionStatus

USERS = "/api/v1/users"
LEGAL = "/api/v1/legal"
EMAIL = "gdpr.flow@example.com"
PASSWORD = "Str0ngPass1"


@pytest.fixture
def headers(client, monkeypatch):
    for name in ("send_gdpr_request_received", "send_gdpr_deletion_confirmation"):
        monkeypatch.setattr(f"app.services.email.{name}", lambda *a, **k: True)
    client.post(f"{USERS}/register", json={
        "email": EMAIL, "password": PASSWORD, "full_name": "Gdpr Flow",
        "gdpr_consent": True, "privacy_policy_accepted": True,
    })
    response = client.post(f"{USERS}/login", data={"username": EMAIL, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _stored(db_session):
    db_session.expire_all()
    return db_session.query(User).filter(User.email == EMAIL).one()


def test_withdrawing_the_privacy_policy_is_stored_in_its_column_and_in_the_history(
    client, db_session, headers
):
    # privacy_policy used to be written to a "privacy_policy_consent"
    # attribute the model does not have: the history said "withdrawn", the
    # stored flag stayed "accepted", and the answer was a 200.
    response = client.post(
        f"{LEGAL}/consent",
        json={"marketing_consent": False, "privacy_policy_accepted": False},
        headers=headers,
    )

    assert response.status_code == 200
    user = _stored(db_session)
    assert user.privacy_policy_accepted is False
    assert user.privacy_policy_accepted_date is not None
    assert user.consent_history[-1]["type"] == "privacy_policy"
    assert user.consent_history[-1]["granted"] is False


# --- erasure --------------------------------------------------------------


def test_erasure_schedules_the_account_and_ends_its_sessions(client, db_session, headers):
    request = {"request_type": "deletion", "password": PASSWORD}

    response = client.post(f"{LEGAL}/data-request", json=request, headers=headers)

    assert response.status_code == 200
    user = _stored(db_session)
    assert (user.is_active, user.data_deletion_requested) == (False, True)
    assert user.data_deletion_date is not None
    assert client.get(f"{USERS}/me", headers=headers).status_code == 403
    # A second request is refused: the account is no longer active.
    again = client.post(f"{LEGAL}/data-request", json=request, headers=headers)
    assert again.status_code == 403
    # Logging in again is refused like a wrong password.
    login = client.post(f"{USERS}/login", data={"username": EMAIL, "password": PASSWORD})
    assert login.status_code == 401
    assert login.json()["detail"] == "Incorrect email or password"


# --- erasure and the account's orders -------------------------------------
# The account is deactivated at once, so its owner gets 403 on every order
# route. An unpaid order left open could still be paid on its Checkout
# Session and become a confirmed, paid order nobody can see.


@pytest.fixture
def buyer_quiet(monkeypatch):
    StripePaymentProvider.reset_mock_state()
    for name in ("send_gdpr_request_received", "send_gdpr_deletion_confirmation"):
        monkeypatch.setattr(f"app.services.email.{name}", lambda *a, **k: True)
    monkeypatch.setattr("app.api.v1.orders.send_order_confirmation_email", lambda *a, **k: True)
    yield
    StripePaymentProvider.reset_mock_state()


def _erase(client, token):
    return client.post(
        f"{LEGAL}/data-request",
        json={"request_type": "deletion", "password": PASSWORD},
        headers=_auth_headers(token),
    )


def test_erasure_cancels_an_unpaid_order_and_expires_its_checkout_session(
    client, db_session, buyer_quiet
):
    token, order, product, _ = _place(client, db_session, quantity=3)
    opened = client.post(
        f"{ORDERS_PREFIX}/{order['id']}/checkout-session",
        headers=_with_idempotency_key(_auth_headers(token)),
    )
    assert opened.status_code == 200, opened.text
    session_id = opened.json()["checkout_session_id"]
    assert client.get(f"/api/v1/products/{product.id}").json()["stock_quantity"] == 7  # cached

    assert _erase(client, token).status_code == 200

    stored = _order(db_session, order["id"])
    assert (stored.status, stored.payment_status) == ("cancelled", "pending")
    assert _stock(db_session, product) == 10
    assert client.get(f"/api/v1/products/{product.id}").json()["stock_quantity"] == 10
    session = StripePaymentProvider().retrieve_checkout_session(session_id)
    assert session.status == CheckoutSessionStatus.EXPIRED


def test_erasure_leaves_a_paid_order_alone(client, db_session, buyer_quiet):
    token, order, product, _ = _place(client, db_session, quantity=3)
    assert _pay(client, token, order).json()["status"] == "paid"

    assert _erase(client, token).status_code == 200

    stored = _order(db_session, order["id"])
    assert (stored.status, stored.payment_status) == ("confirmed", "paid")
    assert _stock(db_session, product) == 7
