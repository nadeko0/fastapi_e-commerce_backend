"""
Tests for the Stripe Checkout Session (hosted-redirect) payment flow: the
mock provider's create_checkout_session/retrieve_checkout_session/
expire_checkout_session (app/services/payment/stripe_provider.py), the
POST /orders/{id}/checkout-session endpoint, and the checkout.session.*
branches of the webhook handler in app/api/v1/orders.py.

This flow is additional to, and does not replace, the existing
PaymentIntent-based /pay flow covered by test_payment.py - that suite is
untouched. Fixtures/helpers are reused from test_payment.py rather than
duplicated, since tests/ has no __init__.py (pytest's default "prepend"
import mode puts it on sys.path - see test_payment.py's own comment on
`from conftest import TestingSessionLocal` for the same mechanism).
"""
import json
import time
import uuid

import pytest
from test_payment import (
    ORDERS_PREFIX,
    TOKEN_SUCCESS_DEFAULT,
    _create_admin,
    _create_verified_buyer_with_order,
    _get_order,
    _get_payments,
    _payment_payload,
    _post_webhook,
    _sign,
)

from app.services.payment.exceptions import (
    CardError,
    IdempotencyError,
    InvalidRequestError,
)
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.payment.types import CheckoutSessionStatus


def CHECKOUT_SESSION_URL(order_id):
    return f"{ORDERS_PREFIX}/{order_id}/checkout-session"


# test_payment.py's autouse fixtures (mock-state reset, email-bug stub) are
# module-scoped by pytest - they do not apply across module boundaries, so
# this file needs its own copies rather than relying on the import above.


@pytest.fixture(autouse=True)
def _reset_stripe_mock_state():
    StripePaymentProvider.reset_mock_state()
    yield
    StripePaymentProvider.reset_mock_state()


@pytest.fixture(autouse=True)
def _stub_order_confirmation_email(monkeypatch):
    monkeypatch.setattr("app.api.v1.orders.send_order_confirmation_email", lambda *a, **k: True)
    yield


def _checkout_webhook_body(
    event_type: str,
    session_id: str,
    payment_intent_id: str = None,
    payment_status: str = "paid",
    event_id: str = None,
) -> bytes:
    event_id = event_id or f"evt_{uuid.uuid4().hex[:24]}"
    obj = {"id": session_id, "payment_status": payment_status}
    if payment_intent_id is not None:
        obj["payment_intent"] = payment_intent_id
    return json.dumps(
        {
            "id": event_id,
            "type": event_type,
            "created": int(time.time()),
            "data": {"object": obj},
        }
    ).encode()


# -- Provider-level (mock) behavior ------------------------------------------


