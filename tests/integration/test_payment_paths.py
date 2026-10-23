"""
The error and race paths of the order/payment routes (app/api/v1/orders.py)
that the flow tests do not reach: an Idempotency-Key race lost at the
unique index, the provider timing out, refusing a reused key or rejecting
the request, Redis failing under the refund bookkeeping, and outcomes
reported twice or out of order. Each test checks what the customer is
told and what is left behind: the order's status and payment_status, the
Payment rows, stock, and the emails queued.

A race is reproduced by committing the competing row from another session
at the point the route has passed its own pre-check and not yet committed
(the suite's sessions share one SQLite connection, so the route then sees
it at its commit exactly as Postgres would show it the other transaction's
row).
"""
from decimal import Decimal

import pytest
from conftest import IS_POSTGRES, TestingSessionLocal
from payment_support import (
    CART,
    PAY_3DS,
    PAY_DECLINED,
    Outbox,
    Shop,
    cents,
    new_key,
    order_state,
    payment_rows,
    reset_provider,
)

import app.api.v1.orders as orders_module
from app.models.order import Order
from app.models.payment import Payment
from app.models.user import User
from app.services.payment.exceptions import (
    IdempotencyError,
    InvalidRequestError,
    PaymentProviderTimeoutError,
)
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.payment.types import PaymentIntentStatus
from app.services.redis_async import AsyncRedisService


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def outbox(monkeypatch):
    return Outbox(monkeypatch)


@pytest.fixture
def shop(client, db_session):
    return Shop(client, db_session)


# A twin request that inserts its row while this request holds the order's
# FOR UPDATE lock cannot exist on PostgreSQL: the insert takes a FOR KEY SHARE
# lock on the order it references (payments.order_id), which FOR UPDATE
# conflicts with, so the twin waits until this request has committed (the
# lock itself is shown in test_postgres_only.py). SQLite, which has no row
# locks, is where the twin can be simulated.
_needs_no_row_locks = pytest.mark.skipif(
    IS_POSTGRES,
    reason="the simulated twin would wait for the order row lock this request holds "
    "(an insert referencing the order conflicts with FOR UPDATE)",
)


def _insert_from_another_session(row):
    db = TestingSessionLocal()
    try:
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _provider_calls(monkeypatch, name):
    """Count calls to one mock-provider method, still running it."""
    calls = []
    real = getattr(StripePaymentProvider, name)

    def _counting(self, *args, **kwargs):
        calls.append(kwargs)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(StripePaymentProvider, name, _counting)
    return calls


def _provider_raises(monkeypatch, name, error):
    def _raise(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(StripePaymentProvider, name, _raise)


# -- POST /orders --------------------------------------------------------------

def test_order_needs_a_phone_number_on_the_profile(shop, outbox):
    product = shop.product(stock=5)
    buyer = shop.buyer()
    user = shop.db.get(User, buyer.user_id)
    user.phone = None
    shop.db.commit()
    assert shop.add_to_cart(buyer, product, 2).status_code == 200

    response = shop.place_order(buyer)

    assert response.status_code == 400
    assert "phone" in response.json()["detail"]
    assert shop.stock(product) == 5
    assert shop.db.query(Order).count() == 0
    assert outbox.take() == []


def test_order_racing_a_twin_with_the_same_key_returns_the_twins_order(
    shop, outbox, monkeypatch
):
    # Two checkouts with one key both pass the "already placed?" read; the
    # unique index lets one insert its order. The loser rolls back and
    # answers with the winner's order. That index fails the INSERT, which
    # the flush right after db.add(order) sends - and only the commit
    # further down was guarded, so the loser answered 500.
    product = shop.product(price="7.50", stock=5)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product, 2).status_code == 200
    key = new_key()
    real_get_cart = AsyncRedisService.get_cart
    twin = {}

    async def _twin_commits_first(self, user_id):
        if twin:
            return await real_get_cart(self, user_id)
        twin["id"] = _insert_from_another_session(Order(
            user_id=buyer.user_id, status="new", payment_status="pending",
            shipping_address_id=buyer.address_id, idempotency_key=key,
            total_amount=Decimal("15.00"), currency="USD",
        ))
        return await real_get_cart(self, user_id)

    monkeypatch.setattr(AsyncRedisService, "get_cart", _twin_commits_first)

    response = shop.place_order(buyer, key=key)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["id"] == twin["id"]
    assert [o.id for o in shop.db.query(Order).all()] == [twin["id"]]
    # The loser took no stock (the twin row was inserted directly, so it
    # took none either) and its cart lines were not removed.
    assert shop.stock(product) == 5
    cart = shop.client.get(CART, headers=buyer.headers)
    assert [i["quantity"] for i in cart.json()["data"]["items"]] == [2]
    # Only the request that placed the order confirms it.
    assert outbox.take() == []


