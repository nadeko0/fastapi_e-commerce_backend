"""RedisService (sync) and AsyncRedisService behave the same.

Every test runs one scenario against both classes, on the same fakeredis
FakeServer (the autouse `fake_redis` fixture), and asserts the same
outcome. The shared rules live in app/services/redis_codec.py; these tests
pin the part that is not shared: the I/O, the WATCH/MULTI plumbing, the key
formats and the error mapping.
"""

import asyncio
import json

import fakeredis
import pytest

import app.services.redis_async as redis_async
import app.services.redis_codec as codec
from app.schemas.cart import CartOwner, new_guest_cart_id
from app.schemas.common import Cart
from app.services.redis import RedisService
from app.services.redis_async import AsyncRedisService, TokenStateUnavailable, get_cache_service
from app.services.redis_codec import (
    CartFullError,
    CartNotFoundError,
    InsufficientStockError,
    ItemNotInCartError,
)


class _Adapter:
    """Calls a service method the same way whichever class it is: awaits it
    on a fresh event loop for the async service."""

    def __init__(self, service, is_async):
        self.service = service
        self.is_async = is_async

    def __getattr__(self, name):
        attribute = getattr(self.service, name)
        if not self.is_async or not callable(attribute):
            return attribute

        def call(*args, **kwargs):
            result = attribute(*args, **kwargs)
            if asyncio.iscoroutine(result):
                return asyncio.run(result)
            return result

        return call


@pytest.fixture(params=["sync", "async"])
def svc(request):
    if request.param == "sync":
        return _Adapter(RedisService(), False)
    return _Adapter(AsyncRedisService(), True)


@pytest.fixture
def raw():
    """The sync fakeredis client on the shared server: what is actually
    stored, whichever class stored it."""
    return RedisService()._redis


def _add(svc, owner, product_id=1, quantity=1, stock=10, **kwargs):
    return svc.add_to_cart_atomic(
        owner, product_id, quantity, None, f"p{product_id}", None, stock, **kwargs
    )


# --- Carts -----------------------------------------------------------------


def test_add_merges_a_line_and_stores_it_under_the_cart_key_with_a_ttl(svc, raw):
    _add(svc, 7, quantity=2)
    cart = _add(svc, 7, quantity=3)

    assert cart.items["1"].quantity == 5
    stored = json.loads(raw.get("cart:7"))
    assert stored["items"]["1"]["quantity"] == 5
    assert 0 < raw.ttl("cart:7") <= int(codec.cart_ttl().total_seconds())
    assert svc.get_cart(7).items["1"].quantity == 5


def test_add_refuses_more_than_the_stock_and_writes_nothing(svc, raw):
    _add(svc, 7, quantity=4, stock=5)

    with pytest.raises(InsufficientStockError) as excinfo:
        _add(svc, 7, quantity=2, stock=5)

    assert (excinfo.value.available, excinfo.value.requested) == (5, 6)
    assert svc.get_cart(7).items["1"].quantity == 4


def test_add_refuses_a_new_line_beyond_the_cap(svc, monkeypatch):
    monkeypatch.setattr(codec.settings, "CART_MAX_LINES", 1)
    _add(svc, 7, product_id=1)

    with pytest.raises(CartFullError):
        _add(svc, 7, product_id=2)
    # More of an existing line is still fine.
    assert _add(svc, 7, product_id=1).items["1"].quantity == 2


def test_variants_are_distinct_lines(svc):
    _add(svc, 7, variant_id=None)
    cart = _add(svc, 7, variant_id=3)

    assert len(cart.items) == 2


def test_update_remove_and_clear(svc):
    _add(svc, 7, quantity=2)
    _add(svc, 7, product_id=2)

    assert svc.update_cart_quantity_atomic(7, 1, 4, 10).items["1"].quantity == 4
    with pytest.raises(InsufficientStockError):
        svc.update_cart_quantity_atomic(7, 1, 11, 10)
    with pytest.raises(ItemNotInCartError):
        svc.update_cart_quantity_atomic(7, 99, 1, 10)

    assert set(svc.remove_from_cart_atomic(7, 2).items) == {"1"}
    with pytest.raises(ItemNotInCartError):
        svc.remove_from_cart_atomic(7, 2)

    assert svc.clear_cart_atomic(7).items == {}


