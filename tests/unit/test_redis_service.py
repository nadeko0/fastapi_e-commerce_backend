import json
from datetime import timedelta
from decimal import Decimal

import pytest
from redis import ConnectionError as RedisConnectionError
from redis import TimeoutError as RedisTimeoutError

from app.core.clock import utc_now
from app.schemas.cart import CartOwner, is_guest_cart_id, new_guest_cart_id
from app.schemas.category import CategoryTreeResponse
from app.schemas.common import Cart
from app.services.redis import (
    CartFullError,
    CartNotFoundError,
    ItemNotInCartError,
    RedisService,
)


def _service():
    # fake_redis (autouse, conftest.py) already reinitialized the
    # RedisService singleton to be backed by a fresh FakeRedis instance.
    return RedisService()


def _break(monkeypatch, redis, method_name):
    """Make redis._redis.<method_name> raise ConnectionError, simulating a
    dropped/broken connection mid-operation."""
    def _raise(*args, **kwargs):
        raise RedisConnectionError("connection lost")
    monkeypatch.setattr(redis._redis, method_name, _raise)


def test_get_cart_fails_open_on_redis_timeout_error(monkeypatch):
    """Regression test: every method in RedisService only caught
    ConnectionError, not the broader RedisError (TimeoutError, ResponseError,
    etc are siblings of ConnectionError, not subclasses of it). A socket
    timeout mid-operation used to bypass every fail-open except clause and
    propagate as a raw, unhandled exception out of the cart/product/session
    endpoint calling it."""
    redis = _service()

    def _raise(*args, **kwargs):
        raise RedisTimeoutError("timed out")

    monkeypatch.setattr(redis._redis, "get", _raise)

    assert redis.get_cart(1) is None


def test_cache_product_round_trips_through_get_cached_product():
    redis = _service()
    data = {"id": 1, "name": "Widget", "price": "9.99"}

    assert redis.cache_product(1, data) is True
    cached = redis.get_cached_product(1)

    assert cached["name"] == "Widget"


def test_get_cached_product_returns_none_when_absent():
    redis = _service()

    assert redis.get_cached_product(999999) is None


def test_cache_category_tree_round_trips_through_get_cached_category_tree():
    redis = _service()
    tree = CategoryTreeResponse(tree=[], total_categories=0, max_depth=0)

    assert redis.cache_category_tree(tree) is True
    cached = redis.get_cached_category_tree()

    assert cached is not None
    assert cached.total_categories == 0


def test_get_cached_category_tree_returns_none_when_absent():
    redis = _service()

    assert redis.get_cached_category_tree() is None


def test_invalidate_category_cache_removes_cached_tree():
    redis = _service()
    tree = CategoryTreeResponse(tree=[], total_categories=0, max_depth=0)
    redis.cache_category_tree(tree)

    assert redis.invalidate_category_cache() is True
    assert redis.get_cached_category_tree() is None


def test_invalidate_product_cache_removes_cached_product():
    redis = _service()
    redis.cache_product(5, {"id": 5})

    assert redis.invalidate_product_cache(5) is True
    assert redis.get_cached_product(5) is None


def test_create_session_returns_id_and_get_session_reads_it_back():
    redis = _service()

    session_id = redis.create_session(42, {"role": "client"})

    assert session_id.startswith("session:42:")
    session = redis.get_session(session_id)
    assert session["role"] == "client"


def test_delete_session_removes_it():
    redis = _service()
    session_id = redis.create_session(42, {"role": "client"})

    assert redis.delete_session(session_id) is True
    assert redis.get_session(session_id) is None


def test_get_session_returns_none_when_absent():
    redis = _service()

    assert redis.get_session("session:does-not-exist") is None


def test_generic_get_setex_and_delete():
    redis = _service()

    assert redis.setex("custom:key", 60, {"hello": "world"}) is True
    assert redis.get("custom:key")["hello"] == "world"
    assert redis.delete("custom:key") is True
    assert redis.get("custom:key") is None


def test_delete_cart_removes_cart_key():
    redis = _service()
    from datetime import timedelta

    from app.schemas.common import Cart

    cart = Cart(
        user_id=7,
        items={},
        created_at=utc_now(),
        updated_at=utc_now(),
        expires_at=utc_now() + timedelta(days=1),
    )
    redis.update_cart(cart)

    assert redis.delete_cart(7) is True
    assert redis.get_cart(7) is None


