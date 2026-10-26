"""Validator repros for 2A-01/2B-03, 2A-02, 2A-03. Each asserts the FAULTY behaviour."""
from datetime import timedelta

from payment_support import PAY_3DS, PAY_OK, Shop, order_state, reset_provider

from app.core.clock import utc_now
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.reconciliation import reconcile_payments


def _setup(client, db_session):
    reset_provider()
    shop = Shop(client, db_session)
    p = shop.product(stock=1)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, p).status_code == 200
    r = shop.place_order(buyer)
    assert r.status_code == 201, r.text
    return shop, p, buyer, r.json()


def test_v_2a01_late_3ds_charge_on_expired_order_unrefundable_and_unreported(client, db_session, tasks_db):
    shop, p, buyer, order = _setup(client, db_session)
    assert shop.pay(buyer, order["id"], token=PAY_3DS).status_code == 200
    assert shop.expire_unpaid(order["id"]) == 1          # in-flight attempt ignored
    assert shop.stock(p) == 1                           # restocked, resellable
    late = shop.intent_event("payment_intent.succeeded", shop.payments(order["id"])[0])
    assert late.json()["requires_manual_review"] is True
    assert order_state(shop, order["id"]) == ("cancelled", "pending")
    admin = shop.admin()
    assert shop.refund(admin, order["id"]).status_code == 409   # no in-app refund path
    # Real Stripe now reports the intent succeeded: mirror that in the mock.
    import dataclasses
    from app.services.payment.types import PaymentIntentStatus
    pid = shop.payments(order["id"])[0].payment_intent_id
    cur = StripePaymentProvider._intents_by_id[pid]
    StripePaymentProvider._intents_by_id[pid] = (dataclasses.replace(cur, status=PaymentIntentStatus.SUCCEEDED, amount_received=cur.amount)
        if dataclasses.is_dataclass(cur) else cur.model_copy(update={"status": PaymentIntentStatus.SUCCEEDED, "amount_received": cur.amount}))
    res = reconcile_payments(db_session, StripePaymentProvider(), now=utc_now() + timedelta(hours=2))
    assert res.opened == 0                                       # reconciliation silent


def test_v_2a02_checkout_session_without_webhook_skipped_by_reconciliation(client, db_session):
    shop, p, buyer, order = _setup(client, db_session)
    r = shop.checkout_session(buyer, order["id"])
    assert r.status_code in (200, 201), r.text
    pay = shop.payments(order["id"])[0]
    assert pay.payment_intent_id.startswith("pending-cs-")
    res = reconcile_payments(db_session, StripePaymentProvider(), now=utc_now() + timedelta(hours=2))
    assert res.checked == 0 and res.skipped == 1


def test_v_2a03_stale_pending_refund_event_regresses_refunded_order(client, db_session):
    shop, p, buyer, order = _setup(client, db_session)
    assert shop.pay(buyer, order["id"], token=PAY_OK).status_code == 200
    admin = shop.admin()
    ref = shop.refund(admin, order["id"])
    assert ref.status_code == 200, ref.text
    assert order_state(shop, order["id"])[1] == "refunded"
    pay = shop.payments(order["id"])[0]
    shop.webhook("refund.created", {
        "id": ref.json()["id"], "object": "refund", "status": "pending",
        "amount": pay.amount, "payment_intent": pay.payment_intent_id, "currency": pay.currency.lower(),
    })
    assert order_state(shop, order["id"])[1] == "paid"     # refunded -> paid
    assert shop.set_status(admin, order["id"], "processing").status_code == 200