def test_update_remove_and_clear_on_a_missing_cart_store_nothing(svc, raw):
    with pytest.raises(ItemNotInCartError):
        svc.update_cart_quantity_atomic(7, 1, 1, 10)
    with pytest.raises(ItemNotInCartError):
        svc.remove_from_cart_atomic(7, 1)
    assert svc.clear_cart_atomic(7).items == {}

    assert raw.exists("cart:7") == 0


def test_must_exist_refuses_to_create_a_cart(svc, raw):
    with pytest.raises(CartNotFoundError):
        _add(svc, 7, must_exist=True)

    assert raw.exists("cart:7") == 0


def test_guest_cart_has_the_guest_ttl_and_is_deleted_when_emptied(svc, raw, monkeypatch):
    monkeypatch.setattr(codec.settings, "REDIS_GUEST_CART_TTL_DAYS", 2)
    guest = CartOwner.guest(new_guest_cart_id())

    _add(svc, guest)
    assert 0 < raw.ttl(guest.key) <= 2 * 86400

    svc.remove_from_cart_atomic(guest, 1)
    assert raw.exists(guest.key) == 0


def test_guest_without_an_identifier_has_no_cart(svc):
    anonymous = CartOwner.guest(None)

    assert svc.get_cart(anonymous) is None
    with pytest.raises(CartNotFoundError):
        _add(svc, anonymous)


def test_get_cart_ignores_a_value_stored_for_another_owner(svc, raw):
    _add(svc, 7)
    raw.set("cart:8", raw.get("cart:7"))

    assert svc.get_cart(8) is None


def test_update_and_delete_cart(svc, raw):
    cart = svc.empty_cart(7)

    assert svc.update_cart(cart)
    assert raw.exists("cart:7") == 1
    assert svc.delete_cart(7) is True
    assert svc.delete_cart(7) is False


def test_merge_guest_cart_moves_lines_and_deletes_the_guest_cart(svc, raw):
    guest = CartOwner.guest(new_guest_cart_id())
    _add(svc, guest, product_id=1, quantity=2)
    _add(svc, 7, product_id=1, quantity=1)
    _add(svc, 7, product_id=2, quantity=1)

    assert svc.merge_guest_cart(guest.guest_id, 7) is True

    cart = svc.get_cart(7)
    assert cart.items["1"].quantity == 3
    assert cart.items["2"].quantity == 1
    assert raw.exists(guest.key) == 0
    # A second login with the same guest cart finds it gone.
    assert svc.merge_guest_cart(guest.guest_id, 7) is True
    assert svc.get_cart(7).items["1"].quantity == 3


def test_merge_with_no_guest_cart_writes_nothing(svc, raw):
    assert svc.merge_guest_cart(new_guest_cart_id(), 7) is True

    assert raw.exists("cart:7") == 0


def test_remove_ordered_items_subtracts_and_deletes_when_nothing_is_left(svc, raw):
    _add(svc, 7, product_id=1, quantity=3)
    _add(svc, 7, product_id=2, quantity=1)
    ordered = svc.get_cart(7)
    _add(svc, 7, product_id=1, quantity=2)  # added after checkout read the cart

    assert svc.remove_ordered_items_atomic(7, ordered) is True

    cart = svc.get_cart(7)
    assert {k: v.quantity for k, v in cart.items.items()} == {"1": 2}

    assert svc.remove_ordered_items_atomic(7, cart) is True
    assert raw.exists("cart:7") == 0
    # No stored cart at all: still success, nothing stored.
    assert svc.remove_ordered_items_atomic(7, cart) is True