def test_mark_once_returns_true_first_time_and_false_on_repeat():
    redis = _service()

    assert redis.mark_once("email_sent:confirmation:1", 3600) is True
    assert redis.mark_once("email_sent:confirmation:1", 3600) is False
    # A different key is independent.
    assert redis.mark_once("email_sent:confirmation:2", 3600) is True


def test_mark_once_fails_open_on_connection_error(monkeypatch):
    """mark_once fails open (returns True, "go ahead") rather than closed -
    for a duplicate-send guard, occasionally sending one extra email during
    a Redis outage is far less harmful than silently never sending it."""
    redis = _service()
    _break(monkeypatch, redis, "set")

    assert redis.mark_once("email_sent:confirmation:1", 3600) is True


def test_add_to_blacklist_and_is_blacklisted():
    redis = _service()

    assert redis.is_blacklisted("some-token") is False
    redis.add_to_blacklist("some-token", 3600)
    assert redis.is_blacklisted("some-token") is True


# --- Malformed / corrupted data already stored in Redis -------------------


def test_get_cart_returns_none_for_non_json_data():
    redis = _service()
    redis._redis.set(redis._get_cart_key(1), "not-json-at-all{{{")

    assert redis.get_cart(1) is None


def test_get_cart_returns_none_when_json_does_not_match_cart_schema():
    """Real bug found & fixed: valid JSON that fails Cart's pydantic
    validation (e.g. missing the required user_id field) used to raise an
    uncaught pydantic ValidationError out of get_cart instead of failing
    open. _deserialize now catches ValidationError alongside the JSON/attr
    errors it already handled (app/services/redis.py)."""
    redis = _service()
    redis._redis.set(redis._get_cart_key(1), '{"items": {}}')

    assert redis.get_cart(1) is None


def test_get_cart_converts_legacy_list_shaped_items():
    redis = _service()
    payload = (
        '{"user_id": 3, "items": [{"product_id": 7, "quantity": 2, '
        '"price_snapshot": 1.5, "name_snapshot": "n", "image_snapshot": "i"}], '
        '"created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00"}'
    )
    redis._redis.set(redis._get_cart_key(3), payload)

    cart = redis.get_cart(3)

    assert cart is not None
    assert cart.items["7"].quantity == 2
    # The price such a cart recorded was a float; it loads as the exact
    # two-decimal amount.
    assert cart.items["7"].price_snapshot == Decimal("1.50")
    assert str(cart.items["7"].price_snapshot) == "1.50"


def test_cart_written_before_live_pricing_survives_a_mutation():
    """A stored cart in the previous format (float price, name and image on
    every line) is read, changed and written back without losing a line."""
    redis = _service()
    payload = (
        '{"user_id": 3, "items": {'
        '"7": {"product_id": 7, "variant_id": null, "quantity": 2, "added_at": "2026-10-01T10:00:00", '
        '"price_snapshot": 9.99, "name_snapshot": "n", "image_snapshot": "i"}, '
        '"8:4": {"product_id": 8, "variant_id": 4, "quantity": 1, "added_at": "2026-10-01T10:00:00", '
        '"price_snapshot": 14.5, "name_snapshot": "m", "image_snapshot": ""}}, '
        '"created_at": "2026-10-01T10:00:00", "updated_at": "2026-10-01T10:00:00", '
        '"expires_at": "2026-10-08T10:00:00"}'
    )
    redis._redis.set(redis._get_cart_key(3), payload)

    cart = redis.update_cart_quantity_atomic(3, 8, 3, stock_quantity=10, variant_id=4)

    assert {key: item.quantity for key, item in cart.items.items()} == {"7": 2, "8:4": 3}
    stored = json.loads(redis._redis.get(redis._get_cart_key(3)))
    assert stored["items"]["7"]["price_snapshot"] == "9.99"
    assert stored["items"]["8:4"]["price_snapshot"] == "14.50"
    assert redis.get_cart(3).items["7"].price_snapshot == Decimal("9.99")


