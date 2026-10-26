import torture_support  # noqa: F401, I001  (must precede payment_support & co.)

import asyncio
import collections
import json
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import fakeredis
import httpx2
import jwt
import pytest
from conftest import TestingSessionLocal
from payment_support import PAY_3DS, PAY_OK, Outbox, Shop, new_key, reset_provider
from sqlalchemy import func, select, text
from stripe_event_support import run_event
from torture_support import requires_postgres

import app.api.v1.orders as orders_module
import app.services.redis_async as redis_async_module
from app import tasks
from app.core.clock import utc_now
from app.core.config import settings
from app.main import app
from app.models.address import Address
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.payment import Payment, WebhookEvent
from app.models.payment_ledger import PaymentRefund
from app.models.product import Product, ProductVariant
from app.models.user import User
from app.services.payment.stripe_provider import StripePaymentProvider

# Concurrency torture: 20-50 simultaneous requests per burst, repeated over
# several rounds, against the real app on ONE event loop (httpx2 AsyncClient
# over ASGITransport, every request released by one asyncio.Barrier), on
# PostgreSQL. After each burst the database (and the fake Redis keyspace) is
# checked with a fresh session.
#
# Four families of races:
#   1. refresh-token rotation and reuse detection (and logout racing refresh);
#   2. Idempotency-Key races on the money-moving endpoints (POST /orders,
#      /pay, /refund): identical twins and twins with another payload;
#   3. last-unit stock contention with overlapping carts locked in different
#      orders (deadlock-prone);
#   4. GDPR erasure racing the same user's cart, orders, payments, webhooks,
#      refreshes, then the anonymisation task racing the webhook worker.
#
# Every scenario also asserts: no 500 anywhere, and every 4xx/5xx is
# application/problem+json. Tests named test_bug_* are deterministic
# regressions for a defect the random bursts can only hit by chance; the race
# window is widened from the test (a hook or a slow fake I/O call), never by
# editing app/.


USERS = "/api/v1/users"
CART = "/api/v1/cart"
ORDERS = "/api/v1/orders"
LEGAL = "/api/v1/legal"
WEBHOOK = f"{ORDERS}/webhooks/stripe"
PASSWORD = "Str0ngPass1"
PROBLEM_JSON = "application/problem+json"
TYPE_BASE = "https://"  # every problem type is an absolute URI

ROUNDS = 6
N = 30


# -- Fixtures ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def shop(client, db_session, monkeypatch):
    Outbox(monkeypatch)
    for name in (
        "send_gdpr_request_received",
        "send_gdpr_deletion_confirmation",
    ):
        monkeypatch.setattr(f"app.services.email.{name}", lambda *a, **k: True)
    return Shop(client, db_session)


# -- Harness -------------------------------------------------------------------


def _transport():
    # raise_app_exceptions=False: an unhandled error is answered 500 by the
    # app's own handler and counted here, instead of aborting the burst.
    return httpx2.ASGITransport(app=app, raise_app_exceptions=False)


async def _fire(requests):
    """Send every (method, url, kwargs) at once on this loop: each request
    waits on one barrier, so all of them enter the app together."""
    async with httpx2.AsyncClient(
        transport=_transport(), base_url="http://test", timeout=120
    ) as ac:
        barrier = asyncio.Barrier(len(requests))

        async def one(spec):
            method, url, kwargs = spec
            await barrier.wait()
            return await ac.request(method, url, **kwargs)

        return await asyncio.gather(*(one(r) for r in requests), return_exceptions=True)


def burst(requests):
    responses = asyncio.run(_fire(requests))
    errors = [r for r in responses if isinstance(r, BaseException)]
    assert not errors, f"transport errors: {errors!r}"
    return responses


def histogram(responses):
    return dict(sorted(collections.Counter(r.status_code for r in responses).items()))


def check_responses(responses, allowed):
    """No 5xx, nothing outside `allowed`, every error is problem+json."""
    hist = histogram(responses)
    bodies = {r.status_code: r.text[:300] for r in responses if r.status_code >= 500}
    assert not any(code >= 500 for code in hist), f"5xx in burst: {hist} {bodies}"
    assert set(hist) <= set(allowed), f"unexpected statuses {hist}, allowed {allowed}: " + str(
        {r.status_code: r.text[:200] for r in responses if r.status_code not in allowed}
    )
    for r in responses:
        if r.status_code >= 400:
            assert r.headers["content-type"].startswith(PROBLEM_JSON), (r.status_code, r.text)
            body = r.json()
            assert body["status"] == r.status_code
    print(f"HIST {hist}")  # shown with -s; the torture log quotes these
    return hist


def problem_type(response):
    return response.json().get("type", "").rsplit("/", 1)[-1]


def fresh():
    return TestingSessionLocal()


