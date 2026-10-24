"""The Redis behaviour the application relies on, on fakeredis and on a real server.

fakeredis is an emulator: it is fast and needs nothing, and the rest of the
suite runs on it. What this module checks is that the pieces whose meaning
lives in the server (the rate-limit Lua script, expiry, SET NX, WATCH/MULTI
with a competing writer, the stored JSON) behave the same on the real thing.
Every test runs twice:

    backend=fakeredis   always, like the rest of the suite
    backend=real        only when REAL_REDIS_URL names a server, e.g.
                        REAL_REDIS_URL=redis://localhost:6379/0
                        (marker real_redis: `pytest -m real_redis` selects
                        exactly the real runs; CI job test-redis does)

A difference between the two is a finding about fakeredis or about the code,
and shows as one failing parametrization.

The real run starts and ends with FLUSHDB on the database in the URL (under
xdist, on the database numbered from it upwards, one per worker), so it must
be a throwaway server (the CI service container, a docker run). It refuses
anything but a loopback host.
"""

import asyncio
import json
import os
import threading
import time
from decimal import Decimal
from urllib.parse import urlparse

import pytest
import redis
import redis.asyncio

import app.services.redis_async as redis_async
import app.services.redis_codec as codec
from app.core import rate_limit
from app.core.rate_limit import Rate
from app.services.redis import RedisService
from app.services.redis_async import AsyncRedisService, is_token_blacklisted, is_token_superseded
from app.services.redis_codec import InsufficientStockError

REAL_REDIS_URL = os.environ.get("REAL_REDIS_URL") or None

API_PREFIX = "/api/v1/users"


def _worker_url(url):
    """Under pytest-xdist each worker gets its own database number (the URL's
    plus the worker's index; a server has 16 by default), so one worker's
    FLUSHDB cannot clear another's keys."""
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker is None:
        return url
    parsed = urlparse(url)
    base = int(parsed.path.lstrip("/") or 0)
    return parsed._replace(path=f"/{base + int(worker.removeprefix('gw'))}").geturl()


@pytest.fixture(
    params=[
        "fakeredis",
        pytest.param(
            "real",
            marks=[
                pytest.mark.real_redis,
                pytest.mark.skipif(
                    REAL_REDIS_URL is None, reason="set REAL_REDIS_URL=redis://host:port/db to run on a real Redis"
                ),
            ],
        ),
    ]
)
def backend(request, fake_redis, monkeypatch):
    """The Redis the application talks to in this test: the autouse fakeredis
    from tests/conftest.py, or the real server swapped in at the same two
    seams (the sync client RedisService builds, the async client factory)."""
    if request.param == "fakeredis":
        yield "fakeredis"
        return

    assert urlparse(REAL_REDIS_URL).hostname in ("localhost", "127.0.0.1", "::1"), (
        "REAL_REDIS_URL is flushed: loopback only"
    )
    url = _worker_url(REAL_REDIS_URL)
    sync_client = redis.Redis.from_url(url, decode_responses=True)
    sync_client.flushdb()
    monkeypatch.setattr("app.services.redis.Redis", lambda *a, **k: sync_client)
    RedisService()
    monkeypatch.setattr(
        redis_async,
        "_create_client",
        lambda: redis.asyncio.Redis.from_url(url, decode_responses=True),
    )
    monkeypatch.setattr(redis_async, "_clients", {})
    try:
        yield "real"
    finally:
        sync_client.flushdb()
        sync_client.close()


@pytest.fixture
def raw(backend):
    """The sync client on the keyspace the services use: what is stored."""
    return RedisService()._redis


def arun(coro):
    """Run a coroutine on a fresh loop and close that loop's Redis client
    before the loop goes (an open connection warns, and warnings are errors)."""

    async def main():
        try:
            return await coro
        finally:
            await redis_async.close_async_redis()

    return asyncio.run(main())


class _Adapter:
    """One calling convention for RedisService and AsyncRedisService."""

    def __init__(self, service, is_async):
        self.service = service
        self.is_async = is_async

    def __getattr__(self, name):
        attribute = getattr(self.service, name)
        if not self.is_async or not callable(attribute):
            return attribute

        def call(*args, **kwargs):
            result = attribute(*args, **kwargs)
            return arun(result) if asyncio.iscoroutine(result) else result

        return call


