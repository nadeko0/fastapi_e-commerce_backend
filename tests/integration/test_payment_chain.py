"""
The whole customer journey, end to end over the HTTP API on the mock
payment provider, from "a person clicks buy" to "money back":

  guest cart -> sign up, verify, log in (the guest cart is merged) ->
  order from the cart -> payment (/pay: declined, 3D Secure, succeeded;
  or a Stripe Checkout Session) -> the customer following the order ->
  an admin fulfilling it -> refunds -> cancellations and expiry

After every step each scenario checks the order as stored and as the
customer sees it (status, payment_status, total in Decimal and in cents,
currency, lines), every Payment row (status, amount, amount_refunded,
currency, manual-review flag), stock, what the provider was asked for
(amount in cents, currency) and which emails were queued.

The main scenarios run in several two-decimal store currencies
(STORE_CURRENCY = USD, EUR, GBP, PLN): an order is priced, charged,
reported and refunded in the store's currency, never another.

Rules the scenarios rely on, as the code states them (app/services/
orders.py, app/api/v1/orders.py, app/tasks.py): stock is taken when the
order is placed and returned only when it is cancelled (by its customer,
by an admin, or by the unpaid-order expiry), never by a refund; a payment
moves a new order to confirmed; an admin moves it confirmed -> processing
-> sent -> delivered, one step at a time, or cancels it before delivery;
only a paid order is refunded, and it becomes refunded once its refunds
cover the charge.
"""
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from payment_support import (
    CART,
    ORDERS,
    PAY_3DS,
    PAY_DECLINED,
    USERS,
    Buyer,
    Outbox,
    Shop,
    cents,
    new_key,
    order_state,
    payment_rows,
    reset_provider,
)

from app.core.config import settings
from app.main import app
from app.services.payment.stripe_provider import StripePaymentProvider

CURRENCIES = ["USD", "EUR", "GBP", "PLN"]


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture(params=CURRENCIES)
def currency(request, monkeypatch):
    monkeypatch.setattr(settings, "STORE_CURRENCY", request.param)
    return request.param


@pytest.fixture
def outbox(monkeypatch):
    return Outbox(monkeypatch)


@pytest.fixture
def shop(client, db_session, monkeypatch):
    return Shop(client, db_session)


@pytest.fixture
def provider_calls(monkeypatch):
    """What the app asked the provider for: (method, amount, currency)."""
    calls = []
    for name in ("create_payment_intent", "create_checkout_session", "create_refund"):
        real = getattr(StripePaymentProvider, name)

        def _spy(self, *args, _real=real, _name=name, **kwargs):
            calls.append((_name, kwargs.get("amount"), kwargs.get("currency")))
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(StripePaymentProvider, name, _spy)
    return calls


def _customer_sees(shop, buyer, order_id):
    """The order as its customer reads it, one by id and in the list,
    which must agree."""
    one = shop.get_order(buyer, order_id)
    assert one.status_code == 200, one.text
    data = one.json()["data"]
    listed = [o for o in shop.list_orders(buyer).json()["data"] if o["id"] == order_id]
    assert len(listed) == 1
    for field in ("status", "payment_status", "total_amount", "currency"):
        assert listed[0][field] == data[field]
    return {
        "status": data["status"],
        "payment_status": data["payment_status"],
        "total": Decimal(data["total_amount"]),
        "currency": data["currency"],
        "lines": sorted((i["product_id"], i["variant_id"], i["quantity"],
                         Decimal(i["price_at_time"])) for i in data["items"]),
    }


def _emails(outbox):
    return [(kind, status, payment_status) for kind, _, _, status, payment_status in outbox.take()]


def _catalog(shop):
    widget = shop.product(price="12.50", stock=10, name="Widget")
    shirt = shop.product(price="9.00", stock=8, name="Shirt")
    shirt_xl = shop.variant(shirt, price_override="7.25", stock=5)
    return widget, shirt, shirt_xl