def test_get_cart_treats_an_impossible_recorded_price_as_unreadable():
    """Not a 500: a recorded price that cannot be a price is a stored cart
    that fails validation, handled like any other."""
    redis = _service()
    payload = (
        '{"user_id": 3, "items": {"7": {"product_id": 7, "quantity": 2, '
        '"price_snapshot": "1E+40"}}, "expires_at": "2026-10-08T10:00:00"}'
    )
    redis._redis.set(redis._get_cart_key(3), payload)

    assert redis.get_cart(3) is None


def test_get_cached_product_returns_none_for_non_json_data():
    redis = _service()
    redis._redis.set("product:42", "{not valid json")

    assert redis.get_cached_product(42) is None


def test_get_cached_category_tree_returns_none_for_non_json_data():
    redis = _service()
    redis._redis.set("category:tree", "{not valid json")

    result = redis.get_cached_category_tree()

    assert result is None
    assert redis._redis.get("category:tree") is None


def test_get_cached_category_tree_returns_none_when_json_is_not_a_dict():
    redis = _service()
    redis._redis.set("category:tree", '["a", "b"]')

    result = redis.get_cached_category_tree()

    assert result is None
    assert redis._redis.get("category:tree") is None


def test_get_session_returns_none_for_non_json_data():
    redis = _service()
    redis._redis.set("session:corrupt", "not-json")

    assert redis.get_session("session:corrupt") is None


def test_generic_get_returns_none_for_non_json_data():
    redis = _service()
    redis._redis.set("custom:corrupt", "not-json")

    assert redis.get("custom:corrupt") is None


# --- Connection-drop / fail-open behavior for every public method ---------


def test_get_cart_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "get")

    assert redis.get_cart(1) is None


def test_update_cart_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "set")
    cart = Cart(
        user_id=1,
        items={},
        created_at=utc_now(),
        updated_at=utc_now(),
        expires_at=utc_now() + timedelta(days=1),
    )

    assert redis.update_cart(cart) is False


def test_delete_cart_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "delete")

    assert redis.delete_cart(1) is False


def test_add_to_blacklist_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "set")

    assert redis.add_to_blacklist("tok", 3600) is False


def test_is_blacklisted_fails_closed_on_connection_error(monkeypatch):
    """is_blacklisted deliberately fails *closed* (returns True) on a
    connection error, since assuming a token is still valid on a Redis
    outage would be a security hole."""
    redis = _service()
    _break(monkeypatch, redis, "exists")

    assert redis.is_blacklisted("tok") is True


def test_cache_product_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "set")

    assert redis.cache_product(1, {"id": 1}) is False


def test_cache_category_tree_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "set")
    tree = CategoryTreeResponse(tree=[], total_categories=0, max_depth=0)

    assert redis.cache_category_tree(tree) is False


def test_get_cached_category_tree_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "get")

    assert redis.get_cached_category_tree() is None


def test_get_cached_product_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "get")

    assert redis.get_cached_product(1) is None


def test_create_session_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "set")

    assert redis.create_session(1, {"role": "client"}) == ""


def test_get_session_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "get")

    assert redis.get_session("session:1") is None


def test_delete_session_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "delete")

    assert redis.delete_session("session:1") is False


# --- Atomic cart mutations: failure and checkout clean-up ------------------


def _seed_cart(redis, user_id, lines):
    """lines: {(product_id, variant_id): quantity}. `user_id` is the
    owner of the cart: a user id, or a CartOwner (which can be a guest)."""
    for (product_id, variant_id), quantity in lines.items():
        redis.add_to_cart_atomic(
            user_id=user_id, product_id=product_id, quantity=quantity, price=Decimal("1.00"),
            name="n", image="i", stock_quantity=100, variant_id=variant_id,
        )
    return redis.get_cart(user_id)


def test_atomic_cart_mutations_return_none_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "transaction")

    assert redis.add_to_cart_atomic(1, 1, 1, Decimal("1.00"), "n", "i", 10) is None
    assert redis.clear_cart_atomic(1) is None


def test_remove_ordered_items_deletes_the_key_when_nothing_else_is_left():
    redis = _service()
    ordered = _seed_cart(redis, 1, {(7, None): 2, (8, 3): 1})

    assert redis.remove_ordered_items_atomic(1, ordered) is True
    assert redis._redis.exists(redis._get_cart_key(1)) == 0