def login(shop, email):
    response = shop.client.post(f"{USERS}/login", data={"username": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def claims(token):
    return jwt.decode(token, options={"verify_signature": False})


def live_refresh_jtis(fake_redis, family):
    """jtis of the family that Redis would still redeem."""
    live = []
    for key in fake_redis.scan_iter("refresh_active:*"):
        value = fake_redis.get(key)
        if value and json.loads(value)["family"] == family:
            live.append(key.split(":", 1)[1])
    return live


def bearer(buyer):
    return buyer.headers


# -- 1. Refresh-token rotation -------------------------------------------------


@requires_postgres
@pytest.mark.parametrize("round_", range(ROUNDS))
def test_refresh_token_reuse_race_has_one_winner_and_burns_the_family(
    shop, fake_redis, round_
):
    """N concurrent /refresh with the same refresh token. Design
    (rotate_refresh_token): a refresh token is single-use; presenting a spent
    one is a theft signal that revokes the whole family. So: exactly one 200,
    N-1 401 problem+json, and - because the losers ARE reuses of a spent
    token - the winner's freshly rotated refresh token is revoked too (the
    family is burnt). Never two usable refresh tokens out of one."""
    buyer = shop.buyer()
    tokens = login(shop, buyer.email)
    family = claims(tokens["refresh_token"])["family"]

    responses = burst([
        ("POST", f"{USERS}/refresh", {"json": {"refresh_token": tokens["refresh_token"]}})
        for _ in range(N)
    ])

    hist = check_responses(responses, {200, 401})
    assert hist.get(200) == 1, hist
    winner = next(r for r in responses if r.status_code == 200).json()
    assert claims(winner["refresh_token"])["family"] == family
    # Reuse detection burnt the family: no jti of it is redeemable any more.
    assert live_refresh_jtis(fake_redis, family) == []
    again = shop.client.post(f"{USERS}/refresh", json={"refresh_token": winner["refresh_token"]})
    assert again.status_code == 401
    assert again.headers["content-type"].startswith(PROBLEM_JSON)
    # The original token is spent as well.
    replay = shop.client.post(f"{USERS}/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert replay.status_code == 401


@requires_postgres
@pytest.mark.parametrize("round_", range(ROUNDS))
def test_refresh_racing_logout_leaves_no_live_refresh_token(shop, fake_redis, round_):
    """Logout with the refresh token (revoke its family) racing N-1 refreshes
    of that same token. Whatever the interleaving, once logout has answered
    200 no refresh token of the family may still be redeemable: a token
    rotated while logout ran must not survive it."""
    buyer = shop.buyer()
    tokens = login(shop, buyer.email)
    family = claims(tokens["refresh_token"])["family"]
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    requests = [
        ("POST", f"{USERS}/refresh", {"json": {"refresh_token": tokens["refresh_token"]}})
        for _ in range(N - 1)
    ]
    requests.insert(N // 2, (
        "POST", f"{USERS}/logout",
        {"json": {"refresh_token": tokens["refresh_token"]}, "headers": headers},
    ))
    responses = burst(requests)

    check_responses(responses, {200, 401})
    logout = responses[N // 2]
    assert logout.status_code == 200, logout.text
    refreshed = [r.json()["refresh_token"] for i, r in enumerate(responses)
                 if i != N // 2 and r.status_code == 200]
    assert len(refreshed) <= 1
    assert live_refresh_jtis(fake_redis, family) == [], "a refresh token outlived logout"
    for token in refreshed:
        assert shop.client.post(
            f"{USERS}/refresh", json={"refresh_token": token}
        ).status_code == 401


class _SlowSmembersRedis(fakeredis.FakeAsyncRedis):
    """A fake async Redis whose SMEMBERS reply takes a little while to come
    back, as over a real network: widens the gap between revoke_refresh_family
    reading the family's members and deleting them."""

    delay = 0.2

    async def smembers(self, name):
        members = await super().smembers(name)
        if str(name).startswith("refresh_family:"):
            await asyncio.sleep(self.delay)
        return members


@requires_postgres
def test_bug_logout_racing_refresh_leaves_a_live_refresh_token(
    shop, fake_redis, monkeypatch
):
    """Deterministic regression for the interleaving the burst above can hit:

      logout:  SMEMBERS refresh_family:F -> {J1}     (slow reply)
      refresh: SET refresh_active:J2; SADD F J2; GET+DEL refresh_active:J1 -> ok
      logout:  DEL refresh_active:J1; DEL refresh_family:F
      => logout answers 200, and J2 (returned by refresh) is still redeemable.

    app/services/redis_async.py revoke_refresh_family reads the members and
    deletes them in two round trips with no atomicity, and
    rotate_refresh_token registers the new jti before redeeming the old one.
    """
    server = fake_redis.connection_pool.connection_kwargs["server"]
    monkeypatch.setattr(
        redis_async_module, "_create_client",
        lambda: _SlowSmembersRedis(server=server, decode_responses=True),
    )
    buyer = shop.buyer()
    tokens = login(shop, buyer.email)
    family = claims(tokens["refresh_token"])["family"]
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    async def scenario():
        async with httpx2.AsyncClient(transport=_transport(), base_url="http://test") as ac:
            logout = asyncio.create_task(ac.post(
                f"{USERS}/logout", json={"refresh_token": tokens["refresh_token"]},
                headers=headers,
            ))
            await asyncio.sleep(_SlowSmembersRedis.delay / 4)  # logout is inside SMEMBERS
            refresh = await ac.post(
                f"{USERS}/refresh", json={"refresh_token": tokens["refresh_token"]}
            )
            return await logout, refresh

    logout, refresh = asyncio.run(scenario())

    assert logout.status_code == 200, logout.text
    if refresh.status_code == 200:
        # The token handed out while logout ran must be dead once logout is done.
        survivor = refresh.json()["refresh_token"]
        reuse = shop.client.post(f"{USERS}/refresh", json={"refresh_token": survivor})
        assert reuse.status_code == 401, (
            f"refresh token rotated during logout survived the logout: {reuse.status_code}"
        )
        assert live_refresh_jtis(fake_redis, family) == []
    else:
        assert refresh.status_code == 401


# -- 2. Idempotency-Key races --------------------------------------------------


def _order_count(user_id):
    with fresh() as db:
        return db.scalar(select(func.count()).select_from(Order).where(Order.user_id == user_id))


@requires_postgres
@pytest.mark.parametrize("round_", range(ROUNDS))
def test_order_same_key_twins_create_one_order(shop, round_):
    """N identical POST /orders with one Idempotency-Key: one order, one stock
    decrement, and every answer the first one's replay (201, same id and
    Location)."""
    product = shop.product(price="10.00", stock=50)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product, quantity=2).status_code == 200
    key = new_key()

    responses = burst([
        ("POST", ORDERS, {"json": {"shipping_address_id": buyer.address_id},
                          "headers": {**buyer.headers, "Idempotency-Key": key}})
        for _ in range(N)
    ])

    hist = check_responses(responses, {201})
    assert hist == {201: N}, hist
    ids = {r.json()["id"] for r in responses}
    assert len(ids) == 1, ids
    assert len({r.headers["location"] for r in responses}) == 1
    assert _order_count(buyer.user_id) == 1
    assert shop.stock(product) == 48
    with fresh() as db:
        assert db.scalar(select(func.count()).select_from(OrderItem)) == 1


@requires_postgres
@pytest.mark.parametrize("round_", range(ROUNDS))
def test_order_same_key_different_address_executes_once(shop, round_):
    """Same key, two different payloads (shipping addresses), N/2 each: one
    order; the twins of the winner's payload replay it (201), the others are
    answered 422 idempotency-key-reused."""
    product = shop.product(price="10.00", stock=50)
    buyer = shop.buyer()
    other = Address(user_id=buyer.user_id, street="2 Side Street", city="Gotham",
                    state="NJ", postal_code="07001", country="US")
    shop.db.add(other)
    shop.db.commit()
    assert shop.add_to_cart(buyer, product, quantity=1).status_code == 200
    key = new_key()
    addresses = [buyer.address_id, other.id]

    responses = burst([
        ("POST", ORDERS, {"json": {"shipping_address_id": addresses[i % 2]},
                          "headers": {**buyer.headers, "Idempotency-Key": key}})
        for i in range(N)
    ])

    check_responses(responses, {201, 422})
    assert _order_count(buyer.user_id) == 1
    assert shop.stock(product) == 49
    with fresh() as db:
        order = db.scalars(select(Order).where(Order.user_id == buyer.user_id)).one()
    for i, r in enumerate(responses):
        if addresses[i % 2] == order.shipping_address_id:
            assert r.status_code == 201, r.text
            assert r.json()["id"] == order.id
        else:
            assert r.status_code == 422, r.text
            assert problem_type(r) == "idempotency-key-reused"


def test_bug_order_replay_racing_its_first_request_answers_cart_empty(shop, monkeypatch):
    """A retry of POST /orders with the key of a request still in flight.
    Interleaving (app/api/v1/orders.py create_order):

      retry:  _order_by_idempotency_key -> None   (first not committed yet)
      first:  commit; remove_ordered_items_atomic  (cart now empty)
      retry:  redis.get_cart -> empty -> 400 "Cart is empty"

    The documented answer to a reused key is the first order (201). Widened
    here by holding the retry right after its key lookup until the first
    request has finished."""
    product = shop.product(price="10.00", stock=50)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product, quantity=1).status_code == 200
    key = new_key()
    real_lookup = orders_module._order_by_idempotency_key
    state = {"held": False}

    async def scenario():
        retry_paused = asyncio.Event()
        first_done = asyncio.Event()

        async def lookup(db, user_id, idempotency_key):
            found = await real_lookup(db, user_id, idempotency_key)
            if found is None and not state["held"]:
                state["held"] = True
                retry_paused.set()
                await first_done.wait()
            return found

        monkeypatch.setattr(orders_module, "_order_by_idempotency_key", lookup)
        request = {"json": {"shipping_address_id": buyer.address_id},
                   "headers": {**buyer.headers, "Idempotency-Key": key}}
        async with httpx2.AsyncClient(transport=_transport(), base_url="http://test") as ac:
            retry = asyncio.create_task(ac.post(ORDERS, **request))
            await retry_paused.wait()
            first = await ac.post(ORDERS, **request)
            first_done.set()
            return first, await retry

    first, retry = asyncio.run(scenario())

    assert first.status_code == 201, first.text
    assert _order_count(buyer.user_id) == 1
    assert retry.status_code == 201, (
        f"a replay of the in-flight key answered {retry.status_code}: {retry.text}"
    )
    assert retry.json()["id"] == first.json()["id"]


def _unpaid_order(shop, price="19.99"):
    product = shop.product(price=price, stock=100)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    return buyer, order


def _pay_body(order, token=PAY_OK, amount=None):
    return {"order_id": order["id"], "payment_method": "stripe",
            "amount": str(amount if amount is not None else order["total_amount"]),
            "payment_method_token": token}


def _payments(order_id):
    with fresh() as db:
        return db.scalars(select(Payment).where(Payment.order_id == order_id)
                          .order_by(Payment.id)).all()


@requires_postgres
@pytest.mark.parametrize("round_", range(ROUNDS))
def test_pay_same_key_twins_charge_once(shop, round_):
    """N identical /pay with one key: one attempt, one PaymentIntent, the
    order paid once; every answer is the replay (200 paid) or 409
    idempotency-key-in-use while the first is in flight."""
    buyer, order = _unpaid_order(shop)
    key = new_key()

    responses = burst([
        ("POST", f"{ORDERS}/{order['id']}/pay",
         {"json": _pay_body(order), "headers": {**buyer.headers, "Idempotency-Key": key}})
        for _ in range(N)
    ])

    hist = check_responses(responses, {200, 409})
    assert hist.get(200, 0) >= 1, hist
    for r in responses:
        if r.status_code == 409:
            assert problem_type(r) == "idempotency-key-in-use", r.text
        else:
            assert r.json()["status"] == "paid"
    payments = _payments(order["id"])
    assert [(p.status, p.requires_manual_review) for p in payments] == [("succeeded", False)]
    assert len({r.json()["id"] for r in responses if r.status_code == 200}) == 1
    assert len(StripePaymentProvider._intents_by_id) == 1
    assert shop.order(order["id"]).payment_status == "paid"


@requires_postgres
@pytest.mark.parametrize("round_", range(ROUNDS))
def test_pay_same_key_different_bodies_execute_once(shop, round_):
    """Same key, two bodies (two cards): one attempt, one charge; the other
    body's requests are 422 idempotency-key-reused, never a second charge."""
    buyer, order = _unpaid_order(shop)
    key = new_key()
    tokens = [PAY_OK, "pm_card_mastercard"]

    responses = burst([
        ("POST", f"{ORDERS}/{order['id']}/pay",
         {"json": _pay_body(order, token=tokens[i % 2]),
          "headers": {**buyer.headers, "Idempotency-Key": key}})
        for i in range(N)
    ])

    check_responses(responses, {200, 409, 422})
    payments = _payments(order["id"])
    assert len(payments) == 1
    assert payments[0].status == "succeeded"
    assert len(StripePaymentProvider._intents_by_id) == 1
    winner_token = StripePaymentProvider._intents_by_id[payments[0].payment_intent_id].payment_method
    for i, r in enumerate(responses):
        if tokens[i % 2] != winner_token:
            assert r.status_code == 422, r.text
            assert problem_type(r) == "idempotency-key-reused"
        else:
            assert r.status_code in (200, 409), r.text


@requires_postgres
@pytest.mark.parametrize("round_", range(ROUNDS))
def test_pay_distinct_keys_charge_once(shop, round_):
    """N /pay with N different keys (N tabs): the order lock and the
    in-flight guard admit one attempt; the rest are 409 (another attempt in
    progress / already paid). One charge."""
    buyer, order = _unpaid_order(shop)

    responses = burst([
        ("POST", f"{ORDERS}/{order['id']}/pay",
         {"json": _pay_body(order), "headers": {**buyer.headers, "Idempotency-Key": new_key()}})
        for _ in range(N)
    ])

    hist = check_responses(responses, {200, 409})
    assert hist.get(200) == 1, hist
    payments = _payments(order["id"])
    assert [p.status for p in payments] == ["succeeded"]
    assert len(StripePaymentProvider._intents_by_id) == 1


def _paid_order(shop, price="19.99"):
    buyer, order = _unpaid_order(shop, price=price)
    paid = shop.pay(buyer, order["id"])
    assert paid.status_code == 200, paid.text
    return buyer, order, shop.admin()


def _refunds(order_id):
    with fresh() as db:
        return db.scalars(
            select(PaymentRefund).join(Payment).where(Payment.order_id == order_id)
            .order_by(PaymentRefund.id)
        ).all()


@requires_postgres
@pytest.mark.parametrize("round_", range(ROUNDS))
def test_refund_same_key_twins_refund_once(shop, round_):
    """N identical full refunds with one key: one provider refund, one
    PaymentRefund row, amount_refunded == the charge once, order refunded;
    every answer names the same refund (or is a 409 in-use)."""
    buyer, order, admin = _paid_order(shop)
    key = new_key()

    responses = burst([
        ("POST", f"{ORDERS}/{order['id']}/refund",
         {"json": {}, "headers": {**admin, "Idempotency-Key": key}})
        for _ in range(N)
    ])

    check_responses(responses, {200, 409})
    ok = [r.json() for r in responses if r.status_code == 200]
    assert ok and len({r["id"] for r in ok}) == 1, histogram(responses)
    refunds = _refunds(order["id"])
    assert len(refunds) == 1
    payment = _payments(order["id"])[0]
    assert payment.amount_refunded == payment.amount == refunds[0].amount
    assert len(StripePaymentProvider._refunds_by_id) == 1
    assert shop.order(order["id"]).payment_status == "refunded"


@requires_postgres
@pytest.mark.parametrize("round_", range(ROUNDS))
def test_refund_same_key_different_amounts_execute_once(shop, round_):
    """Same key, two partial amounts: one refund; the other amount's requests
    are refused (409 idempotency-key-in-use from the provider's key check)."""
    buyer, order, admin = _paid_order(shop)
    key = new_key()
    amounts = ["1.00", "2.00"]

    responses = burst([
        ("POST", f"{ORDERS}/{order['id']}/refund",
         {"json": {"amount": amounts[i % 2]}, "headers": {**admin, "Idempotency-Key": key}})
        for i in range(N)
    ])

    check_responses(responses, {200, 409})
    refunds = _refunds(order["id"])
    assert len(refunds) == 1
    payment = _payments(order["id"])[0]
    assert payment.amount_refunded == refunds[0].amount
    assert len(StripePaymentProvider._refunds_by_id) == 1
    winner = f"{refunds[0].amount / 100:.2f}"
    for i, r in enumerate(responses):
        assert r.status_code == (200 if amounts[i % 2] == winner else 409), r.text


@requires_postgres
@pytest.mark.parametrize("round_", range(ROUNDS))
def test_refund_distinct_keys_never_over_refund(shop, round_):
    """N partial refunds of 1.00 with distinct keys on a 19.99 charge: the
    provider refuses what exceeds the charge; what the provider refunded is
    exactly what is recorded, never more than the charge, no 500 (the
    refunded <= amount CHECK)."""
    buyer, order, admin = _paid_order(shop)

    responses = burst([
        ("POST", f"{ORDERS}/{order['id']}/refund",
         {"json": {"amount": "1.00"}, "headers": {**admin, "Idempotency-Key": new_key()}})
        for _ in range(N)
    ])

    hist = check_responses(responses, {200, 400})
    assert hist.get(200) == 19, hist
    refunds = _refunds(order["id"])
    payment = _payments(order["id"])[0]
    assert len(refunds) == 19
    assert payment.amount_refunded == sum(r.amount for r in refunds) == 1900
    provider_total = sum(r.amount for r in StripePaymentProvider._refunds_by_id.values())
    assert provider_total == 1900
    assert {r.json()["id"] for r in responses if r.status_code == 200} == {
        r.refund_id for r in refunds}


class _SlowLookupDict(dict):
    """The mock provider's PaymentIntent store, answering a little slowly (as
    Stripe's own servers would take time between checking a key and
    recording the refund)."""

    def get(self, key, default=None):
        time.sleep(0.05)
        return super().get(key, default)


@requires_postgres
def test_bug_mock_provider_refund_key_is_not_atomic(shop, monkeypatch):
    """POST /refund relies on the provider alone for the Idempotency-Key
    (the key is not stored locally). The in-process mock
    (app/services/payment/stripe_provider.py create_refund) checks
    _refunds_by_key and records the new refund later, with no lock, while
    the route calls it from a thread pool: two concurrent requests with the
    same key both miss the cache and both refund. Real Stripe answers the
    second with an idempotency error (409). Seen without any hook in the
    bursts above (2 of 36 rounds); widened here by a slow intent lookup
    between the key check and the insert."""
    buyer, order, admin = _paid_order(shop)
    monkeypatch.setattr(StripePaymentProvider, "_intents_by_id",
                        _SlowLookupDict(StripePaymentProvider._intents_by_id))
    key = new_key()

    responses = burst([
        ("POST", f"{ORDERS}/{order['id']}/refund",
         {"json": {"amount": "1.00"}, "headers": {**admin, "Idempotency-Key": key}})
        for _ in range(5)
    ])

    check_responses(responses, {200, 409})
    assert len(_refunds(order["id"])) == 1, "one Idempotency-Key, several refunds"
    assert len(StripePaymentProvider._refunds_by_id) == 1


@requires_postgres
@pytest.mark.parametrize("round_", range(ROUNDS))
def test_webhook_redeliveries_and_workers_apply_once(shop, round_):
    """N deliveries of one Stripe event at once (at-least-once delivery),
    then 10 workers handed the same event at once: one stored row, applied
    once (one status email, the attempt succeeded once)."""
    product = shop.product(price="12.00", stock=50)
    buyer = shop.buyer()
    order, attempt = _requires_action_attempt(shop, buyer, product)
    outbox = Outbox.__new__(Outbox)
    outbox.sent = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.api.v1.orders.send_order_status_update_email",
                   outbox._recorder("status_update"))
        body, headers = _intent_event(attempt.payment_intent_id, order["id"], attempt.amount,
                                      attempt.currency, buyer.email)
        responses = burst([("POST", WEBHOOK, {"content": body, "headers": headers})
                           for _ in range(N)])
        check_responses(responses, {200})
        acks = collections.Counter(r.json()["status"] for r in responses)
        assert acks == {"received": 1, "already_processed": N - 1}, acks
        event_id = json.loads(body)["id"]
        with ThreadPoolExecutor(max_workers=10) as pool:
            outcomes = list(pool.map(lambda _: run_event(event_id), range(10)))
    applied = [o for o in outcomes if o.get("status") == "processed"]
    assert len(applied) == 1, outcomes
    assert all(o.get("status") in ("processed", "skipped") for o in outcomes), outcomes
    assert [kind for kind, *_ in outbox.sent] == ["status_update"]
    with fresh() as db:
        assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 1
        assert db.get(Order, order["id"]).payment_status == "paid"
    assert [p.status for p in _payments(order["id"])] == ["succeeded"]


@requires_postgres
@pytest.mark.parametrize("round_", range(ROUNDS))
def test_concurrent_cancellations_restock_once(shop, round_):
    """The customer's /cancel and the admin's status change to cancelled,
    N at once on one unpaid order: one succeeds, the stock comes back once."""
    product = shop.product(price="5.00", stock=10)
    variant = shop.variant(product, stock=10)
    buyer = shop.buyer()
    admin = shop.admin()
    assert shop.add_to_cart(buyer, product, quantity=2).status_code == 200
    assert shop.add_to_cart(buyer, product, quantity=3, variant=variant).status_code == 200
    placed = shop.place_order(buyer)
    assert placed.status_code == 201, placed.text
    order_id = placed.json()["id"]
    assert (shop.stock(product), shop.stock(variant)) == (8, 7)

    requests = []
    for i in range(N):
        if i % 2:
            requests.append(("POST", f"{ORDERS}/{order_id}/cancel", {"headers": buyer.headers}))
        else:
            requests.append(("PUT", f"{ORDERS}/{order_id}/status",
                             {"json": {"status": "cancelled"}, "headers": admin}))
    responses = burst(requests)

    hist = check_responses(responses, {200, 400, 409})
    assert hist.get(200) == 1, hist
    assert (shop.stock(product), shop.stock(variant)) == (10, 10)
    assert shop.order(order_id).status == "cancelled"


# -- 3. Stock contention --------------------------------------------------------


def _stock_rows():
    with fresh() as db:
        products = {("P", p.id): p.stock_quantity for p in db.scalars(select(Product))}
        variants = {("V", v.id): v.stock_quantity for v in db.scalars(select(ProductVariant))}
    return {**products, **variants}


def _ordered_by_line():
    with fresh() as db:
        rows = db.execute(select(OrderItem.product_id, OrderItem.variant_id,
                                 func.sum(OrderItem.quantity))
                          .group_by(OrderItem.product_id, OrderItem.variant_id)).all()
    totals = collections.Counter()
    for product_id, variant_id, qty in rows:
        totals[("V", variant_id) if variant_id is not None else ("P", product_id)] += qty
    return totals


def _lines_of_orders():
    with fresh() as db:
        orders = db.scalars(select(Order)).all()
        result = {}
        for order in orders:
            items = db.scalars(select(OrderItem).where(OrderItem.order_id == order.id)).all()
            result[order.user_id] = (order, sorted(
                (i.product_id, i.variant_id or 0, i.quantity) for i in items))
    return result


def _checkout_burst(shop, carts):
    """carts: [(buyer, [(product, variant|None, qty), ...])], each cart filled
    in its own line order; then every buyer checks out at once."""
    for buyer, lines in carts:
        for product, variant, qty in lines:
            added = shop.add_to_cart(buyer, product, quantity=qty, variant=variant)
            assert added.status_code == 200, added.text
    return burst([
        ("POST", ORDERS, {"json": {"shipping_address_id": buyer.address_id},
                          "headers": {**buyer.headers, "Idempotency-Key": new_key()}})
        for buyer, _ in carts
    ])


def _assert_stock_invariants(carts, responses, initial):
    final = _stock_rows()
    ordered = _ordered_by_line()
    by_user = _lines_of_orders()
    for key, start in initial.items():
        assert final[key] >= 0, f"oversold {key}: {final[key]}"
        assert start - final[key] == ordered.get(key, 0), (key, start, final[key], ordered)
    for (buyer, lines), r in zip(carts, responses, strict=True):
        want = sorted((p.id, v.id if v else 0, q) for p, v, q in lines)
        if r.status_code == 201:
            order, got = by_user[buyer.user_id]
            assert r.json()["id"] == order.id
            assert got == want, "partial order"
        elif r.status_code >= 500:
            # Checked by check_responses afterwards; here only: all or nothing.
            assert buyer.user_id not in by_user, "a failed checkout left an order behind"
        else:
            assert r.status_code == 409, r.text
            assert problem_type(r) == "insufficient-stock", r.text
            assert buyer.user_id not in by_user, "a refused checkout left an order behind"
            # Stock only goes down in this burst: a refusal is only right
            # if some line is short even now.
            assert any(
                final[("V", v.id) if v else ("P", p.id)] < q for p, v, q in lines
            ), f"spurious 409 for {want} with final stock {final}"
    assert len(by_user) == sum(1 for r in responses if r.status_code == 201)


@requires_postgres
@pytest.mark.parametrize("round_", range(8))
def test_last_unit_contention_never_oversells_or_deadlocks(shop, round_):
    """40 buyers, overlapping 2-3 line carts over stocks of 1-3 (plain lines
    and variant lines, the same product both ways), each cart filled in a
    random line order (A,B vs B,A), all checking out at once."""
    rng = random.Random(1000 + round_)
    p1 = shop.product(price="5.00", stock=1, name="P1")
    p2 = shop.product(price="6.00", stock=2, name="P2")
    p3 = shop.product(price="7.00", stock=3, name="P3")
    p4 = shop.product(price="8.00", stock=1, name="P4")
    v4a = shop.variant(p4, stock=1)
    v4b = shop.variant(p4, stock=2)
    p5 = shop.product(price="9.00", stock=2, name="P5")
    pool = [(p1, None), (p2, None), (p3, None), (p4, None), (p4, v4a), (p4, v4b), (p5, None)]
    initial = _stock_rows()
    carts = []
    for _ in range(40):
        picks = rng.sample(pool, rng.choice([2, 3]))
        lines = []
        for product, variant in picks:
            stock = (variant or product).stock_quantity
            lines.append((product, variant, rng.choice([1, 1, 2]) if stock >= 2 else 1))
        carts.append((shop.buyer(), lines))

    responses = _checkout_burst(shop, carts)

    # Stock invariants first, so that they are checked even in a round
    # where a deadlock 503 fails the burst.
    _assert_stock_invariants(carts, responses, initial)
    assert any(r.status_code == 201 for r in responses)
    check_responses(responses, {201, 409})


@requires_postgres
@pytest.mark.parametrize("round_", range(ROUNDS))
def test_plain_and_variant_lines_of_one_product_do_not_deadlock(shop, round_):
    """Plenty of stock, so every checkout must succeed. Half the carts hold
    a variant of P only, half hold P itself AND that variant. lock_stock_rows
    promises consistent lock ordering (products by id, then variants by id)
    so checkouts cannot deadlock (SQLSTATE 40P01); a deadlock surfaces as a
    503 'database temporarily unavailable'."""
    p = shop.product(price="5.00", stock=1000, name="P")
    v = shop.variant(p, stock=1000)
    q = shop.product(price="6.00", stock=1000, name="Q")
    initial = _stock_rows()
    carts = []
    for i in range(N):
        if i % 2:
            lines = [(p, v, 1)] if i % 4 == 1 else [(p, v, 1), (q, None, 1)]
        else:
            lines = [(p, None, 1), (p, v, 1)] if i % 4 == 0 else [(p, v, 1), (p, None, 1)]
        carts.append((shop.buyer(), lines))

    responses = _checkout_burst(shop, carts)

    _assert_stock_invariants(carts, responses, initial)
    hist = check_responses(responses, {201})
    assert hist == {201: N}, hist


@requires_postgres
def test_bug_variant_line_checkout_deadlocks_with_plain_line_checkout(shop, monkeypatch):
    """Deterministic two-transaction version of the deadlock above.

      A (cart {V of P}):    lock_stock_rows -> V FOR UPDATE; UPDATE V ...
      B (cart {P, V}):      lock_stock_rows -> P FOR UPDATE; V FOR UPDATE (waits on A)
      A commit:             INSERT order_items(product_id=P) -> FK check takes
                            FOR KEY SHARE on P (waits on B's FOR UPDATE)  => 40P01

    lock_stock_rows (app/services/orders.py) locks only the rows whose stock
    changes, with FOR UPDATE, which conflicts with the FOR KEY SHARE that the
    order_items -> products foreign key takes on the product of a variant
    line at commit. Each request runs on its own thread and loop; A is held
    right after its locks until B is seen waiting for a lock."""
    p = shop.product(price="5.00", stock=100, name="P")
    v = shop.variant(p, stock=100)
    a, b = shop.buyer(), shop.buyer()
    assert shop.add_to_cart(a, p, variant=v).status_code == 200
    assert shop.add_to_cart(b, p).status_code == 200
    assert shop.add_to_cart(b, p, variant=v).status_code == 200

    real_lock = orders_module.lock_stock_rows
    a_locked = threading.Event()
    release_a = threading.Event()

    def lock(db, lines):
        real_lock(db, lines)
        if list(lines) == [(p.id, v.id)] and not a_locked.is_set():
            a_locked.set()
            release_a.wait(timeout=20)

    monkeypatch.setattr(orders_module, "lock_stock_rows", lock)
    results = {}

    def checkout(name, buyer):
        async def go():
            async with httpx2.AsyncClient(transport=_transport(), base_url="http://test",
                                          timeout=60) as ac:
                return await ac.post(ORDERS, json={"shipping_address_id": buyer.address_id},
                                     headers={**buyer.headers, "Idempotency-Key": new_key()})
        results[name] = asyncio.run(go())

    ta = threading.Thread(target=checkout, args=("a", a))
    tb = threading.Thread(target=checkout, args=("b", b))
    ta.start()
    assert a_locked.wait(timeout=20)
    tb.start()
    deadline = time.monotonic() + 10
    with fresh() as db:
        while time.monotonic() < deadline:
            waiting = db.scalar(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ))
            db.rollback()
            if waiting:
                break
            time.sleep(0.02)
    release_a.set()
    ta.join(timeout=60)
    tb.join(timeout=60)

    statuses = {name: (r.status_code, r.text[:200]) for name, r in results.items()}
    assert results["a"].status_code == 201 and results["b"].status_code == 201, statuses