class TestStripePaymentProviderCheckoutSession:
    def test_create_checkout_session_returns_open_session_with_url(self):
        provider = StripePaymentProvider()
        session = provider.create_checkout_session(
            amount=1999,
            currency="USD",
            idempotency_key=str(uuid.uuid4()),
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )
        assert session.id.startswith("cs_test_")
        assert session.url and session.id in session.url
        assert session.status == CheckoutSessionStatus.OPEN
        assert session.payment_status == "unpaid"
        assert session.amount_total == 1999
        assert session.payment_intent is None

    def test_create_checkout_session_requires_success_and_cancel_url(self):
        provider = StripePaymentProvider()
        with pytest.raises(InvalidRequestError):
            provider.create_checkout_session(
                amount=500,
                currency="USD",
                idempotency_key=str(uuid.uuid4()),
                success_url="",
                cancel_url="https://example.com/cancel",
            )

    def test_reused_idempotency_key_with_same_params_returns_same_session(self):
        provider = StripePaymentProvider()
        key = str(uuid.uuid4())
        first = provider.create_checkout_session(
            amount=800,
            currency="USD",
            idempotency_key=key,
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )
        second = provider.create_checkout_session(
            amount=800,
            currency="USD",
            idempotency_key=key,
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )
        assert first.id == second.id

    def test_reused_idempotency_key_with_different_amount_raises(self):
        provider = StripePaymentProvider()
        key = str(uuid.uuid4())
        provider.create_checkout_session(
            amount=800,
            currency="USD",
            idempotency_key=key,
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )
        with pytest.raises(IdempotencyError):
            provider.create_checkout_session(
                amount=900,
                currency="USD",
                idempotency_key=key,
                success_url="https://example.com/success",
                cancel_url="https://example.com/cancel",
            )

    def test_retrieve_unknown_session_raises_card_error(self):
        provider = StripePaymentProvider()
        with pytest.raises(CardError) as exc_info:
            provider.retrieve_checkout_session("cs_test_does_not_exist")
        assert exc_info.value.code == "resource_missing"

    def test_expire_open_session_marks_it_expired(self):
        provider = StripePaymentProvider()
        session = provider.create_checkout_session(
            amount=500,
            currency="USD",
            idempotency_key=str(uuid.uuid4()),
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )
        expired = provider.expire_checkout_session(session.id)
        assert expired.status == CheckoutSessionStatus.EXPIRED
        assert provider.retrieve_checkout_session(session.id).status == CheckoutSessionStatus.EXPIRED

    def test_expiring_an_already_expired_session_raises(self):
        provider = StripePaymentProvider()
        session = provider.create_checkout_session(
            amount=500,
            currency="USD",
            idempotency_key=str(uuid.uuid4()),
            success_url="https://example.com/success",
            cancel_url="https://example.com/cancel",
        )
        provider.expire_checkout_session(session.id)
        with pytest.raises(CardError):
            provider.expire_checkout_session(session.id)


# -- POST /orders/{id}/checkout-session --------------------------------------


class TestCreateCheckoutSessionEndpoint:
    def test_creates_session_for_valid_order(self, client, db_session):
        setup = _create_verified_buyer_with_order(client, db_session)
        order = setup["order"]

        response = client.post(CHECKOUT_SESSION_URL(order["id"]), headers=setup["headers"])

        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert body["order_id"] == order["id"]
        assert body["checkout_session_id"].startswith("cs_test_")
        assert body["url"]
        assert body["status"] == "pending"
        assert float(body["amount"]) == float(order["total_amount"])

        payments = _get_payments(db_session, order["id"])
        assert len(payments) == 1
        assert payments[0].checkout_session_id == body["checkout_session_id"]
        assert payments[0].checkout_session_url == body["url"]
        # Not yet a real PaymentIntent - a fresh Checkout Session has none
        # until the customer completes the hosted page (see
        # types.py's CheckoutSession docstring).
        assert payments[0].payment_intent_id.startswith("pending-")

    def test_rejects_already_paid_order(self, client, db_session):
        setup = _create_verified_buyer_with_order(client, db_session)
        order = setup["order"]
        pay_response = client.post(
            f"{ORDERS_PREFIX}/{order['id']}/pay",
            json=_payment_payload(order, token=TOKEN_SUCCESS_DEFAULT),
            headers=setup["headers"],
        )
        assert pay_response.json()["data"]["status"] == "paid"

        response = client.post(CHECKOUT_SESSION_URL(order["id"]), headers=setup["headers"])
        assert response.status_code == 400
        assert "already paid" in response.json()["detail"].lower()

    def test_rejects_cancelled_order(self, client, db_session):
        setup = _create_verified_buyer_with_order(client, db_session)
        order = setup["order"]
        admin = _create_admin(db_session)

        cancel_response = client.put(
            f"{ORDERS_PREFIX}/{order['id']}/status",
            params={"status": "cancelled"},
            headers=admin["headers"],
        )
        assert cancel_response.status_code == 200

        response = client.post(CHECKOUT_SESSION_URL(order["id"]), headers=setup["headers"])
        assert response.status_code == 400
        assert "cancelled" in response.json()["detail"].lower()

    def test_unknown_order_returns_404(self, client, db_session):
        setup = _create_verified_buyer_with_order(client, db_session)
        response = client.post(CHECKOUT_SESSION_URL(999999), headers=setup["headers"])
        assert response.status_code == 404

    def test_idempotency_key_retry_returns_original_session(self, client, db_session):
        # Regression test for: a retried checkout-session request (client
        # timeout + retry, double click) with the same Idempotency-Key must
        # not create a second Checkout Session for the same order.
        setup = _create_verified_buyer_with_order(client, db_session)
        order = setup["order"]
        headers = {**setup["headers"], "Idempotency-Key": "checkout-session-1"}

        first = client.post(CHECKOUT_SESSION_URL(order["id"]), headers=headers)
        second = client.post(CHECKOUT_SESSION_URL(order["id"]), headers=headers)

        assert first.status_code == 200
        assert second.status_code == 200
        assert (
            first.json()["data"]["checkout_session_id"]
            == second.json()["data"]["checkout_session_id"]
        )

        payments = _get_payments(db_session, order["id"])
        assert len(payments) == 1

    @pytest.mark.parametrize("field", ["success_url", "cancel_url"])
    @pytest.mark.parametrize(
        "bad_url",
        [
            "javascript:alert(document.cookie)",
            "data:text/html,<script>alert(1)</script>",
            "ftp://example.com/whatever",
            "not-a-url-at-all",
        ],
    )
    def test_rejects_non_http_redirect_urls(self, client, db_session, field, bad_url):
        # success_url/cancel_url are client-supplied redirect targets Stripe
        # sends the customer's browser to after a real payment - a
        # non-http(s) scheme here has no legitimate use and must be rejected
        # up front (422) rather than passed through to the payment provider.
        setup = _create_verified_buyer_with_order(client, db_session)
        order = setup["order"]

        payload = {
            "success_url": "https://example.com/success",
            "cancel_url": "https://example.com/cancel",
        }
        payload[field] = bad_url

        response = client.post(
            CHECKOUT_SESSION_URL(order["id"]), json=payload, headers=setup["headers"]
        )

        assert response.status_code == 422