# -- POST /orders/{id}/pay -------------------------------------------------------

def test_pay_with_an_amount_other_than_the_total_is_refused_before_the_provider(
    shop, monkeypatch
):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    created = _provider_calls(monkeypatch, "create_payment_intent")

    response = shop.pay(buyer, order["id"], amount="19.98")

    assert response.status_code == 400
    assert response.json()["detail"] == "Payment amount does not match order total"
    assert created == []
    assert shop.payments(order["id"]) == []
    assert order_state(shop, order["id"]) == ("new", "pending")


@_needs_no_row_locks
def test_pay_racing_a_twin_with_the_same_key_returns_the_twins_attempt(
    shop, outbox, monkeypatch
):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    outbox.take()
    key = new_key()
    real_guard = orders_module._guard_currency_matches_order
    twin = {}

    def _twin_records_its_attempt_first(requested_currency, order_row):
        twin["id"] = _insert_from_another_session(Payment(
            order_id=order_row.id, provider="stripe", payment_intent_id="pi_twin_attempt",
            idempotency_key=key, amount=1999, currency="USD", status="processing",
        ))
        return real_guard(requested_currency, order_row)

    monkeypatch.setattr(
        orders_module, "_guard_currency_matches_order", _twin_records_its_attempt_first
    )
    created = _provider_calls(monkeypatch, "create_payment_intent")

    response = shop.pay(buyer, order["id"], key=key)

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["id"] == twin["id"]
    assert body["status"] == "pending"
    assert body["transaction_id"] == "pi_twin_attempt"
    # The loser never reached the provider: one attempt, one charge at most.
    assert created == []
    assert payment_rows(shop, order["id"]) == [("processing", 1999, 0, "USD", False)]
    assert order_state(shop, order["id"]) == ("new", "pending")
    assert outbox.take() == []


def test_pay_whose_provider_key_is_refused_as_reused_answers_409(shop, outbox, monkeypatch):
    # The provider refuses the request outright (IdempotencyError: the key
    # is already in use at the provider). Nothing was charged by this
    # request, and the order is left as it was.
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    outbox.take()
    _provider_raises(monkeypatch, "create_payment_intent",
                     IdempotencyError("Keys for idempotent requests can only be used once"))

    response = shop.pay(buyer, order["id"])

    assert response.status_code == 409
    assert "Idempotency key" in response.json()["detail"]
    assert order_state(shop, order["id"]) == ("new", "pending")
    assert [p[1:] for p in payment_rows(shop, order["id"])] == [(1999, 0, "USD", False)]
    assert outbox.take() == []


def _confirm_reports(monkeypatch, status):
    real_confirm = StripePaymentProvider.confirm_payment_intent

    def _confirm(self, payment_intent_id, **kwargs):
        intent = real_confirm(self, payment_intent_id, **kwargs)
        intent.status = status
        return intent

    monkeypatch.setattr(StripePaymentProvider, "confirm_payment_intent", _confirm)


def test_pay_whose_intent_is_still_processing_is_not_marked_paid(shop, outbox, monkeypatch):
    # The live provider reports any PaymentIntent status it does not model
    # (requires_capture, ...) and Stripe's own "processing" as PROCESSING.
    # /pay treated everything that was not requires_action as a success:
    # the order was marked paid, confirmed and emailed before any money
    # was taken. The outcome of a processing intent arrives by webhook.
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    outbox.take()
    _confirm_reports(monkeypatch, PaymentIntentStatus.PROCESSING)

    response = shop.pay(buyer, order["id"])

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "pending"
    assert order_state(shop, order["id"]) == ("new", "pending")
    assert payment_rows(shop, order["id"]) == [("processing", 1999, 0, "USD", False)]
    assert outbox.take() == []
    # The webhook then settles it.
    payment = shop.payments(order["id"])[0]
    assert shop.intent_event("payment_intent.succeeded", payment).status_code == 200
    assert order_state(shop, order["id"]) == ("confirmed", "paid")


