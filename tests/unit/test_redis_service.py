from datetime import datetime, timedelta

from redis import ConnectionError as RedisConnectionError

from app.schemas.category import CategoryTreeResponse
from app.schemas.common import Cart
from app.services.redis import RedisService


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
    from datetime import datetime, timedelta

    from app.schemas.common import Cart

    cart = Cart(
        user_id=7,
        items={},
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=1),
    )
    redis.update_cart(cart)

    assert redis.delete_cart(7) is True
    assert redis.get_cart(7) is None


def test_add_to_blacklist_and_is_blacklisted():
    redis = _service()

    assert redis.is_blacklisted("some-token") is False
    redis.add_to_blacklist("some-token", 3600)
    assert redis.is_blacklisted("some-token") is True


def test_cleanup_expired_carts_ignores_keys_with_no_ttl():
    redis = _service()
    # cleanup_expired_carts only removes a key when self._redis.ttl(key) is
    # falsy. Redis's ttl() returns -1 for a key with no expiry at all (as
    # opposed to -2 for a missing key, or 0 for "about to expire") - and
    # `not -1` is False, so a cart key written without an expiry (e.g. via
    # plain set()) is left alone, not swept up despite having no TTL.
    redis._redis.set("cart:99", "{}")

    cleaned = redis.cleanup_expired_carts()

    assert cleaned == 0
    assert redis._redis.get("cart:99") == "{}"


def test_cleanup_expired_carts_ignores_keys_with_ttl():
    redis = _service()
    redis._redis.setex("cart:100", 3600, "{}")

    cleaned = redis.cleanup_expired_carts()

    assert cleaned == 0


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
    _break(monkeypatch, redis, "setex")
    cart = Cart(
        user_id=1,
        items={},
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=1),
    )

    assert redis.update_cart(cart) is False


def test_delete_cart_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "delete")

    assert redis.delete_cart(1) is False


def test_add_to_blacklist_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "setex")

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
    _break(monkeypatch, redis, "setex")

    assert redis.cache_product(1, {"id": 1}) is False


def test_cache_category_tree_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "setex")
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
    _break(monkeypatch, redis, "setex")

    assert redis.create_session(1, {"role": "client"}) == ""


def test_get_session_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "get")

    assert redis.get_session("session:1") is None


def test_delete_session_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "delete")

    assert redis.delete_session("session:1") is False


def test_cleanup_expired_carts_fails_open_on_connection_error(monkeypatch):
    redis = _service()
    _break(monkeypatch, redis, "scan_iter")

    assert redis.cleanup_expired_carts() == 0


def test_cleanup_expired_carts_removes_keys_with_no_real_ttl(monkeypatch):
    """ttl() returning 0 (or any falsy value other than -1) should cause the
    key to be swept up and counted."""
    redis = _service()
    redis._redis.set("cart:200", "{}")
    monkeypatch.setattr(redis._redis, "ttl", lambda key: 0)

    cleaned = redis.cleanup_expired_carts()

    assert cleaned == 1
    assert redis._redis.get("cart:200") is None


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
    _break(monkeypatch, redis, "setex")

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
    monkeypatch.setattr(redis._redis, "setex", lambda *a, **k: False)

    result = redis.cache_category_tree(tree)

    assert result is False
    assert redis._redis.get("category:tree") is None