@pytest.fixture(params=["sync", "async"])
def svc(request, backend):
    if request.param == "sync":
        return _Adapter(RedisService(), False)
    return _Adapter(AsyncRedisService(), True)


def _add(svc, owner, product_id=1, quantity=1, stock=10, **kwargs):
    return svc.add_to_cart_atomic(owner, product_id, quantity, None, f"p{product_id}", None, stock, **kwargs)


# --- The rate-limit Lua script (_COUNT_LUA) ---------------------------------


def test_the_count_script_counts_inside_a_window_and_starts_a_new_one_after_it(backend, raw):
    rate = Rate(5, 1)

    async def scenario():
        first = [await rate_limit._hit("rl:parity", rate) for _ in range(3)]
        pttl = raw.pttl("rl:parity")
        await asyncio.sleep(1.2)
        return first, pttl, await rate_limit._hit("rl:parity", rate)

    first, pttl, after_window = arun(scenario())

    assert [count for count, _ in first] == [1, 2, 3]
    # The window starts with the first request: later hits see less time left,
    # never more, and the key itself carries that expiry.
    assert all(0 < ttl_ms <= 1000 for _, ttl_ms in first)
    assert [ttl for _, ttl in first] == sorted((ttl for _, ttl in first), reverse=True)
    assert 0 < pttl <= 1000
    # The key expired with its window, so the next request opens a new one.
    assert after_window[0] == 1
    assert 0 < after_window[1] <= 1000


def test_the_count_script_gives_an_immortal_counter_its_missing_expiry(backend, raw):
    # A counter that somehow has no TTL (PTTL -1) would block for ever; the
    # script puts the window's expiry on it and still counts the request.
    raw.set("rl:immortal", "41")
    assert raw.pttl("rl:immortal") == -1

    count, ttl_ms = arun(rate_limit._hit("rl:immortal", Rate(100, 60)))

    assert (count, ttl_ms) == (42, 60_000)
    assert 0 < raw.pttl("rl:immortal") <= 60_000


def test_the_count_script_is_reloaded_when_the_server_forgets_it(backend, raw):
    # SCRIPT FLUSH (a restart, a failover) makes EVALSHA answer NOSCRIPT; the
    # Script object loads the body again and the request is still counted.
    rate = Rate(10, 60)
    assert arun(rate_limit._hit("rl:noscript", rate))[0] == 1

    raw.script_flush()

    assert arun(rate_limit._hit("rl:noscript", rate))[0] == 2


def test_concurrent_hits_get_distinct_counts(backend):
    # INCR and the expiry are one atomic step: 25 requests racing for one key
    # are numbered 1..25 with no repeats and no gaps.
    async def scenario():
        return await asyncio.gather(*(rate_limit._hit("rl:race", Rate(100, 60)) for _ in range(25)))

    results = arun(scenario())

    assert sorted(count for count, _ in results) == list(range(1, 26))


# --- Token revocation ---------------------------------------------------------


def test_a_blacklisted_token_is_seen_by_both_clients_until_it_expires(backend, raw):
    sync_service = RedisService()
    assert sync_service.add_to_blacklist("tok-1", 1) is True

    assert sync_service.is_blacklisted("tok-1") is True
    assert arun(is_token_blacklisted("tok-1")) is True
    assert 0 < raw.pttl("blacklist:tok-1") <= 1000
    assert arun(is_token_blacklisted("other")) is False

    time.sleep(1.2)

    assert sync_service.is_blacklisted("tok-1") is False
    assert arun(is_token_blacklisted("tok-1")) is False


def test_the_password_change_watermark_supersedes_older_tokens(backend, raw):
    watermark = time.time()
    assert RedisService().setex("pwd_changed:7", 60, watermark) is True

    assert arun(is_token_superseded(7, watermark - 5)) is True
    assert arun(is_token_superseded(7, watermark + 5)) is False
    assert arun(is_token_superseded(8, watermark - 5)) is False
    # A token with no iat predates every watermark.
    assert arun(is_token_superseded(7, None)) is True
    assert 0 < raw.ttl("pwd_changed:7") <= 60