def _ready_buyer(shop, catalog):
    """A verified buyer whose order holds 2 widgets and 1 shirt in XL
    (2 x 12.50 + 7.25 = 32.25)."""
    widget, shirt, shirt_xl = catalog
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, widget, 2).status_code == 200
    assert shop.add_to_cart(buyer, shirt, 1, variant=shirt_xl).status_code == 200
    response = shop.place_order(buyer)
    assert response.status_code == 200, response.text
    return buyer, response.json()["data"]


# -- The main journey ------------------------------------------------------------

def test_guest_to_refund_journey(client, shop, outbox, provider_calls, currency, monkeypatch):
    widget, shirt, shirt_xl = _catalog(shop)
    admin = shop.admin()

    # 1. A guest fills a cart (over https: the cart cookie is Secure).
    visitor = TestClient(app, base_url="https://testserver")
    guest = Shop(visitor, shop.db)
    add = visitor.post(f"{CART}/items", json={"product_id": widget.id, "quantity": 2})
    assert add.status_code == 200, add.text
    add = visitor.post(f"{CART}/items", json={"product_id": shirt.id, "quantity": 1,
                                                 "variant_id": shirt_xl.id})
    assert add.status_code == 200
    cart = add.json()["data"]
    assert (Decimal(cart["total_amount"]), cart["items_count"]) == (Decimal("32.25"), 2)
    # Cart validation: no more than the stock, nothing that is not for sale.
    too_many = visitor.post(f"{CART}/items", json={"product_id": widget.id, "quantity": 9})
    assert too_many.status_code == 400
    assert visitor.post(f"{CART}/items", json={"product_id": 999999}).status_code == 404
    assert shop.stock(widget) == 10  # a cart reserves nothing

    # 2. Signs up, verifies the address, logs in: the guest cart is merged.
    tokens = []
    monkeypatch.setattr("app.api.v1.users.send_welcome_email",
                        lambda email, name, token: tokens.append(token) or True)
    email = f"journey-{uuid.uuid4().hex[:8]}@example.com"
    registered = visitor.post(f"{USERS}/register", json={
        "email": email, "password": "Str0ngPass1", "full_name": "Journey Buyer",
        "gdpr_consent": True, "privacy_policy_accepted": True, "marketing_consent": False})
    assert registered.status_code == 200, registered.text
    login = visitor.post(f"{USERS}/login", data={"username": email, "password": "Str0ngPass1"})
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
    merged = visitor.get(CART, headers=headers).json()["data"]
    assert sorted((i["product_id"], i["variant_id"], i["quantity"]) for i in merged["items"]) == [
        (widget.id, None, 2), (shirt.id, shirt_xl.id, 1)]
    address = visitor.post(f"{USERS}/addresses", headers=headers, json={
        "street": "1 Journey Street", "city": "Metropolis", "state": "NY",
        "postal_code": "10001", "country": "US"})
    assert address.status_code == 200, address.text
    buyer = Buyer(headers, 0, email, address.json()["data"]["id"])
    # Not yet: an order needs a verified address and a complete profile.
    assert guest.place_order(buyer).status_code == 403
    assert visitor.post(f"{USERS}/verify-email", json={"token": tokens[0]}).status_code == 200
    assert guest.place_order(buyer).status_code == 400  # no phone yet
    assert visitor.put(f"{USERS}/me", headers=headers, json={"phone": "+48123456789"}).status_code == 200

    # 3. Places the order from the cart (retried with the same key).
    key = new_key()
    placed = guest.place_order(buyer, key=key)
    assert placed.status_code == 200, placed.text
    order = placed.json()["data"]
    order_id = order["id"]
    assert guest.place_order(buyer, key=key).json()["data"]["id"] == order_id
    total = Decimal("32.25")
    assert _customer_sees(guest, buyer, order_id) == {
        "status": "new", "payment_status": "pending", "total": total, "currency": currency,
        "lines": sorted([(widget.id, None, 2, Decimal("12.50")),
                         (shirt.id, shirt_xl.id, 1, Decimal("7.25"))])}
    assert (shop.stock(widget), shop.stock(shirt), shop.stock(shirt_xl)) == (8, 8, 4)
    assert visitor.get(CART, headers=headers).json()["data"]["items"] == []
    assert _emails(outbox) == [("confirmation", "new", "pending")]

    # 4. Pays: the wrong currency is refused, a card declined, then paid.
    other = "USD" if currency != "USD" else "EUR"
    wrong = guest.pay(buyer, order_id, currency=other)
    assert wrong.status_code == 400
    declined = guest.pay(buyer, order_id, token=PAY_DECLINED, currency=currency)
    assert declined.status_code == 402
    assert declined.json()["detail"] == "Your card was declined."
    assert order_state(shop, order_id) == ("new", "failed")
    paid = guest.pay(buyer, order_id, currency=currency)
    assert paid.status_code == 200, paid.text
    assert (paid.json()["data"]["status"], Decimal(paid.json()["data"]["amount"]),
            paid.json()["data"]["currency"]) == ("paid", total, currency)
    assert payment_rows(shop, order_id) == [
        ("failed", 3225, 0, currency, False), ("succeeded", 3225, 0, currency, False)]
    assert [c for c in provider_calls if c[0] == "create_payment_intent"] == [
        ("create_payment_intent", 3225, currency)] * 2
    assert _customer_sees(guest, buyer, order_id)["status"] == "confirmed"
    assert _emails(outbox) == [("status_update", "confirmed", "paid")]

    # 5. An admin fulfils it, one step at a time.
    assert shop.set_status(admin, order_id, "delivered").status_code == 400
    for status in ("processing", "sent", "delivered"):
        moved = shop.set_status(admin, order_id, status)
        assert moved.status_code == 200, moved.text
        assert _customer_sees(guest, buyer, order_id)["status"] == status
        assert _emails(outbox) == [("status_update", status, "paid")]
    assert shop.set_status(admin, order_id, "cancelled").status_code == 400

    # 6. Refunds: part, a retry of it, too much, then the rest.
    refund_key = new_key()
    partial = shop.refund(admin, order_id, amount="10.00", key=refund_key)
    assert partial.status_code == 200, partial.text
    assert (partial.json()["data"]["amount"], partial.json()["data"]["currency"],
            partial.json()["data"]["status"], partial.json()["data"]["payment_status"]) == (
        "10.00", currency, "succeeded", "paid")
    replay = shop.refund(admin, order_id, amount="10.00", key=refund_key)
    assert replay.json()["data"]["id"] == partial.json()["data"]["id"]
    assert shop.refund(admin, order_id, amount="30.00").status_code == 400
    assert payment_rows(shop, order_id)[1] == ("succeeded", 3225, 1000, currency, False)
    # Stripe reports the refund too; it is the same one.
    paying = shop.payments(order_id)[1]
    shop.webhook("refund.created", {"id": partial.json()["data"]["id"], "object": "refund",
                                    "amount": 1000, "currency": currency.lower(),
                                    "payment_intent": paying.payment_intent_id,
                                    "status": "succeeded"})
    rest = shop.refund(admin, order_id)
    assert rest.status_code == 200
    assert (rest.json()["data"]["amount"], rest.json()["data"]["payment_status"]) == (
        "22.25", "refunded")
    assert payment_rows(shop, order_id) == [
        ("failed", 3225, 0, currency, False), ("succeeded", 3225, 3225, currency, False)]
    assert [c[1:] for c in provider_calls if c[0] == "create_refund"] == [
        (1000, None), (1000, None), (3000, None), (None, None)]
    seen = _customer_sees(guest, buyer, order_id)
    assert (seen["status"], seen["payment_status"], seen["total"]) == ("delivered", "refunded", total)
    assert shop.refund(admin, order_id, amount="0.01").status_code == 400
    assert guest.pay(buyer, order_id).status_code == 400
    # Refunds return money, not goods: stock is as the order left it.
    assert (shop.stock(widget), shop.stock(shirt), shop.stock(shirt_xl)) == (8, 8, 4)
    assert cents(seen["total"]) == 3225
    assert _emails(outbox) == []