def test_remove_ordered_items_keeps_what_was_added_after_the_order_read_the_cart():
    """Regression test: checkout read the cart, built the order from it, then
    deleted the whole key - anything added in between was deleted without
    ever having been ordered."""
    redis = _service()
    ordered = _seed_cart(redis, 1, {(7, None): 2, (8, 3): 1})
    # Lands after checkout's read: a new line, and one more of an ordered line.
    _seed_cart(redis, 1, {(9, None): 4, (7, None): 1})

    assert redis.remove_ordered_items_atomic(1, ordered) is True

    left = redis.get_cart(1)
    assert {key: item.quantity for key, item in left.items.items()} == {"9": 4, "7": 1}
    assert redis._redis.ttl(redis._get_cart_key(1)) > 0


def test_remove_ordered_items_tolerates_a_line_reduced_or_removed_meanwhile():
    redis = _service()
    ordered = _seed_cart(redis, 1, {(7, None): 5, (8, None): 1, (9, None): 1})
    redis.update_cart_quantity_atomic(1, 7, 2, stock_quantity=100)
    redis.remove_from_cart_atomic(1, 8)

    assert redis.remove_ordered_items_atomic(1, ordered) is True
    assert redis._redis.exists(redis._get_cart_key(1)) == 0


def test_remove_ordered_items_is_a_no_op_when_the_cart_is_already_gone():
    redis = _service()
    ordered = _seed_cart(redis, 1, {(7, None): 2})
    redis.delete_cart(1)

    assert redis.remove_ordered_items_atomic(1, ordered) is True
    assert redis._redis.exists(redis._get_cart_key(1)) == 0


def test_remove_ordered_items_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    ordered = _seed_cart(redis, 1, {(7, None): 2})
    _break(monkeypatch, redis, "transaction")

    assert redis.remove_ordered_items_atomic(1, ordered) is False


def test_delete_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "delete")

    assert redis.delete("some:key") is False


def test_invalidate_category_cache_fails_open_on_connection_error(monkeypatch):
    redis = _service()

    def _raise(*args, **kwargs):
        raise RedisConnectionError("connection lost")

    monkeypatch.setattr(redis, "delete", _raise)

    assert redis.invalidate_category_cache() is False


def test_invalidate_product_cache_fails_open_on_connection_error(monkeypatch):
    redis = _service()

    def _raise(*args, **kwargs):
        raise RedisConnectionError("connection lost")

    monkeypatch.setattr(redis, "delete", _raise)

    assert redis.invalidate_product_cache(1) is False


def test_generic_get_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "get")

    assert redis.get("custom:key") is None


def test_generic_setex_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "set")

    assert redis.setex("custom:key", 60, {"a": 1}) is False


# --- Remaining _deserialize / cache_category_tree branches ----------------


def test_deserialize_returns_none_for_empty_string():
    redis = _service()

    assert redis._deserialize("", Cart) is None


def test_deserialize_cart_defaults_items_when_key_missing():
    redis = _service()

    cart = redis._deserialize('{"user_id": 9}', Cart)

    assert cart is not None
    assert cart.items == {}


def test_deserialize_category_tree_recovers_from_json_string_payload():
    """A stray JSON string (not an object) in the cache key is coerced to an
    empty tree instead of failing validation."""
    redis = _service()

    tree = redis._deserialize('"oops"', CategoryTreeResponse)

    assert tree is not None
    assert tree.total_categories == 0


def test_deserialize_category_tree_recovers_from_list_payload():
    redis = _service()

    tree = redis._deserialize("[1, 2, 3]", CategoryTreeResponse)

    assert tree is not None
    assert tree.total_categories == 0


def test_cache_category_tree_deletes_key_when_setex_reports_failure(monkeypatch):
    """If the underlying setex call returns a falsy result (write did not
    take effect) rather than raising, cache_category_tree proactively
    deletes the key to avoid leaving stale/partial data cached."""
    redis = _service()
    tree = CategoryTreeResponse(tree=[], total_categories=0, max_depth=0)
    redis._redis.set("category:tree", "stale-value")
    monkeypatch.setattr(redis._redis, "set", lambda *a, **k: False)

    result = redis.cache_category_tree(tree)

    assert result is False
    assert redis._redis.get("category:tree") is None