def test_a_refresh_token_is_redeemed_by_exactly_one_of_many_racing_callers(backend, raw):
    # GET + DELETE in one MULTI/EXEC: reuse detection depends on the loser
    # getting nothing.
    service = RedisService()
    assert service.register_refresh_token("jti-1", 7, "fam-1", 60) is True
    winners = []
    barrier = threading.Barrier(8)

    def redeem():
        barrier.wait()
        winners.append(RedisService().consume_refresh_token("jti-1"))

    threads = [threading.Thread(target=redeem) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(w is not None for w in winners) == [False] * 7 + [True]
    assert next(w for w in winners if w) == {"user_id": 7, "family": "fam-1"}
    assert raw.exists("refresh_active:jti-1") == 0


def test_logout_revokes_the_access_token_through_the_http_stack(backend, client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    tokens = client.post(
        f"{API_PREFIX}/login",
        data={"username": valid_registration_payload["email"], "password": valid_registration_payload["password"]},
    ).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert client.get(f"{API_PREFIX}/me", headers=headers).status_code == 200

    assert client.post(f"{API_PREFIX}/logout", headers=headers).status_code == 200

    assert client.get(f"{API_PREFIX}/me", headers=headers).status_code == 401


# --- SET NX EX: the "do this once" guard ----------------------------------------


def test_mark_once_lets_one_caller_through_per_ttl_window(backend, raw):
    service = RedisService()

    assert service.mark_once("once:email:1", 1) is True
    assert service.mark_once("once:email:1", 1) is False
    assert arun(AsyncRedisService().mark_once("once:email:1", 1)) is False
    assert 0 < raw.pttl("once:email:1") <= 1000

    time.sleep(1.2)

    assert arun(AsyncRedisService().mark_once("once:email:1", 1)) is True
    assert service.mark_once("once:email:1", 1) is False


def test_mark_once_has_one_winner_among_racing_threads(backend):
    outcomes = []
    barrier = threading.Barrier(10)

    def claim():
        barrier.wait()
        outcomes.append(RedisService().mark_once("once:race", 30))

    threads = [threading.Thread(target=claim) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count(True) == 1


# --- WATCH / MULTI with a competing writer ----------------------------------------


def test_a_write_between_watch_and_exec_makes_the_transaction_retry(svc, raw):
    # The mutator runs after WATCH and before EXEC. On its first call a
    # competing client adds 3 of the same product: EXEC must fail (WatchError)
    # and the whole read-mutate-write run again on the new state, so neither
    # add is lost: 3 + 2.
    calls = []
    inner = codec.add_mutator(1, 2, None, "p1", None, 10)

    def mutator(cart):
        calls.append(1)
        if len(calls) == 1:
            RedisService().add_to_cart_atomic(7, 1, 3, None, "p1", None, 10)
        inner(cart)

    cart = svc._atomic_mutate_cart(7, mutator)

    assert len(calls) == 2
    assert cart.items["1"].quantity == 5
    assert json.loads(raw.get("cart:7"))["items"]["1"]["quantity"] == 5


def test_the_stock_check_runs_again_on_the_state_the_competing_writer_left(svc, raw):
    # Stock 4: the competing add of 3 lands first, so this add of 2 must be
    # refused on the retry. Nothing of the refused add is written.
    calls = []
    inner = codec.add_mutator(1, 2, None, "p1", None, 4)

    def mutator(cart):
        calls.append(1)
        if len(calls) == 1:
            RedisService().add_to_cart_atomic(7, 1, 3, None, "p1", None, 4)
        inner(cart)

    with pytest.raises(InsufficientStockError) as excinfo:
        svc._atomic_mutate_cart(7, mutator)

    assert (excinfo.value.available, excinfo.value.requested) == (4, 5)
    assert json.loads(raw.get("cart:7"))["items"]["1"]["quantity"] == 3


def test_racing_adds_all_land(svc, raw):
    # 8 threads add 1 each through whichever class is under test: the lost
    # update the WATCH guard exists to prevent would leave less than 8.
    barrier = threading.Barrier(8)
    errors = []

    def add():
        try:
            barrier.wait()
            _add(svc, 7, quantity=1, stock=100)
        except Exception as e:  # pragma: no cover - surfaced by the assert below
            errors.append(e)

    threads = [threading.Thread(target=add) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert json.loads(raw.get("cart:7"))["items"]["1"]["quantity"] == 8


def test_merging_a_guest_cart_is_retried_when_the_account_cart_changes_meanwhile(svc, raw, monkeypatch):
    # merge_guest_cart WATCHes both keys. The account cart changes between
    # the watch and the exec (here: when the merge reads it, through a
    # competing add); the merge reruns and the quantities add up.
    from app.schemas.cart import CartOwner, new_guest_cart_id

    guest_id = new_guest_cart_id()
    guest = CartOwner.guest(guest_id)
    _add(svc, guest, quantity=2)
    _add(svc, 7, quantity=1)

    original = codec.plan_merge_into_account
    calls = []

    def competing(account, user_id, guest_cart, account_data):
        calls.append(1)
        if len(calls) == 1:
            RedisService().add_to_cart_atomic(7, 1, 4, None, "p1", None, 100)
        return original(account, user_id, guest_cart, account_data)

    monkeypatch.setattr(codec, "plan_merge_into_account", competing)

    assert svc.merge_guest_cart(guest_id, 7) is True

    assert len(calls) == 2
    assert json.loads(raw.get("cart:7"))["items"]["1"]["quantity"] == 1 + 4 + 2
    assert raw.exists(guest.key) == 0


# --- Expiry ------------------------------------------------------------------------


def test_carts_expire_with_the_ttl_of_their_owner_kind(svc, raw):
    from app.schemas.cart import CartOwner, new_guest_cart_id

    guest = CartOwner.guest(new_guest_cart_id())
    _add(svc, 7)
    _add(svc, guest)

    assert 0 < raw.ttl("cart:7") <= int(codec.cart_ttl().total_seconds())
    assert 0 < raw.ttl(guest.key) <= int(codec.cart_ttl(guest).total_seconds())
    assert raw.ttl("cart:7") > raw.ttl(guest.key)


def test_an_expired_cart_reads_as_no_cart(svc, raw):
    _add(svc, 7)
    assert svc.get_cart(7) is not None

    raw.pexpire("cart:7", 300)
    time.sleep(0.5)

    assert svc.get_cart(7) is None
    assert raw.exists("cart:7") == 0


def test_setex_stores_a_value_that_disappears_when_its_ttl_ends(svc, raw):
    assert svc.setex("scratch:k", 1, {"a": 1}) is True
    assert svc.get("scratch:k") == {"a": 1}
    assert 0 < raw.pttl("scratch:k") <= 1000

    time.sleep(1.2)

    assert svc.get("scratch:k") is None


# --- redis_codec round trip ----------------------------------------------------------


def test_a_cart_survives_the_trip_through_the_server_unchanged(svc, raw):
    name = "Crème brûlée \U0001f370 ☃ \"quoted\" \\ back"
    svc.add_to_cart_atomic(7, 1, 3, Decimal("19.99"), name, "https://img.example/é.png", 10, variant_id=4)
    svc.add_to_cart_atomic(7, 2, 1, Decimal("0.10"), "plain", None, 10)

    cart = svc.get_cart(7)

    line = next(i for i in cart.items.values() if i.product_id == 1)
    assert line.name_snapshot == name
    assert line.price_snapshot == Decimal("19.99")
    assert line.image_snapshot == "https://img.example/é.png"
    assert line.variant_id == 4
    assert next(i for i in cart.items.values() if i.product_id == 2).price_snapshot == Decimal("0.10")
    # What is stored is the codec's own serialization, readable as plain JSON.
    assert json.loads(raw.get("cart:7"))["user_id"] == 7
    assert cart.expires_at > cart.created_at
    assert len(cart.items) == 2


def test_a_cached_product_and_a_large_value_round_trip(svc, raw):
    product = {"id": 5, "name": "Ünï \U0001f4a1", "price": "12.30", "tags": ["a", "b"], "nested": {"x": None}}
    assert svc.cache_product(5, product) is True
    assert svc.get_cached_product(5) == product
    assert 0 < raw.ttl("product:5") <= int(codec.PRODUCT_CACHE_TTL.total_seconds())

    big = {"blob": "x" * 300_000}
    assert svc.setex("scratch:big", 30, big) is True
    assert svc.get("scratch:big") == big