# -- 3D Secure ---------------------------------------------------------------------

def test_3ds_payment_completed_by_webhook(shop, outbox, provider_calls, currency):
    catalog = _catalog(shop)
    buyer, order = _ready_buyer(shop, catalog)
    outbox.take()

    pending = shop.pay(buyer, order["id"], token=PAY_3DS)

    body = pending.json()["data"]
    assert (pending.status_code, body["status"], body["requires_action"]) == (
        200, "requires_action", True)
    # With a return_url (which /pay always sends), Stripe answers a
    # redirect, and the mock does the same (tests/unit/test_mock_test_cards.py).
    assert body["next_action"]["type"] == "redirect_to_url"
    assert order_state(shop, order["id"]) == ("new", "pending")
    assert payment_rows(shop, order["id"]) == [("requires_action", 3225, 0, currency, False)]
    assert _emails(outbox) == []
    # Another attempt is refused while this one is in 3D Secure.
    assert shop.pay(buyer, order["id"]).status_code == 409

    done = shop.intent_event("payment_intent.succeeded", shop.payments(order["id"])[0])

    assert done.json()["data"] == {"status": "processed", "applied": True}
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, order["id"]) == [("succeeded", 3225, 0, currency, False)]
    assert _emails(outbox) == [("status_update", "confirmed", "paid")]
    assert provider_calls == [("create_payment_intent", 3225, currency)]