# -- 4. GDPR erasure racing the account's own activity --------------------------

PII_NAME = "Jane Piiholder"


def _intent_event(intent_id, order_id, amount, currency, email, event_id=None):
    """A payment_intent.succeeded carrying the personal data Stripe events
    carry (receipt_email, billing details on the charge)."""
    obj = {
        "id": intent_id, "object": "payment_intent",
        "amount": amount, "amount_received": amount, "currency": currency.lower(),
        "status": "succeeded",
        "receipt_email": email,
        "metadata": {"order_id": str(order_id)},
        "charges": {"data": [{"billing_details": {"name": PII_NAME, "email": email}}]},
    }
    body = json.dumps({
        "id": event_id or f"evt_{uuid.uuid4().hex[:24]}", "object": "event",
        "type": "payment_intent.succeeded", "created": int(time.time()),
        "data": {"object": obj},
    }).encode()
    signature = StripePaymentProvider.sign_payload(
        body, settings.STRIPE_WEBHOOK_SECRET.get_secret_value())
    return body, {"Stripe-Signature": signature, "Content-Type": "application/json"}


def _requires_action_attempt(shop, buyer, product):
    order = shop.order_of(buyer, product)
    response = shop.pay(buyer, order["id"], token=PAY_3DS)
    assert response.status_code == 200, response.text
    payment = shop.payments(order["id"])[-1]
    return order, payment


