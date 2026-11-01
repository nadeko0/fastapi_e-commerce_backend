import uuid

import pytest
from payment_support import Shop, reset_provider

from app.models.order import Order
from app.models.payment import Payment
from app.models.payment_ledger import PaymentDispute, PaymentRefund
from app.services.payment.exceptions import CardError, PaymentProviderTimeoutError
from app.services.payment.stripe_provider import StripePaymentProvider

API = "/api/v1"
ADMIN = f"{API}/admin"


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


def _setup_cancelled_order_with_flagged_payment(shop, amount=2500, flagged=True):
    buyer = shop.buyer()
    product = shop.product(price="25.00", stock=10)
    order = shop.order_of(buyer, product)
    order_id = order["id"]

    order_row = shop.db.get(Order, order_id)
    order_row.status = "cancelled"

    payment_intent_id = f"pi_test_{uuid.uuid4().hex[:20]}"
    payment = Payment(
        order_id=order_id,
        payment_intent_id=payment_intent_id,
        amount=amount,
        amount_refunded=0,
        currency="USD",
        status="succeeded",
        requires_manual_review=flagged,
    )
    shop.db.add(payment)
    shop.db.commit()
    shop.db.refresh(payment)

    # Populate mock provider's intent state so create_refund succeeds
    from app.services.payment.types import PaymentIntent, PaymentIntentStatus
    provider_intent = PaymentIntent(
        id=payment_intent_id,
        amount=amount,
        currency="USD",
        status=PaymentIntentStatus.SUCCEEDED,
        client_secret=f"{payment_intent_id}_secret",
    )
    StripePaymentProvider._intents_by_id[payment_intent_id] = provider_intent
    return order_id, payment


def test_admin_payments_guard(client, db_session):
    shop = Shop(client, db_session)
    buyer = shop.buyer()

    # Anonymous
    assert client.get(f"{ADMIN}/payments").status_code == 401
    assert client.post(f"{ADMIN}/payments/1/refund", headers={"Idempotency-Key": "test-key-12345"}).status_code == 401

    # Non-admin
    assert client.get(f"{ADMIN}/payments", headers=buyer.headers).status_code == 403
    assert client.post(f"{ADMIN}/payments/1/refund", headers={**buyer.headers, "Idempotency-Key": "test-key-12345"}).status_code == 403


def test_list_payments_filtering(client, db_session):
    shop = Shop(client, db_session)
    admin = shop.admin()
    _, p1 = _setup_cancelled_order_with_flagged_payment(shop, amount=1000, flagged=True)
    _, p2 = _setup_cancelled_order_with_flagged_payment(shop, amount=2000, flagged=False)

    flagged_res = client.get(f"{ADMIN}/payments?requires_manual_review=true", headers=admin)
    assert flagged_res.status_code == 200
    flagged_ids = [item["id"] for item in flagged_res.json()["items"]]
    assert p1.id in flagged_ids
    assert p2.id not in flagged_ids

    unflagged_res = client.get(f"{ADMIN}/payments?requires_manual_review=false", headers=admin)
    assert unflagged_res.status_code == 200
    unflagged_ids = [item["id"] for item in unflagged_res.json()["items"]]
    assert p2.id in unflagged_ids
    assert p1.id not in unflagged_ids


