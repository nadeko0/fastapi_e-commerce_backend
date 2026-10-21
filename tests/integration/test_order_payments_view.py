"""
GET /orders/{id}/payments (app/api/v1/orders.py): what an admin needs to
reconcile an order - every payment attempt with its refunds and disputes,
and which of them are flagged for manual review (a second charge, a
success for a cancelled order, a refund that failed after it was
recorded). Nothing else in the API shows these.
"""
import pytest
from payment_support import PAY_DECLINED, Shop, reset_provider, use_production_sessions


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def shop(client, db_session, monkeypatch):
    use_production_sessions(monkeypatch)
    return Shop(client, db_session)


def test_an_admin_sees_every_attempt_with_its_refunds_and_disputes(shop):
    product = shop.product(price="19.99", stock=4)
    buyer = shop.buyer()
    admin = shop.admin()
    order = shop.order_of(buyer, product)
    assert shop.pay(buyer, order["id"], token=PAY_DECLINED).status_code == 402
    assert shop.pay(buyer, order["id"]).status_code == 200
    paying = shop.payments(order["id"])[1]
    refund_id = shop.refund(admin, order["id"], amount="5.00").json()["data"]["id"]
    shop.webhook("refund.failed", {"id": refund_id, "object": "refund", "amount": 500,
                                   "currency": "usd", "payment_intent": paying.payment_intent_id,
                                   "status": "failed", "failure_reason": "lost_or_stolen_card"})
    shop.webhook("charge.dispute.created", {
        "id": "du_view", "object": "dispute", "amount": 1999, "currency": "usd",
        "payment_intent": paying.payment_intent_id, "status": "needs_response",
        "reason": "product_not_received"})

    response = shop.client.get(f"/api/v1/orders/{order['id']}/payments", headers=admin)

    assert response.status_code == 200, response.text
    declined, paid = response.json()["data"]
    assert (declined["status"], declined["amount"], declined["refunds"], declined["disputes"]) == (
        "failed", "19.99", [], [])
    assert (paid["status"], paid["amount"], paid["amount_refunded"], paid["currency"]) == (
        "succeeded", "19.99", "0.00", "USD")
    assert paid["transaction_id"] == paying.payment_intent_id
    assert paid["requires_manual_review"] is False
    [refund] = paid["refunds"]
    assert {k: refund[k] for k in ("id", "amount", "status", "source", "failure_reason",
                                   "requires_manual_review")} == {
        "id": refund_id, "amount": "5.00", "status": "failed", "source": "admin",
        "failure_reason": "lost_or_stolen_card", "requires_manual_review": True}
    [dispute] = paid["disputes"]
    assert {k: dispute[k] for k in ("id", "amount", "status", "reason", "closed_at")} == {
        "id": "du_view", "amount": "19.99", "status": "needs_response",
        "reason": "product_not_received", "closed_at": None}


def test_the_payments_view_is_for_admins_only(shop):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)

    assert shop.client.get(f"/api/v1/orders/{order['id']}/payments",
                           headers=buyer.headers).status_code == 403
    assert shop.client.get(f"/api/v1/orders/{order['id']}/payments").status_code == 401
    assert shop.client.get("/api/v1/orders/999999/payments",
                           headers=shop.admin()).status_code == 404