@pytest.mark.parametrize("status", [
    PaymentIntentStatus.REQUIRES_PAYMENT_METHOD,
    PaymentIntentStatus.CANCELED,
    PaymentIntentStatus.REQUIRES_CONFIRMATION,
])
def test_pay_whose_intent_ends_unpaid_without_an_error_fails_the_attempt(
    shop, outbox, monkeypatch, status
):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    outbox.take()
    _confirm_reports(monkeypatch, status)

    response = shop.pay(buyer, order["id"])

    assert response.status_code == 402, response.text
    assert order_state(shop, order["id"]) == ("new", "failed")
    assert payment_rows(shop, order["id"]) == [("failed", 1999, 0, "USD", False)]
    assert outbox.take() == []


# -- POST /orders/{id}/checkout-session ----------------------------------------

@_needs_no_row_locks
def test_checkout_session_racing_a_twin_with_the_same_key_returns_the_twins_session(
    shop, monkeypatch
):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    real_guard = orders_module._guard_currency_matches_order
    twin = {}

    def _twin_records_its_session_first(requested_currency, order_row):
        twin["id"] = _insert_from_another_session(Payment(
            order_id=order_row.id, provider="stripe", payment_intent_id="pending-twin",
            idempotency_key=key, amount=1999, currency="USD", status="processing",
            checkout_session_id="cs_test_twin", checkout_session_url="https://pay.test/twin",
        ))
        return real_guard(requested_currency, order_row)

    monkeypatch.setattr(
        orders_module, "_guard_currency_matches_order", _twin_records_its_session_first
    )
    created = _provider_calls(monkeypatch, "create_checkout_session")

    response = shop.checkout_session(buyer, order["id"], key=key)

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert (body["payment_id"], body["checkout_session_id"], body["url"]) == (
        twin["id"], "cs_test_twin", "https://pay.test/twin")
    assert created == []
    assert len(shop.payments(order["id"])) == 1


def test_checkout_session_provider_timeout_leaves_the_attempt_unknown(shop, outbox, monkeypatch):
    product = shop.product(price="19.99", stock=3)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    outbox.take()
    _provider_raises(monkeypatch, "create_checkout_session", PaymentProviderTimeoutError())

    response = shop.checkout_session(buyer, order["id"])

    assert response.status_code == 503
    assert response.headers["Retry-After"]
    assert response.json()["detail"] == "Payment provider unavailable, please retry"
    assert payment_rows(shop, order["id"]) == [("unknown", 1999, 0, "USD", False)]
    assert shop.payments(order["id"])[0].checkout_session_id is None
    assert order_state(shop, order["id"]) == ("new", "pending")
    assert shop.stock(product) == 2
    assert outbox.take() == []


def test_checkout_session_after_a_timeout_with_a_new_key_answers_409_and_creates_nothing(
    shop, monkeypatch
):
    # The first request may have created its session at Stripe: a new key
    # would be a second session for the order. Only the first key settles it.
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    with monkeypatch.context() as patched:
        _provider_raises(patched, "create_checkout_session", PaymentProviderTimeoutError())
        assert shop.checkout_session(buyer, order["id"]).status_code == 503
    created = _provider_calls(monkeypatch, "create_checkout_session")

    response = shop.checkout_session(buyer, order["id"])

    assert response.status_code == 409
    assert "same Idempotency-Key" in response.json()["detail"]
    assert created == []
    assert [p.status for p in shop.payments(order["id"])] == ["unknown"]