# --- Cart owners: accounts and guests ---------------------------------------


def _guest():
    return CartOwner.guest(new_guest_cart_id())


def _lines(cart):
    return {key: item.quantity for key, item in cart.items.items()}


def test_guest_identifier_is_long_random_and_url_safe():
    identifiers = {new_guest_cart_id() for _ in range(200)}

    assert len(identifiers) == 200
    assert all(len(identifier) == 43 for identifier in identifiers)
    assert all(is_guest_cart_id(identifier) for identifier in identifiers)


def test_only_a_well_formed_identifier_is_a_guest_identifier():
    good = new_guest_cart_id()

    for bad in [None, 1, "", "1", good[:-1], good + "A", good[:-1] + "!", good[:-1] + "\n", "cart:1"]:
        assert is_guest_cart_id(bad) is False
        if isinstance(bad, str):
            with pytest.raises(ValueError):
                CartOwner.guest(bad)


def test_account_and_guest_carts_live_under_separate_key_namespaces():
    redis = _service()
    guest = _guest()

    assert redis._get_cart_key(7) == "cart:7"
    assert redis._get_cart_key(CartOwner.account(7)) == "cart:7"
    assert redis._get_cart_key(guest) == f"guest_cart:{guest.guest_id}"
    assert redis._get_cart_key(CartOwner.guest()) is None


def test_guest_cart_gets_the_guest_ttl_and_account_cart_the_account_ttl(monkeypatch):
    monkeypatch.setattr("app.services.redis.settings.REDIS_GUEST_CART_TTL_DAYS", 2)
    monkeypatch.setattr("app.services.redis.settings.REDIS_CART_TTL_DAYS", 9)
    redis = _service()
    guest = _guest()

    guest_cart = _seed_cart(redis, guest, {(7, None): 1})
    account_cart = _seed_cart(redis, 1, {(7, None): 1})

    assert 2 * 86400 - 60 < redis._redis.ttl(guest.key) <= 2 * 86400
    assert 9 * 86400 - 60 < redis._redis.ttl("cart:1") <= 9 * 86400
    assert abs((guest_cart.expires_at - utc_now()) - timedelta(days=2)) < timedelta(minutes=1)
    assert abs((account_cart.expires_at - utc_now()) - timedelta(days=9)) < timedelta(minutes=1)
    assert redis.empty_cart(guest).expires_at < redis.empty_cart(1).expires_at


def test_guest_without_an_identifier_has_no_cart_and_touches_nothing(monkeypatch):
    redis = _service()
    nobody = CartOwner.guest()
    for method in ("get", "transaction"):
        _break(monkeypatch, redis, method)  # any Redis call would show as a failure

    assert redis.get_cart(nobody) is None
    assert redis.clear_cart_atomic(nobody).items == {}
    with pytest.raises(ItemNotInCartError):
        redis.update_cart_quantity_atomic(nobody, 7, 1, stock_quantity=10)
    with pytest.raises(ItemNotInCartError):
        redis.remove_from_cart_atomic(nobody, 7)
    with pytest.raises(CartNotFoundError):
        redis.add_to_cart_atomic(nobody, 7, 1, Decimal("1.00"), "n", "i", 10)


def test_add_with_must_exist_does_not_create_a_cart():
    redis = _service()
    guest = _guest()

    with pytest.raises(CartNotFoundError):
        redis.add_to_cart_atomic(guest, 7, 1, Decimal("1.00"), "n", "i", 10, must_exist=True)

    assert redis._redis.exists(guest.key) == 0
    _seed_cart(redis, guest, {(7, None): 1})
    cart = redis.add_to_cart_atomic(guest, 7, 2, Decimal("1.00"), "n", "i", 10, must_exist=True)
    assert _lines(cart) == {"7": 3}


def test_update_remove_and_clear_never_create_a_cart():
    redis = _service()
    guest = _guest()

    for owner in (guest, 5):
        with pytest.raises(ItemNotInCartError):
            redis.update_cart_quantity_atomic(owner, 7, 1, stock_quantity=10)
        with pytest.raises(ItemNotInCartError):
            redis.remove_from_cart_atomic(owner, 7)
        assert redis.clear_cart_atomic(owner).items == {}

    assert list(redis._redis.scan_iter("*")) == []


