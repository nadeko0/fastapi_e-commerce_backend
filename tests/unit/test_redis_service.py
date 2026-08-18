from app.schemas.category import CategoryTreeResponse
from app.services.redis import RedisService


def _service():
    # fake_redis (autouse, conftest.py) already reinitialized the
    # RedisService singleton to be backed by a fresh FakeRedis instance.
    return RedisService()


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