# -- Webhook: checkout.session.* ----------------------------------------------


class TestCheckoutSessionWebhook:
    def _create_session(self, client, headers, order):
        response = client.post(CHECKOUT_SESSION_URL(order["id"]), headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def test_completed_event_marks_order_paid(self, client, db_session):
        setup = _create_verified_buyer_with_order(client, db_session)
        order = setup["order"]
        session = self._create_session(client, setup["headers"], order)
        pi_id = f"pi_{uuid.uuid4().hex[:24]}"

        body = _checkout_webhook_body(
            "checkout.session.completed", session["checkout_session_id"], payment_intent_id=pi_id
        )
        response = _post_webhook(client, body, _sign(body))

        assert response.status_code == 200
        assert response.json()["data"]["applied"] is True

        db_order = _get_order(db_session, order["id"])
        assert db_order.payment_status == "paid"
        assert db_order.status == "confirmed"

        payments = _get_payments(db_session, order["id"])
        assert payments[0].status == "succeeded"
        # Backfilled from the session's payment_intent field, replacing the
        # "pending-..." placeholder set at session-creation time.
        assert payments[0].payment_intent_id == pi_id

    def test_completed_event_with_unpaid_status_is_not_applied(self, client, db_session):
        # Delayed/async payment methods can report checkout.session.completed
        # with payment_status="unpaid" - the real outcome arrives later via
        # checkout.session.async_payment_succeeded/_failed. Must not be
        # treated as a success.
        setup = _create_verified_buyer_with_order(client, db_session)
        order = setup["order"]
        session = self._create_session(client, setup["headers"], order)

        body = _checkout_webhook_body(
            "checkout.session.completed",
            session["checkout_session_id"],
            payment_status="unpaid",
        )
        response = _post_webhook(client, body, _sign(body))

        assert response.status_code == 200
        assert response.json()["data"]["applied"] is False

        db_order = _get_order(db_session, order["id"])
        assert db_order.payment_status == "pending"

    def test_async_payment_succeeded_marks_order_paid(self, client, db_session):
        setup = _create_verified_buyer_with_order(client, db_session)
        order = setup["order"]
        session = self._create_session(client, setup["headers"], order)
        pi_id = f"pi_{uuid.uuid4().hex[:24]}"

        body = _checkout_webhook_body(
            "checkout.session.async_payment_succeeded",
            session["checkout_session_id"],
            payment_intent_id=pi_id,
        )
        response = _post_webhook(client, body, _sign(body))

        assert response.status_code == 200
        assert response.json()["data"]["applied"] is True
        db_order = _get_order(db_session, order["id"])
        assert db_order.payment_status == "paid"

    def test_duplicate_completed_event_does_not_double_apply(self, client, db_session):
        setup = _create_verified_buyer_with_order(client, db_session)
        order = setup["order"]
        session = self._create_session(client, setup["headers"], order)
        event_id = f"evt_{uuid.uuid4().hex[:24]}"

        body = _checkout_webhook_body(
            "checkout.session.completed",
            session["checkout_session_id"],
            payment_intent_id=f"pi_{uuid.uuid4().hex[:24]}",
            event_id=event_id,
        )
        signature = _sign(body)

        first = _post_webhook(client, body, signature)
        second = _post_webhook(client, body, signature)

        assert first.status_code == 200
        assert first.json()["data"]["applied"] is True
        assert second.status_code == 200
        assert second.json()["data"]["status"] == "already_processed"

    def test_expired_event_marks_payment_failed(self, client, db_session):
        setup = _create_verified_buyer_with_order(client, db_session)
        order = setup["order"]
        session = self._create_session(client, setup["headers"], order)

        body = _checkout_webhook_body(
            "checkout.session.expired", session["checkout_session_id"], payment_status="unpaid"
        )
        response = _post_webhook(client, body, _sign(body))

        assert response.status_code == 200
        assert response.json()["data"]["applied"] is True

        db_order = _get_order(db_session, order["id"])
        assert db_order.payment_status == "failed"

        payments = _get_payments(db_session, order["id"])
        assert payments[0].status == "failed"

    def test_async_payment_failed_marks_payment_failed(self, client, db_session):
        setup = _create_verified_buyer_with_order(client, db_session)
        order = setup["order"]
        session = self._create_session(client, setup["headers"], order)

        body = _checkout_webhook_body(
            "checkout.session.async_payment_failed",
            session["checkout_session_id"],
            payment_status="unpaid",
        )
        response = _post_webhook(client, body, _sign(body))

        assert response.status_code == 200
        db_order = _get_order(db_session, order["id"])
        assert db_order.payment_status == "failed"

    def test_completed_for_cancelled_order_is_flagged_not_applied(self, client, db_session):
        # Mirrors test_webhook_success_for_cancelled_order_is_flagged_not_applied
        # in test_payment.py for the PaymentIntent flow: the order was
        # cancelled after the Checkout Session was created, and Stripe is
        # now reporting it completed - a genuine race, not a bug. Must not
        # silently mark a cancelled order as paid.
        setup = _create_verified_buyer_with_order(client, db_session)
        order = setup["order"]
        session = self._create_session(client, setup["headers"], order)
        admin = _create_admin(db_session)

        cancel_response = client.put(
            f"{ORDERS_PREFIX}/{order['id']}/status",
            params={"status": "cancelled"},
            headers=admin["headers"],
        )
        assert cancel_response.status_code == 200

        body = _checkout_webhook_body(
            "checkout.session.completed",
            session["checkout_session_id"],
            payment_intent_id=f"pi_{uuid.uuid4().hex[:24]}",
        )
        response = _post_webhook(client, body, _sign(body))

        assert response.status_code == 200
        assert response.json()["data"]["applied"] is False
        assert response.json()["data"]["requires_manual_review"] is True

        db_order = _get_order(db_session, order["id"])
        assert db_order.status == "cancelled"
        assert db_order.payment_status == "pending"

        payments = _get_payments(db_session, order["id"])
        assert payments[0].requires_manual_review is True

    def test_event_for_unknown_session_is_acknowledged_but_not_applied(self, client, db_session):
        body = _checkout_webhook_body(
            "checkout.session.completed", "cs_test_totally_unknown_to_us"
        )
        response = _post_webhook(client, body, _sign(body))
        assert response.status_code == 200
        assert response.json()["data"]["applied"] is False