def test_a_write_between_the_read_and_the_exec_makes_the_transaction_retry(svc, monkeypatch, raw):
    """WATCH/MULTI really guards the key (on the async client too): a write
    that lands after the read makes EXEC fail, and the mutation is redone on
    the fresh value, so the concurrent add is not lost."""
    _add(svc, 7, product_id=1, quantity=1)
    real_plan = codec.plan_cart_mutation
    calls = []

    def interfering_plan(owner, cart_data, mutator, must_exist):
        calls.append(cart_data)
        if len(calls) == 1:
            # Another client adds a line after we read, before we EXEC.
            other = Cart.model_validate_json(raw.get("cart:7"))
            other.add_item(product_id=2, quantity=5, price=None, name="p2", image=None)
            raw.set("cart:7", codec.serialize(other))
        return real_plan(owner, cart_data, mutator, must_exist)

    monkeypatch.setattr(codec, "plan_cart_mutation", interfering_plan)

    cart = _add(svc, 7, product_id=1, quantity=1)

    assert len(calls) == 2
    assert cart.items["1"].quantity == 2
    assert cart.items["2"].quantity == 5
    assert svc.get_cart(7).items["2"].quantity == 5


def test_concurrent_async_adds_are_not_lost():
    service = AsyncRedisService()

    async def scenario():
        await asyncio.gather(
            *(service.add_to_cart_atomic(7, 1, 1, None, "p", None, 100) for _ in range(20))
        )
        return await service.get_cart(7)

    assert asyncio.run(scenario()).items["1"].quantity == 20


# --- Tokens ------------------------------------------------------------------


def test_blacklist_uses_the_shared_key_and_is_seen_by_both(svc, raw):
    # Both services write it; the sync one is the reader that exists on both
    # (the async side reads through is_token_blacklisted, which raises).
    assert RedisService().is_blacklisted("t") is False
    assert svc.add_to_blacklist("t", 60)

    assert raw.exists("blacklist:t") == 1
    assert 0 < raw.ttl("blacklist:t") <= 60
    assert RedisService().is_blacklisted("t") is True


def test_mark_once_is_true_only_the_first_time(svc):
    assert svc.mark_once("once:k", 60) is True
    assert svc.mark_once("once:k", 60) is False


def test_refresh_token_lifecycle_and_keys(svc, raw):
    assert svc.register_refresh_token("j1", 7, "fam", 300)
    assert svc.register_refresh_token("j2", 7, "fam", 300)

    assert json.loads(raw.get("refresh_active:j1")) == {"user_id": 7, "family": "fam"}
    assert raw.smembers("refresh_family:fam") == {"j1", "j2"}
    assert raw.smembers("refresh_user:7") == {"fam"}
    assert 0 < raw.ttl("refresh_family:fam") <= 300

    assert svc.consume_refresh_token("j1") == {"user_id": 7, "family": "fam"}
    assert svc.consume_refresh_token("j1") is None  # replay
    assert svc.consume_refresh_token("never-issued") is None

    assert svc.revoke_refresh_family("fam") is True
    assert raw.exists("refresh_active:j2", "refresh_family:fam") == 0


def test_revoke_user_refresh_families_ends_every_chain(svc, raw):
    svc.register_refresh_token("a", 7, "f1", 300)
    svc.register_refresh_token("b", 7, "f2", 300)
    svc.register_refresh_token("c", 8, "f3", 300)

    assert svc.revoke_user_refresh_families(7) is True

    assert raw.exists("refresh_active:a", "refresh_active:b", "refresh_user:7") == 0
    assert raw.exists("refresh_active:c") == 1


# --- Cache -------------------------------------------------------------------


def test_product_cache_round_trip_and_invalidate(svc, raw):
    assert svc.get_cached_product(5) is None
    assert svc.cache_product(5, {"id": 5, "name": "x"})

    assert raw.exists("product:5") == 1
    assert 0 < raw.ttl("product:5") <= 3600
    assert svc.get_cached_product(5) == {"id": 5, "name": "x"}
    assert svc.invalidate_product_cache(5) is True
    assert svc.get_cached_product(5) is None