def test_checkout_session_retried_with_its_key_after_a_lost_response_reaches_the_same_session(
    shop, monkeypatch
):
    # Stripe created the session but the answer was lost. Same key -> the
    # same create request with the same Stripe idempotency key; Stripe
    # replays the original session, so there is exactly one.
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    with monkeypatch.context() as patched:
        _lose_the_response(patched, "create_checkout_session")
        assert shop.checkout_session(buyer, order["id"], key=key).status_code == 503
    assert [p.status for p in shop.payments(order["id"])] == ["unknown"]
    (lost_session,) = StripePaymentProvider._sessions_by_key.values()
    created = _provider_calls(monkeypatch, "create_checkout_session")

    response = shop.checkout_session(buyer, order["id"], key=key)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["checkout_session_id"] == lost_session.id
    assert len(created) == 1
    assert created[0]["idempotency_key"] == f"checkout-order-{order['id']}:{key}"
    assert len(StripePaymentProvider._sessions_by_key) == 1
    row = shop.payments(order["id"])[0]
    assert (row.status, row.checkout_session_id) == ("processing", lost_session.id)
    # Settled: the same key now replays, and does not reach Stripe.
    assert shop.checkout_session(buyer, order["id"], key=key).status_code == 200
    assert len(created) == 1


def test_checkout_session_retry_that_times_out_again_stays_unknown(shop, monkeypatch):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    _provider_raises(monkeypatch, "create_checkout_session", PaymentProviderTimeoutError())
    assert shop.checkout_session(buyer, order["id"], key=key).status_code == 503

    assert shop.checkout_session(buyer, order["id"], key=key).status_code == 503

    assert [p.status for p in shop.payments(order["id"])] == ["unknown"]


def test_checkout_session_retry_the_provider_rejects_fails_the_attempt(shop, monkeypatch):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    with monkeypatch.context() as patched:
        _provider_raises(patched, "create_checkout_session", PaymentProviderTimeoutError())
        assert shop.checkout_session(buyer, order["id"], key=key).status_code == 503
    _provider_raises(monkeypatch, "create_checkout_session",
                     InvalidRequestError("Invalid currency: 'usd'", param="currency"))

    response = shop.checkout_session(buyer, order["id"], key=key)

    assert response.status_code == 400
    assert [p.status for p in shop.payments(order["id"])] == ["failed"]


def test_checkout_session_retry_refused_by_the_provider_as_a_reused_key_leaves_it_unknown(
    shop, monkeypatch
):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    with monkeypatch.context() as patched:
        _provider_raises(patched, "create_checkout_session", PaymentProviderTimeoutError())
        assert shop.checkout_session(buyer, order["id"], key=key).status_code == 503
    _provider_raises(monkeypatch, "create_checkout_session", IdempotencyError("reused"))

    response = shop.checkout_session(buyer, order["id"], key=key)

    assert response.status_code == 409
    assert "checkout session parameters" in response.json()["detail"]
    assert [p.status for p in shop.payments(order["id"])] == ["unknown"]


def test_checkout_session_retry_of_an_unknown_attempt_whose_order_was_cancelled_answers_400(
    shop, monkeypatch
):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    _provider_raises(monkeypatch, "create_checkout_session", PaymentProviderTimeoutError())
    assert shop.checkout_session(buyer, order["id"], key=key).status_code == 503
    shop.db.query(Payment).filter(Payment.order_id == order["id"]).update({"status": "failed"})
    shop.db.commit()
    assert shop.cancel(buyer, order["id"]).status_code == 200
    shop.db.query(Payment).filter(Payment.order_id == order["id"]).update({"status": "unknown"})
    shop.db.commit()

    response = shop.checkout_session(buyer, order["id"], key=key)

    assert response.status_code == 400
    assert [p.status for p in shop.payments(order["id"])] == ["unknown"]


def test_checkout_session_retry_of_an_unknown_attempt_is_blocked_by_another_in_flight(
    shop, monkeypatch
):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    _provider_raises(monkeypatch, "create_checkout_session", PaymentProviderTimeoutError())
    assert shop.checkout_session(buyer, order["id"], key=key).status_code == 503
    shop.db.add(Payment(
        order_id=order["id"], provider="stripe", payment_intent_id="pi_other",
        idempotency_key=new_key(), amount=1999, currency="USD", status="processing"))
    shop.db.commit()

    response = shop.checkout_session(buyer, order["id"], key=key)

    assert response.status_code == 409
    assert sorted(p.status for p in shop.payments(order["id"])) == ["processing", "unknown"]