def _pii_left(email, phone="+15551234567"):
    """Every place the buyer's personal data could still be stored."""
    found = []
    with fresh() as db:
        for event in db.scalars(select(WebhookEvent)):
            if event.payload and (email in event.payload or PII_NAME in event.payload):
                found.append(("webhook_events", event.event_id))
        if db.scalar(select(func.count()).select_from(User).where(
                (User.email == email) | (User.phone == phone) | (User.full_name == "Buyer One"))):
            found.append(("users", email))
        if db.scalar(select(func.count()).select_from(Address).where(
                Address.street != "REDACTED")):
            found.append(("addresses", "unredacted"))
    return found


def _backdate_erasure(user_id):
    with fresh() as db:
        user = db.get(User, user_id)
        user.data_deletion_date = utc_now() - timedelta(
            days=settings.DATA_DELETION_GRACE_PERIOD_DAYS + 1)
        db.commit()


def _purge():
    with fresh() as db:
        return tasks.purge_expired_deletion_requests(db)


@requires_postgres
@pytest.mark.parametrize("round_", range(5))
def test_erasure_racing_the_accounts_activity(shop, fake_redis, round_):
    """One erasure request in a burst of 30 requests of the same account:
    cart adds, checkouts, a /pay, refreshes, /me, and webhook deliveries for
    its orders (with PII in the payload). Then the anonymisation task runs
    concurrently with the webhook worker applying the stored events.

    Afterwards: no 500; the account is deactivated and every token refused;
    after the purge no personal data is left (user row, addresses - also any
    created during the race - and the stored webhook payloads)."""
    product = shop.product(price="12.00", stock=500)
    buyer = shop.buyer()
    order_3ds, attempt = _requires_action_attempt(shop, buyer, product)
    unpaid = shop.order_of(buyer, product)
    sessions = [login(shop, buyer.email) for _ in range(4)]
    assert shop.add_to_cart(buyer, product, quantity=1).status_code == 200
    events = [_intent_event(attempt.payment_intent_id, order_3ds["id"], attempt.amount,
                            attempt.currency, buyer.email) for _ in range(2)]
    dup_body, dup_headers = events[0]

    requests = [("POST", f"{LEGAL}/data-request",
                 {"json": {"request_type": "deletion", "password": PASSWORD},
                  "headers": buyer.headers})]
    requests += [("POST", f"{CART}/items", {"json": {"product_id": product.id, "quantity": 1},
                                            "headers": buyer.headers}) for _ in range(8)]
    requests += [("POST", ORDERS, {"json": {"shipping_address_id": buyer.address_id},
                                   "headers": {**buyer.headers, "Idempotency-Key": new_key()}})
                 for _ in range(4)]
    requests += [("POST", f"{ORDERS}/{unpaid['id']}/pay",
                  {"json": _pay_body(unpaid), "headers": {**buyer.headers,
                                                         "Idempotency-Key": new_key()}})
                 for _ in range(2)]
    requests += [("POST", f"{USERS}/refresh", {"json": {"refresh_token": s["refresh_token"]}})
                 for s in sessions]
    requests += [("GET", f"{USERS}/me", {"headers": buyer.headers}) for _ in range(4)]
    requests += [("POST", f"{USERS}/addresses", {
        "json": {"street": "9 Race Road", "city": "Raceville", "state": "CA",
                 "postal_code": "90001", "country": "US"},
        "headers": buyer.headers}) for _ in range(2)]
    requests += [("POST", WEBHOOK, {"content": body, "headers": headers})
                 for body, headers in events]
    requests += [("POST", WEBHOOK, {"content": dup_body, "headers": dup_headers})
                 for _ in range(3)]
    order = list(range(len(requests)))
    random.Random(round_).shuffle(order)
    shuffled = [requests[i] for i in order]

    responses = burst(shuffled)

    check_responses(responses, {200, 201, 400, 401, 403, 409})
    erasure = responses[order.index(0)]
    assert erasure.status_code == 200, erasure.text
    with fresh() as db:
        user = db.get(User, buyer.user_id)
        assert (user.is_active, user.data_deletion_requested) == (False, True)

    # Every token the account ever got - including ones a racing refresh
    # handed out - is refused now.
    handed_out = [s["refresh_token"] for s in sessions] + [
        r.json()["refresh_token"] for r in responses
        if r.request.url.path.endswith("/refresh") and r.status_code == 200]
    for token in handed_out:
        assert shop.client.post(f"{USERS}/refresh",
                                json={"refresh_token": token}).status_code == 401
    me = shop.client.get(f"{USERS}/me", headers=buyer.headers)
    assert me.status_code == 403 and problem_type(me) == "account-inactive"
    assert shop.client.post(f"{USERS}/login", data={
        "username": buyer.email, "password": PASSWORD}).status_code == 401

    # The grace period is over: the anonymisation task runs while the
    # webhook worker applies the stored events.
    _backdate_erasure(buyer.user_id)
    with fresh() as db:
        stored = [e.event_id for e in db.scalars(select(WebhookEvent))]
    assert len(stored) == 2
    outcome = {}
    workers = [threading.Thread(target=lambda e=e: outcome.setdefault(e, run_event(e)))
               for e in stored * 2]
    purger = threading.Thread(target=lambda: outcome.setdefault("purge", _purge()))
    for t in [*workers[:2], purger, *workers[2:]]:
        t.start()
    for t in [*workers, purger]:
        t.join(timeout=60)
    assert outcome["purge"] == 1
    # Events still pending (a worker skipped a locked one) are applied now.
    for event_id in stored:
        run_event(event_id)
    with fresh() as db:
        statuses = {e.status for e in db.scalars(select(WebhookEvent))}
        assert statuses == {"processed"}, statuses
        assert db.get(Order, order_3ds["id"]).payment_status == "paid"
        anonymised = db.get(User, buyer.user_id)
        assert anonymised.email == f"deleted-user-{buyer.user_id}@deleted.invalid"
        assert anonymised.full_name is None and anonymised.phone is None
    assert _pii_left(buyer.email) == []