def test_guest_cart_left_empty_is_deleted_and_account_cart_is_kept():
    redis = _service()
    guest = _guest()
    _seed_cart(redis, guest, {(7, None): 1, (8, None): 1})
    _seed_cart(redis, 1, {(7, None): 1})

    assert redis.remove_from_cart_atomic(guest, 7).items.keys() == {"8"}
    assert redis._redis.exists(guest.key) == 1
    assert redis.remove_from_cart_atomic(guest, 8).items == {}
    assert redis._redis.exists(guest.key) == 0

    _seed_cart(redis, guest, {(7, None): 1})
    assert redis.clear_cart_atomic(guest).items == {}
    assert redis._redis.exists(guest.key) == 0

    assert redis.remove_from_cart_atomic(1, 7).items == {}
    assert redis.get_cart(1).items == {}


def test_a_value_that_is_not_the_cart_of_its_key_is_not_read():
    """An account key must hold that account's cart and a guest key a
    guest cart: one copied under the other's key is unreadable, not
    served."""
    redis = _service()
    guest = _guest()
    _seed_cart(redis, 1, {(7, None): 1})
    _seed_cart(redis, guest, {(8, None): 1})
    account_value, guest_value = redis._redis.get("cart:1"), redis._redis.get(guest.key)

    redis._redis.set("cart:2", account_value)
    redis._redis.set("cart:3", guest_value)
    redis._redis.set(guest.key, account_value)

    assert redis.get_cart(2) is None
    assert redis.get_cart(3) is None
    assert redis.get_cart(guest) is None
    assert _lines(redis.get_cart(1)) == {"7": 1}


def test_add_refuses_a_new_line_beyond_the_cap_for_both_kinds_of_owner(monkeypatch):
    monkeypatch.setattr("app.services.redis.settings.CART_MAX_LINES", 2)
    redis = _service()

    for owner in (_guest(), 1):
        _seed_cart(redis, owner, {(7, None): 1, (7, 3): 1})
        stored = redis._redis.get(redis._get_cart_key(owner))

        with pytest.raises(CartFullError) as refused:
            redis.add_to_cart_atomic(owner, 8, 1, Decimal("1.00"), "n", "i", 10)

        assert refused.value.max_lines == 2
        assert redis._redis.get(redis._get_cart_key(owner)) == stored
        # More of a line that is already there is not a new line.
        assert _lines(redis.add_to_cart_atomic(owner, 7, 1, Decimal("1.00"), "n", "i", 10)) == {
            "7": 2, "7:3": 1,
        }


def test_a_cart_over_the_cap_still_loads_and_can_be_reduced(monkeypatch):
    redis = _service()
    _seed_cart(redis, 1, {(7, None): 1, (8, None): 1, (9, None): 1})
    monkeypatch.setattr("app.services.redis.settings.CART_MAX_LINES", 2)

    assert len(redis.get_cart(1).items) == 3
    assert _lines(redis.update_cart_quantity_atomic(1, 7, 4, stock_quantity=10))["7"] == 4
    assert len(redis.remove_from_cart_atomic(1, 9).items) == 2


# --- Merging a guest cart into an account cart ------------------------------


def test_merge_moves_guest_lines_into_the_account_cart_and_deletes_the_guest_cart():
    redis = _service()
    guest = _guest()
    _seed_cart(redis, 1, {(7, None): 2, (8, 3): 1})
    _seed_cart(redis, guest, {(7, None): 3, (8, None): 1, (9, 4): 2})

    assert redis.merge_guest_cart(guest.guest_id, 1) is True

    merged = redis.get_cart(1)
    assert _lines(merged) == {"7": 5, "8:3": 1, "8": 1, "9:4": 2}
    assert merged.user_id == 1
    assert redis._redis.exists(guest.key) == 0
    assert 29 * 86400 < redis._redis.ttl("cart:1") <= 30 * 86400


def test_merge_into_an_account_without_a_cart_creates_it():
    redis = _service()
    guest = _guest()
    _seed_cart(redis, guest, {(7, None): 3})

    assert redis.merge_guest_cart(guest.guest_id, 1) is True

    assert _lines(redis.get_cart(1)) == {"7": 3}
    assert json.loads(redis._redis.get("cart:1"))["user_id"] == 1