def test_pay_with_the_key_of_an_unknown_checkout_attempt_answers_409_and_reaches_no_provider(
    shop, monkeypatch
):
    # The key belongs to a checkout-session attempt: /pay must not run it
    # as a PaymentIntent (a second way to charge the order).
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    with monkeypatch.context() as patched:
        _provider_raises(patched, "create_checkout_session", PaymentProviderTimeoutError())
        assert shop.checkout_session(buyer, order["id"], key=key).status_code == 503
    created = _provider_calls(monkeypatch, "create_payment_intent")

    response = shop.pay(buyer, order["id"], key=key)

    assert response.status_code == 409
    assert "checkout session" in response.json()["detail"]
    assert created == []
    assert [p.status for p in shop.payments(order["id"])] == ["unknown"]


def test_checkout_session_with_the_key_of_an_unknown_pay_attempt_answers_409(
    shop, monkeypatch
):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    with monkeypatch.context() as patched:
        _provider_raises(patched, "create_payment_intent", PaymentProviderTimeoutError())
        assert shop.pay(buyer, order["id"], key=key).status_code == 503
    created = _provider_calls(monkeypatch, "create_checkout_session")

    response = shop.checkout_session(buyer, order["id"], key=key)

    assert response.status_code == 409
    assert created == []
    assert [p.status for p in shop.payments(order["id"])] == ["unknown"]


def test_checkout_session_whose_provider_key_is_refused_as_reused_answers_409(
    shop, monkeypatch
):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    _provider_raises(monkeypatch, "create_checkout_session",
                     IdempotencyError("Keys for idempotent requests can only be used once"))

    response = shop.checkout_session(buyer, order["id"])

    assert response.status_code == 409
    assert "checkout session parameters" in response.json()["detail"]
    assert order_state(shop, order["id"]) == ("new", "pending")
    assert shop.payments(order["id"])[0].checkout_session_id is None


def test_checkout_session_the_provider_rejects_fails_the_attempt_with_400(shop, monkeypatch):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    _provider_raises(monkeypatch, "create_checkout_session",
                     InvalidRequestError("Invalid currency: 'usd'", param="currency"))

    response = shop.checkout_session(buyer, order["id"])

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid currency: 'usd'"
    assert payment_rows(shop, order["id"]) == [("failed", 1999, 0, "USD", False)]
    # The order itself is untouched and can be paid another way.
    assert order_state(shop, order["id"]) == ("new", "pending")
    assert shop.pay(buyer, order["id"]).status_code == 200


def test_checkout_session_retried_with_the_key_of_a_failed_attempt_answers_409_not_500(
    shop, monkeypatch
):
    # The provider rejected the first request: its attempt is recorded
    # as failed, with no session. A retry with the same key found that row
    # without a session, went on to insert a second row with the same key
    # and hit the unique index - answering 500.
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    with monkeypatch.context() as patched:
        _provider_raises(patched, "create_checkout_session",
                         InvalidRequestError("Invalid currency: 'usd'", param="currency"))
        assert shop.checkout_session(buyer, order["id"], key=key).status_code == 400
    created = _provider_calls(monkeypatch, "create_checkout_session")

    response = shop.checkout_session(buyer, order["id"], key=key)

    assert response.status_code == 409, response.text
    assert "new Idempotency-Key" in response.json()["detail"]
    assert created == []
    assert payment_rows(shop, order["id"]) == [("failed", 1999, 0, "USD", False)]
    # A new key goes through.
    assert shop.checkout_session(buyer, order["id"]).status_code == 200


def test_checkout_session_with_the_key_of_a_declined_pay_attempt_answers_409_not_500(shop):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    assert shop.pay(buyer, order["id"], token=PAY_DECLINED, key=key).status_code == 402

    response = shop.checkout_session(buyer, order["id"], key=key)

    assert response.status_code == 409, response.text
    assert [p.status for p in shop.payments(order["id"])] == ["failed"]
    assert order_state(shop, order["id"]) == ("new", "failed")