def test_refund_flagged_payment_happy_path(client, db_session):
    shop = Shop(client, db_session)
    admin = shop.admin()
    order_id, payment = _setup_cancelled_order_with_flagged_payment(shop, amount=3000, flagged=True)

    key = "refund-flagged-key-1"
    response = client.post(
        f"{ADMIN}/payments/{payment.id}/refund",
        headers={**admin, "Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["amount"] == "30.00"
    assert body["status"] == "succeeded"

    shop.db.expire_all()
    updated_payment = shop.db.get(Payment, payment.id)
    assert updated_payment.amount_refunded == 3000
    assert updated_payment.requires_manual_review is False

    order = shop.db.get(Order, order_id)
    assert order.status == "cancelled"

    refund_rows = shop.db.query(PaymentRefund).filter(PaymentRefund.payment_id == payment.id).all()
    assert len(refund_rows) == 1
    assert refund_rows[0].source == "admin"
    assert refund_rows[0].amount == 3000


def test_a_retry_of_a_completed_refund_moves_no_money(client, db_session, monkeypatch):
    shop = Shop(client, db_session)
    admin = shop.admin()
    _, payment = _setup_cancelled_order_with_flagged_payment(shop, amount=2500, flagged=True)

    key = "refund-replay-key-99"
    first = client.post(
        f"{ADMIN}/payments/{payment.id}/refund",
        headers={**admin, "Idempotency-Key": key},
    )
    assert first.status_code == 200

    def no_second_call(*args, **kwargs):
        raise AssertionError("a retry must not reach the provider")

    monkeypatch.setattr(StripePaymentProvider, "create_refund", no_second_call)
    second = client.post(
        f"{ADMIN}/payments/{payment.id}/refund",
        headers={**admin, "Idempotency-Key": key},
    )
    assert second.status_code == 409

    shop.db.expire_all()
    refund_rows = shop.db.query(PaymentRefund).filter(PaymentRefund.payment_id == payment.id).all()
    assert len(refund_rows) == 1


def test_refund_unflagged_payment_refused(client, db_session):
    shop = Shop(client, db_session)
    admin = shop.admin()
    _, payment = _setup_cancelled_order_with_flagged_payment(shop, amount=2500, flagged=False)

    response = client.post(
        f"{ADMIN}/payments/{payment.id}/refund",
        headers={**admin, "Idempotency-Key": "refund-unflagged-key-1"},
    )
    assert response.status_code == 409
    assert "Only a succeeded payment flagged for manual review can be refunded" in response.text


def test_refund_flagged_payment_provider_failure(client, db_session, monkeypatch):
    shop = Shop(client, db_session)
    admin = shop.admin()
    _, payment = _setup_cancelled_order_with_flagged_payment(shop, amount=2500, flagged=True)

    def failing_refund(self, **kwargs):
        raise PaymentProviderTimeoutError("Connection dropped")

    monkeypatch.setattr(StripePaymentProvider, "create_refund", failing_refund)

    response = client.post(
        f"{ADMIN}/payments/{payment.id}/refund",
        headers={**admin, "Idempotency-Key": "prov-fail-key"},
    )
    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "5"

    shop.db.expire_all()
    p = shop.db.get(Payment, payment.id)
    assert p.requires_manual_review is True
    assert p.amount_refunded == 0


def test_a_flagged_payment_under_an_open_dispute_is_not_sent_to_the_provider(
    client, db_session, monkeypatch
):
    shop = Shop(client, db_session)
    admin = shop.admin()
    _, payment = _setup_cancelled_order_with_flagged_payment(shop, amount=2500, flagged=True)
    shop.db.add(PaymentDispute(
        dispute_id=f"dp_{uuid.uuid4().hex}", payment_id=payment.id, amount=2500,
        currency=payment.currency, reason="fraudulent", status="needs_response",
    ))
    shop.db.commit()

    def no_call(*args, **kwargs):
        raise AssertionError("a disputed charge must not reach the provider")

    monkeypatch.setattr(StripePaymentProvider, "create_refund", no_call)
    response = client.post(
        f"{ADMIN}/payments/{payment.id}/refund",
        headers={**admin, "Idempotency-Key": "disputed-key-1"},
    )
    assert response.status_code == 409, response.text
    shop.db.expire_all()
    assert shop.db.get(Payment, payment.id).requires_manual_review is True


@pytest.mark.parametrize("code", ["charge_disputed", "refund_disputed_payment"])
def test_a_dispute_only_stripe_knows_about_is_a_409(client, db_session, monkeypatch, code):
    shop = Shop(client, db_session)
    admin = shop.admin()
    _, payment = _setup_cancelled_order_with_flagged_payment(shop, amount=2500, flagged=True)

    def refused(self, **kwargs):
        raise CardError("Charge has been charged back; cannot issue a refund.", code=code)

    monkeypatch.setattr(StripePaymentProvider, "create_refund", refused)
    response = client.post(
        f"{ADMIN}/payments/{payment.id}/refund",
        headers={**admin, "Idempotency-Key": f"disputed-{code}"},
    )
    assert response.status_code == 409, response.text


def test_another_provider_refusal_stays_a_400(client, db_session, monkeypatch):
    shop = Shop(client, db_session)
    admin = shop.admin()
    _, payment = _setup_cancelled_order_with_flagged_payment(shop, amount=2500, flagged=True)

    def refused(self, **kwargs):
        raise CardError("Refused.", code="charge_already_refunded")

    monkeypatch.setattr(StripePaymentProvider, "create_refund", refused)
    response = client.post(
        f"{ADMIN}/payments/{payment.id}/refund",
        headers={**admin, "Idempotency-Key": "refused-key-1"},
    )
    assert response.status_code == 400, response.text