def test_3ds_payment_failed_then_paid_with_a_new_key(shop, outbox, currency):
    catalog = _catalog(shop)
    buyer, order = _ready_buyer(shop, catalog)
    outbox.take()
    shop.pay(buyer, order["id"], token=PAY_3DS)

    failed = shop.intent_event("payment_intent.payment_failed", shop.payments(order["id"])[0])

    assert failed.status_code == 200
    assert order_state(shop, order["id"]) == ("new", "failed")
    assert _customer_sees(shop, buyer, order["id"])["payment_status"] == "failed"
    retry = shop.pay(buyer, order["id"])
    assert retry.status_code == 200, retry.text
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, order["id"]) == [
        ("failed", 3225, 0, currency, False), ("succeeded", 3225, 0, currency, False)]
    assert _emails(outbox) == [("status_update", "confirmed", "paid")]


# -- Checkout Session ----------------------------------------------------------------

def test_checkout_session_completed(shop, outbox, provider_calls, currency):
    catalog = _catalog(shop)
    buyer, order = _ready_buyer(shop, catalog)
    outbox.take()

    session = shop.checkout_session(buyer, order["id"])

    assert session.status_code == 200, session.text
    data = session.json()["data"]
    assert (data["status"], Decimal(data["amount"]), data["currency"]) == (
        "pending", Decimal("32.25"), currency)
    assert data["url"].startswith("https://checkout.stripe.com/")
    assert provider_calls == [("create_checkout_session", 3225, currency)]
    attempt = shop.payments(order["id"])[0]

    done = shop.session_event("checkout.session.completed", attempt, payment_intent="pi_cs_1")

    assert done.json()["data"]["applied"] is True
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, order["id"]) == [("succeeded", 3225, 0, currency, False)]
    assert shop.payments(order["id"])[0].payment_intent_id == "pi_cs_1"
    assert _emails(outbox) == [("status_update", "confirmed", "paid")]


def test_checkout_session_expired_leaves_the_order_payable(shop, outbox, currency):
    catalog = _catalog(shop)
    buyer, order = _ready_buyer(shop, catalog)
    outbox.take()
    assert shop.checkout_session(buyer, order["id"]).status_code == 200

    expired = shop.session_event("checkout.session.expired", shop.payments(order["id"])[0],
                                 payment_status="unpaid")

    assert expired.status_code == 200
    assert order_state(shop, order["id"]) == ("new", "failed")
    assert payment_rows(shop, order["id"]) == [("failed", 3225, 0, currency, False)]
    assert shop.pay(buyer, order["id"]).status_code == 200
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert _emails(outbox) == [("status_update", "confirmed", "paid")]