@_needs_no_row_locks
def test_checkout_session_racing_a_twin_still_creating_its_session_answers_409(
    shop, monkeypatch
):
    # The twin has recorded its attempt but is still waiting on the
    # provider, so there is no session to replay yet.
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    real_guard = orders_module._guard_currency_matches_order

    def _twin_records_its_attempt_first(requested_currency, order_row):
        _insert_from_another_session(Payment(
            order_id=order_row.id, provider="stripe", payment_intent_id="pending-twin",
            idempotency_key=key, amount=1999, currency="USD", status="processing",
        ))
        return real_guard(requested_currency, order_row)

    monkeypatch.setattr(
        orders_module, "_guard_currency_matches_order", _twin_records_its_attempt_first
    )
    created = _provider_calls(monkeypatch, "create_checkout_session")

    response = shop.checkout_session(buyer, order["id"], key=key)

    assert response.status_code == 409, response.text
    assert created == []
    assert len(shop.payments(order["id"])) == 1


# -- POST /orders/{id}/refund ----------------------------------------------------

def _paid_order(shop, price="19.99"):
    product = shop.product(price=price, stock=4)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    assert shop.pay(buyer, order["id"]).status_code == 200
    return product, buyer, order


def test_refund_provider_timeout_answers_503_and_records_nothing(shop, outbox, monkeypatch):
    product, buyer, order = _paid_order(shop)
    admin = shop.admin()
    outbox.take()
    _provider_raises(monkeypatch, "create_refund", PaymentProviderTimeoutError())

    response = shop.refund(admin, order["id"], amount="5.00")

    assert response.status_code == 503
    assert response.headers["Retry-After"]
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 0, "USD", False)]
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert shop.stock(product) == 3
    assert outbox.take() == []


def test_refund_still_recorded_when_redis_cannot_guard_the_bookkeeping(
    shop, fake_redis, monkeypatch
):
    # The SETNX marker that keeps a replayed refund from being counted
    # twice fails open: the provider already took the refund, so it is
    # recorded rather than lost.
    _, _, order = _paid_order(shop)
    admin = shop.admin()
    real_set = fake_redis.set

    def _set(name, *args, **kwargs):
        if str(name).startswith("refund_applied:"):
            raise ConnectionError("Redis is down")
        return real_set(name, *args, **kwargs)

    monkeypatch.setattr(fake_redis, "set", _set)

    response = shop.refund(admin, order["id"], amount="4.99")

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert (Decimal(body["amount"]), body["payment_status"]) == (Decimal("4.99"), "paid")
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 499, "USD", False)]


def test_a_refund_replayed_while_redis_is_down_is_counted_once(shop, fake_redis, monkeypatch):
    # The only guard against counting a replayed refund twice was a Redis
    # SETNX marker, and it failed open: with Redis down, a retried refund
    # (same Idempotency-Key, so the provider returned the same refund) was
    # added to amount_refunded a second time. Two retries of 10.00 on a
    # 19.99 charge broke the refunded <= amount constraint and answered 500.
    _, _, order = _paid_order(shop)
    admin = shop.admin()
    real_set = fake_redis.set

    def _set(name, *args, **kwargs):
        if str(name).startswith("refund_applied:"):
            raise ConnectionError("Redis is down")
        return real_set(name, *args, **kwargs)

    monkeypatch.setattr(fake_redis, "set", _set)
    key = new_key()

    first = shop.refund(admin, order["id"], amount="10.00", key=key)
    replay = shop.refund(admin, order["id"], amount="10.00", key=key)

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["id"] == first.json()["data"]["id"]
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 1000, "USD", False)]
    assert order_state(shop, order["id"]) == ("confirmed", "paid")


# -- Outcomes reported twice or out of order -------------------------------------

def test_success_reported_again_by_webhook_after_pay_changes_nothing(shop, outbox):
    # /pay applied the success itself; Stripe then delivers
    # payment_intent.succeeded for the same intent (another event id). It
    # is acknowledged as applied, with no second email and no change.
    product, buyer, order = _paid_order(shop)
    assert [e[0] for e in outbox.take()] == ["confirmation", "status_update"]
    payment = shop.payments(order["id"])[0]

    response = shop.intent_event("payment_intent.succeeded", payment)

    assert response.status_code == 200
    assert response.json()["data"] == {"status": "processed", "applied": True}
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 0, "USD", False)]
    assert outbox.take() == []