def test_category_tree_round_trip_and_invalid_payloads_are_dropped(svc, raw):
    from app.schemas.category import CategoryTreeResponse

    tree = CategoryTreeResponse(tree=[], total_categories=0, max_depth=0)
    assert svc.get_cached_category_tree() is None
    assert svc.cache_category_tree(tree)
    assert raw.exists("category:tree") == 1
    assert svc.get_cached_category_tree() == tree

    for bad in ("not json", "[1, 2]"):
        raw.set("category:tree", bad)
        assert svc.get_cached_category_tree() is None
        assert raw.exists("category:tree") == 0

    raw.set("category:tree", "{}")
    assert svc.invalidate_category_cache() is True


def test_generic_get_setex_delete(svc, raw):
    assert svc.setex("k", 60, {"a": 1})

    assert raw.get("k") == '{"a": 1}'
    assert 0 < raw.ttl("k") <= 60
    assert svc.get("k") == {"a": 1}
    assert svc.delete("k") is True
    assert svc.get("k") is None


# --- Error mapping: the same on both -----------------------------------------


@pytest.fixture
def broken(monkeypatch):
    """Both services talk to a server that refuses every connection."""
    server = fakeredis.FakeServer()
    server.connected = False
    sync_client = fakeredis.FakeRedis(server=server, decode_responses=True)
    monkeypatch.setattr(RedisService(), "_redis", sync_client)
    monkeypatch.setattr(
        redis_async,
        "_create_client",
        lambda: fakeredis.FakeAsyncRedis(server=server, decode_responses=True),
    )


@pytest.mark.parametrize(
    "call, expected",
    [
        (lambda s: s.get_cart(7), None),
        (lambda s: s.update_cart(s.empty_cart(7)), False),
        (lambda s: s.delete_cart(7), False),
        (lambda s: _add(s, 7), None),
        (lambda s: s.update_cart_quantity_atomic(7, 1, 1, 10), None),
        (lambda s: s.clear_cart_atomic(7), None),
        (lambda s: s.merge_guest_cart(new_guest_cart_id(), 7), False),
        (lambda s: s.add_to_blacklist("t", 60), False),
        (lambda s: s.mark_once("k", 60), True),  # fail open
        (lambda s: s.register_refresh_token("j", 7, "f", 60), False),
        (lambda s: s.revoke_refresh_family("f"), False),
        (lambda s: s.revoke_user_refresh_families(7), False),
        (lambda s: s.cache_product(1, {}), False),
        (lambda s: s.get_cached_product(1), None),
        (lambda s: s.get_cached_category_tree(), None),
        (lambda s: s.invalidate_category_cache(), False),
        (lambda s: s.get("k"), None),
        (lambda s: s.setex("k", 1, 1), False),
        (lambda s: s.delete("k"), False),
    ],
)
def test_a_redis_error_maps_to_the_same_result_on_both(svc, broken, call, expected):
    assert call(svc) == expected


def test_consume_refresh_token_on_a_redis_error_is_none_for_celery_and_cannot_tell_for_routes(
    svc, broken
):
    # The one deliberate difference: the sync service (Celery) keeps its
    # "None", the async one (token redemption) must not let an outage read
    # as a replay, which would burn the token's family and answer 401.
    if svc.is_async:
        with pytest.raises(TokenStateUnavailable):
            svc.consume_refresh_token("j")
    else:
        assert svc.consume_refresh_token("j") is None


def test_remove_ordered_items_reports_failure_on_a_redis_error(svc, broken):
    assert svc.remove_ordered_items_atomic(7, svc.empty_cart(7)) is False


def test_non_redis_errors_still_propagate_out_of_a_transaction(svc, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("bug")

    monkeypatch.setattr(codec, "plan_cart_mutation", boom)

    with pytest.raises(ValueError):
        _add(svc, 7)


# --- The dependency ------------------------------------------------------------


def test_get_cache_service_yields_an_async_service_on_the_running_loop():
    async def scenario():
        service = await get_cache_service()
        assert isinstance(service, AsyncRedisService)
        return await service.setex("dep:k", 60, 1), await service.get("dep:k")

    assert asyncio.run(scenario()) == (True, 1)