@pytest.mark.parametrize("outcome,expected", [
    ("checkout.session.async_payment_succeeded", ("confirmed", "paid", "succeeded")),
    ("checkout.session.async_payment_failed", ("new", "failed", "failed")),
])
def test_checkout_session_paid_by_a_delayed_method(shop, outbox, currency, outcome, expected):
    catalog = _catalog(shop)
    buyer, order = _ready_buyer(shop, catalog)
    outbox.take()
    shop.checkout_session(buyer, order["id"])
    attempt = shop.payments(order["id"])[0]

    # Completed, but the bank debit has not cleared: nothing is applied.
    completed = shop.session_event("checkout.session.completed", attempt,
                                   payment_status="unpaid", payment_intent="pi_async")
    assert completed.json()["data"] == {"status": "processed", "applied": False}
    assert order_state(shop, order["id"]) == ("new", "pending")

    shop.session_event(outcome, attempt)

    assert (*order_state(shop, order["id"]), shop.payments(order["id"])[0].status) == expected
    assert shop.payments(order["id"])[0].payment_intent_id == "pi_async"
    assert len(_emails(outbox)) == (1 if expected[1] == "paid" else 0)


# -- Cancellation and expiry ------------------------------------------------------------

def test_customer_cancels_an_unpaid_order(shop, outbox, currency):
    catalog = _catalog(shop)
    widget, shirt, shirt_xl = catalog
    buyer, order = _ready_buyer(shop, catalog)
    admin = shop.admin()
    outbox.take()

    cancelled = shop.cancel(buyer, order["id"])

    assert cancelled.status_code == 200
    assert _customer_sees(shop, buyer, order["id"])["status"] == "cancelled"
    assert (shop.stock(widget), shop.stock(shirt), shop.stock(shirt_xl)) == (10, 8, 5)
    assert _emails(outbox) == [("cancellation", "cancelled", "pending")]
    assert shop.cancel(buyer, order["id"]).status_code == 400
    assert shop.pay(buyer, order["id"]).status_code == 400
    assert shop.checkout_session(buyer, order["id"]).status_code == 400
    assert shop.refund(admin, order["id"]).status_code == 400
    assert (shop.stock(widget), shop.stock(shirt_xl)) == (10, 5)
    assert shop.payments(order["id"]) == []


def test_admin_cancels_a_paid_order_then_refunds_it(shop, outbox, currency):
    catalog = _catalog(shop)
    widget, _, shirt_xl = catalog
    buyer, order = _ready_buyer(shop, catalog)
    admin = shop.admin()
    shop.pay(buyer, order["id"])
    assert shop.cancel(buyer, order["id"]).status_code == 400  # paid: support's job
    outbox.take()

    cancelled = shop.set_status(admin, order["id"], "cancelled")

    assert cancelled.status_code == 200
    assert order_state(shop, order["id"]) == ("cancelled", "paid")
    assert (shop.stock(widget), shop.stock(shirt_xl)) == (10, 5)
    assert _emails(outbox) == [("status_update", "cancelled", "paid")]
    refund = shop.refund(admin, order["id"])
    assert refund.json()["data"]["payment_status"] == "refunded"
    assert order_state(shop, order["id"]) == ("cancelled", "refunded")
    assert payment_rows(shop, order["id"]) == [("succeeded", 3225, 3225, currency, False)]
    assert (shop.stock(widget), shop.stock(shirt_xl)) == (10, 5)  # not returned twice


def test_expired_unpaid_order_and_a_late_payment(shop, outbox, tasks_db, currency):
    catalog = _catalog(shop)
    widget, _, shirt_xl = catalog
    buyer, order = _ready_buyer(shop, catalog)
    shop.pay(buyer, order["id"], token=PAY_3DS)  # the customer is in 3D Secure...
    outbox.take()

    assert shop.expire_unpaid(order["id"]) == 1  # ...for longer than the order lives

    assert order_state(shop, order["id"]) == ("cancelled", "pending")
    assert (shop.stock(widget), shop.stock(shirt_xl)) == (10, 5)
    assert _emails(outbox) == [("cancellation", "cancelled", "pending")]

    late = shop.intent_event("payment_intent.succeeded", shop.payments(order["id"])[0])

    assert late.json()["data"]["requires_manual_review"] is True
    assert order_state(shop, order["id"]) == ("cancelled", "pending")
    assert payment_rows(shop, order["id"]) == [("succeeded", 3225, 0, currency, True)]
    assert (shop.stock(widget), shop.stock(shirt_xl)) == (10, 5)
    assert _emails(outbox) == []