def test_failure_reported_after_the_attempt_succeeded_changes_nothing(shop, outbox):
    _, _, order = _paid_order(shop)
    outbox.take()
    payment = shop.payments(order["id"])[0]

    response = shop.intent_event("payment_intent.payment_failed", payment)

    assert response.status_code == 200
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 0, "USD", False)]
    assert outbox.take() == []


def test_payment_for_an_order_an_admin_already_moved_on_keeps_its_status(shop, outbox):
    # An admin may confirm and start processing an order before it is paid
    # (the transition table does not look at payment). The payment then
    # marks it paid without moving it back to confirmed.
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    admin = shop.admin()
    order = shop.order_of(buyer, product)
    for status in ("confirmed", "processing"):
        assert shop.set_status(admin, order["id"], status).status_code == 200
    outbox.take()

    response = shop.pay(buyer, order["id"])

    assert response.status_code == 200, response.text
    assert order_state(shop, order["id"]) == ("processing", "paid")
    assert [(e[0], e[3], e[4]) for e in outbox.take()] == [
        ("status_update", "processing", "paid")]


def test_checkout_completed_with_an_expanded_payment_intent_backfills_its_id(shop, outbox):
    # The session's payment_intent is a string id on webhook payloads, but
    # an expanded object ({"id": ...}) is read the same way.
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    assert shop.checkout_session(buyer, order["id"]).status_code == 200
    payment = shop.payments(order["id"])[0]

    response = shop.session_event("checkout.session.completed", payment,
                                  payment_intent={"id": "pi_expanded_object", "object": "payment_intent"})

    assert response.status_code == 200
    assert response.json()["data"]["applied"] is True
    assert shop.payments(order["id"])[0].payment_intent_id == "pi_expanded_object"
    assert order_state(shop, order["id"]) == ("confirmed", "paid")


def test_3ds_attempt_success_reported_twice_with_different_event_ids_applies_once(
    shop, outbox
):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    assert shop.pay(buyer, order["id"], token=PAY_3DS).json()["data"]["requires_action"]
    outbox.take()
    payment = shop.payments(order["id"])[0]

    first = shop.intent_event("payment_intent.succeeded", payment)
    second = shop.intent_event("payment_intent.succeeded", payment)

    assert first.json()["data"]["applied"] is True
    assert second.json()["data"]["applied"] is True
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 0, "USD", False)]
    assert [(e[0], e[4]) for e in outbox.take()] == [("status_update", "paid")]
    assert cents(order["total_amount"]) == 1999


# -- /pay after a timeout: the outcome is unknown, not failed -------------------

def _lose_the_response(monkeypatch, name):
    """The provider carries out the call and the response never arrives."""
    real = getattr(StripePaymentProvider, name)

    def _lost(self, *args, **kwargs):
        real(self, *args, **kwargs)
        raise PaymentProviderTimeoutError("response lost")

    monkeypatch.setattr(StripePaymentProvider, name, _lost)


def test_pay_retried_with_its_key_after_a_lost_confirm_response_reaches_the_same_intent(
    shop, monkeypatch
):
    # Stripe confirmed (and charged) but the answer was lost: the attempt's
    # outcome is unknown, not failed. Same key -> the same Stripe requests
    # again (Stripe replays the original result); no second PaymentIntent.
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    with monkeypatch.context() as patched:
        _lose_the_response(patched, "confirm_payment_intent")
        assert shop.pay(buyer, order["id"], key=key).status_code == 503
    assert [p.status for p in shop.payments(order["id"])] == ["unknown"]
    created = _provider_calls(monkeypatch, "create_payment_intent")
    confirmed = _provider_calls(monkeypatch, "confirm_payment_intent")
    first_intent_id = shop.payments(order["id"])[0].payment_intent_id

    response = shop.pay(buyer, order["id"], key=key)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "paid"
    assert response.json()["data"]["transaction_id"] == first_intent_id
    assert len(created) == 1 and len(confirmed) == 1
    assert created[0]["idempotency_key"].endswith(key)
    assert confirmed[0]["idempotency_key"] == created[0]["idempotency_key"] + ":confirm"
    assert len(StripePaymentProvider._intents_by_id) == 1
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 0, "USD", False)]
    assert order_state(shop, order["id"]) == ("confirmed", "paid")