def test_bug_webhook_stored_after_erasure_keeps_personal_data(shop):
    """The anonymisation task redacts the stored webhook events about the
    erased account's payments (redact_users_events), once: the account is
    then excluded from every later run. An event about those payments that
    is stored after that run - a late redelivery, a refund or dispute
    months later, or one that simply raced the task - keeps its personal
    data forever: neither the webhook route (app/api/v1/orders.py
    stripe_webhook -> stripe_events.store_event) nor the worker redacts it."""
    product = shop.product(price="12.00", stock=50)
    buyer = shop.buyer()
    order, attempt = _requires_action_attempt(shop, buyer, product)
    request = shop.client.post(f"{LEGAL}/data-request",
                               json={"request_type": "deletion", "password": PASSWORD},
                               headers=buyer.headers)
    assert request.status_code == 200, request.text
    _backdate_erasure(buyer.user_id)
    assert _purge() == 1
    assert _pii_left(buyer.email) == []

    body, headers = _intent_event(attempt.payment_intent_id, order["id"], attempt.amount,
                                  attempt.currency, buyer.email)
    response = shop.client.post(WEBHOOK, content=body, headers=headers)
    assert response.status_code == 200
    run_event(json.loads(body)["id"])
    # A later run of the task does not pick it up either.
    assert _purge() == 0

    assert _pii_left(buyer.email) == [], "an erased customer's data was stored after erasure"


def test_harness_runs_requests_concurrently_on_one_loop(shop):
    """Sanity check of the harness itself (also on SQLite): a burst's
    requests overlap in time on one event loop."""
    inside = {"now": 0, "max": 0}
    real = orders_module._order_by_idempotency_key

    async def counting(db, user_id, key):
        inside["now"] += 1
        inside["max"] = max(inside["max"], inside["now"])
        await asyncio.sleep(0.05)
        inside["now"] -= 1
        return await real(db, user_id, key)

    product = shop.product(stock=5)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product).status_code == 200
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(orders_module, "_order_by_idempotency_key", counting)
        responses = burst([
            ("POST", ORDERS, {"json": {"shipping_address_id": 999999},
                              "headers": {**buyer.headers, "Idempotency-Key": new_key()}})
            for _ in range(20)
        ])
    check_responses(responses, {404})
    assert inside["max"] >= 10, inside