def test_merge_with_no_guest_cart_writes_nothing():
    redis = _service()
    guest = _guest()
    _seed_cart(redis, 1, {(7, None): 2})
    stored, ttl = redis._redis.get("cart:1"), redis._redis.ttl("cart:1")

    assert redis.merge_guest_cart(guest.guest_id, 1) is True
    assert redis.merge_guest_cart(guest.guest_id, 2) is True

    assert redis._redis.get("cart:1") == stored
    assert redis._redis.ttl("cart:1") <= ttl
    assert redis._redis.exists("cart:2") == 0


def test_merge_leaves_an_unreadable_guest_cart_alone():
    redis = _service()
    guest = _guest()
    redis._redis.set(guest.key, "not-json-at-all{{{")

    assert redis.merge_guest_cart(guest.guest_id, 1) is True

    assert redis._redis.get(guest.key) == "not-json-at-all{{{"
    assert redis._redis.exists("cart:1") == 0


def test_merge_deletes_an_empty_stored_guest_cart_without_touching_the_account():
    redis = _service()
    guest = _guest()
    _seed_cart(redis, guest, {(7, None): 1})
    stored = json.loads(redis._redis.get(guest.key))
    stored["items"] = {}
    redis._redis.set(guest.key, json.dumps(stored))

    assert redis.merge_guest_cart(guest.guest_id, 1) is True

    assert redis._redis.exists(guest.key) == 0
    assert redis._redis.exists("cart:1") == 0


def test_merge_refuses_a_malformed_guest_identifier():
    redis = _service()

    with pytest.raises(ValueError):
        redis.merge_guest_cart("1", 1)


def test_merge_returns_false_on_connection_error_and_changes_neither_cart(monkeypatch):
    redis = _service()
    guest = _guest()
    _seed_cart(redis, 1, {(7, None): 2})
    _seed_cart(redis, guest, {(7, None): 3})
    before = (redis._redis.get("cart:1"), redis._redis.get(guest.key))
    with monkeypatch.context() as broken:
        _break(broken, redis, "transaction")

        assert redis.merge_guest_cart(guest.guest_id, 1) is False

    assert (redis._redis.get("cart:1"), redis._redis.get(guest.key)) == before


def test_merge_is_redone_when_either_cart_changes_between_its_read_and_its_write(monkeypatch):
    """The merge WATCHes both keys. A write to the account cart, then one
    to the guest cart, are each injected between the merge's reads and its
    EXEC: each makes the EXEC fail and the merge start again on fresh
    reads, so neither write is lost and nothing is merged twice."""
    redis = _service()
    guest = _guest()
    _seed_cart(redis, 1, {(7, None): 2})
    _seed_cart(redis, guest, {(7, None): 3})
    real_merge = Cart.merge
    attempts = []

    def _merge_after_a_concurrent_write(self, other, max_lines):
        attempts.append(1)
        if len(attempts) == 1:
            _seed_cart(redis, 1, {(8, None): 4})
        elif len(attempts) == 2:
            _seed_cart(redis, guest, {(9, None): 5, (7, None): 1})
        return real_merge(self, other, max_lines)

    monkeypatch.setattr(Cart, "merge", _merge_after_a_concurrent_write)

    assert redis.merge_guest_cart(guest.guest_id, 1) is True

    assert len(attempts) == 3
    assert _lines(redis.get_cart(1)) == {"7": 6, "8": 4, "9": 5}
    assert redis._redis.exists(guest.key) == 0


def test_merge_respects_the_line_cap_without_failing(monkeypatch):
    monkeypatch.setattr("app.services.redis.settings.CART_MAX_LINES", 3)
    redis = _service()
    guest = _guest()
    _seed_cart(redis, 1, {(7, None): 1, (8, None): 1})
    _seed_cart(redis, guest, {(9, None): 1, (7, None): 2, (10, None): 1})

    assert redis.merge_guest_cart(guest.guest_id, 1) is True

    assert _lines(redis.get_cart(1)) == {"7": 3, "8": 1, "9": 1}
    assert redis._redis.exists(guest.key) == 0