def test_pay_retried_with_its_key_after_a_lost_create_response_creates_one_intent(
    shop, monkeypatch
):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    with monkeypatch.context() as patched:
        _lose_the_response(patched, "create_payment_intent")
        assert shop.pay(buyer, order["id"], key=key).status_code == 503
    assert len(StripePaymentProvider._intents_by_id) == 1

    response = shop.pay(buyer, order["id"], key=key)

    assert response.status_code == 200, response.text
    assert len(StripePaymentProvider._intents_by_id) == 1
    assert payment_rows(shop, order["id"]) == [("succeeded", 1999, 0, "USD", False)]


def test_pay_with_a_new_key_while_an_attempt_is_unknown_answers_409_and_charges_nothing(
    shop, monkeypatch
):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    with monkeypatch.context() as patched:
        _lose_the_response(patched, "confirm_payment_intent")
        assert shop.pay(buyer, order["id"]).status_code == 503

    response = shop.pay(buyer, order["id"])

    assert response.status_code == 409
    assert "same Idempotency-Key" in response.json()["detail"]
    assert len(StripePaymentProvider._intents_by_id) == 1
    assert [p.status for p in shop.payments(order["id"])] == ["unknown"]


def test_pay_retry_that_times_out_again_stays_unknown(shop, monkeypatch):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    _provider_raises(monkeypatch, "create_payment_intent", PaymentProviderTimeoutError())
    assert shop.pay(buyer, order["id"], key=key).status_code == 503

    assert shop.pay(buyer, order["id"], key=key).status_code == 503

    assert [p.status for p in shop.payments(order["id"])] == ["unknown"]


def test_pay_retry_of_an_unknown_attempt_whose_order_was_cancelled_answers_400(
    shop, monkeypatch
):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    _provider_raises(monkeypatch, "create_payment_intent", PaymentProviderTimeoutError())
    assert shop.pay(buyer, order["id"], key=key).status_code == 503
    # The attempt is unknown, and the order is cancelled meanwhile.
    shop.db.query(Payment).filter(Payment.order_id == order["id"]).update({"status": "failed"})
    shop.db.commit()
    assert shop.cancel(buyer, order["id"]).status_code == 200
    shop.db.query(Payment).filter(Payment.order_id == order["id"]).update({"status": "unknown"})
    shop.db.commit()

    response = shop.pay(buyer, order["id"], key=key)

    assert response.status_code == 400
    assert [p.status for p in shop.payments(order["id"])] == ["unknown"]


def test_pay_retry_of_an_unknown_attempt_is_blocked_by_another_attempt_in_flight(
    shop, monkeypatch
):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    _provider_raises(monkeypatch, "create_payment_intent", PaymentProviderTimeoutError())
    assert shop.pay(buyer, order["id"], key=key).status_code == 503
    # A second attempt that started after the first one went stale.
    shop.db.add(Payment(
        order_id=order["id"], provider="stripe", payment_intent_id="pi_other",
        idempotency_key=new_key(), amount=1999, currency="USD", status="processing"))
    shop.db.commit()

    response = shop.pay(buyer, order["id"], key=key)

    assert response.status_code == 409
    assert sorted(p.status for p in shop.payments(order["id"])) == ["processing", "unknown"]


def test_pay_retry_refused_by_the_provider_as_a_reused_key_leaves_the_attempt_unknown(
    shop, monkeypatch
):
    product = shop.product(price="19.99")
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    key = new_key()
    with monkeypatch.context() as patched:
        _provider_raises(patched, "create_payment_intent", PaymentProviderTimeoutError())
        assert shop.pay(buyer, order["id"], key=key).status_code == 503
    _provider_raises(monkeypatch, "create_payment_intent", IdempotencyError("reused"))

    response = shop.pay(buyer, order["id"], key=key)

    assert response.status_code == 409
    assert [p.status for p in shop.payments(order["id"])] == ["unknown"]