# -- Events replayed and out of order ----------------------------------------------------

def test_success_after_failure_for_the_same_attempt_pays_the_order(shop, outbox):
    catalog = _catalog(shop)
    buyer, order = _ready_buyer(shop, catalog)
    shop.pay(buyer, order["id"], token=PAY_3DS)
    attempt = shop.payments(order["id"])[0]
    outbox.take()

    shop.intent_event("payment_intent.payment_failed", attempt)
    shop.intent_event("payment_intent.succeeded", attempt)

    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, order["id"]) == [("succeeded", 3225, 0, "USD", False)]
    assert _emails(outbox) == [("status_update", "confirmed", "paid")]


def test_failure_after_success_and_a_duplicate_success_change_nothing(shop, outbox):
    catalog = _catalog(shop)
    buyer, order = _ready_buyer(shop, catalog)
    shop.pay(buyer, order["id"], token=PAY_3DS)
    attempt = shop.payments(order["id"])[0]
    shop.intent_event("payment_intent.succeeded", attempt)
    outbox.take()

    shop.intent_event("payment_intent.payment_failed", attempt)
    shop.intent_event("payment_intent.succeeded", attempt)
    shop.intent_event("payment_intent.canceled", attempt)

    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, order["id"]) == [("succeeded", 3225, 0, "USD", False)]
    assert _emails(outbox) == []


# -- When a refund is allowed ------------------------------------------------------------

@pytest.mark.parametrize("path,allowed", [
    ([], False),                                        # new, unpaid
    (["pay"], True),                                    # confirmed
    (["pay", "processing"], True),
    (["pay", "processing", "sent"], True),
    (["pay", "processing", "sent", "delivered"], True),
    (["pay", "cancelled"], True),                       # cancelled after payment
    (["decline"], False),                               # payment failed
    (["cancel"], False),                                # cancelled unpaid
])
def test_a_full_refund_in_one_go_at_each_status(shop, path, allowed):
    catalog = _catalog(shop)
    buyer, order = _ready_buyer(shop, catalog)
    admin = shop.admin()
    for step in path:
        if step == "pay":
            assert shop.pay(buyer, order["id"]).status_code == 200
        elif step == "decline":
            assert shop.pay(buyer, order["id"], token=PAY_DECLINED).status_code == 402
        elif step == "cancel":
            assert shop.cancel(buyer, order["id"]).status_code == 200
        else:
            assert shop.set_status(admin, order["id"], step).status_code == 200
    status_before, payment_before = order_state(shop, order["id"])

    response = shop.refund(admin, order["id"])

    if allowed:
        assert response.status_code == 200, response.text
        assert (response.json()["data"]["amount"], response.json()["data"]["payment_status"]) == (
            "32.25", "refunded")
        assert order_state(shop, order["id"]) == (status_before, "refunded")
        assert payment_rows(shop, order["id"])[-1][:3] == ("succeeded", 3225, 3225)
    else:
        assert response.status_code == 400
        assert response.json()["detail"] == "Only a paid order can be refunded"
        assert order_state(shop, order["id"]) == (status_before, payment_before)
        assert all(row[2] == 0 for row in payment_rows(shop, order["id"]))


def test_order_and_payment_endpoints_answer_unknown_orders_with_404(shop):
    buyer = shop.buyer()
    admin = shop.admin()
    assert shop.get_order(buyer, 999999).status_code == 404
    assert shop.pay(buyer, 999999, amount="1.00").status_code == 404
    assert shop.checkout_session(buyer, 999999).status_code == 404
    assert shop.cancel(buyer, 999999).status_code == 404
    assert shop.refund(admin, 999999).status_code == 404
    assert shop.client.get(f"{ORDERS}/999999/payments", headers=admin).status_code == 404
