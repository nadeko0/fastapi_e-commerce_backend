"""Guest carts: a cart for a visitor who has not logged in, carried by a
server-issued HttpOnly cookie, and merged into the account's cart at login.

The guest tests talk to the app over https (`browser` below): the cookie is
Secure by default, and a cookie jar does not send a Secure cookie back over
plain http - which is the point of the attribute. Each `browser()` is one
visitor with a cookie jar of its own.
"""

import json
import re
import secrets
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import fakeredis
import jwt
import pytest
from fastapi.testclient import TestClient
from redis import ConnectionError as RedisConnectionError

from app.core.clock import utc_now
from app.core.config import Settings, settings
from app.core.security import blacklist_token, create_access_token
from app.main import app
from app.models.category import Category
from app.models.product import Product, ProductVariant
from app.models.user import User
from app.schemas.cart import CART_KEY_PREFIX, GUEST_CART_KEY_PREFIX
from app.schemas.common import Cart
from app.services.redis import RedisService
from app.services.redis_async import AsyncRedisService

USERS_PREFIX = "/api/v1/users"
CART_PREFIX = "/api/v1/cart"
ORDERS_PREFIX = "/api/v1/orders"

COOKIE = "__Host-guest_cart"
PASSWORD = "Str0ngPass1"
SEVEN_DAYS = 7 * 24 * 3600
THIRTY_DAYS = 30 * 24 * 3600


@pytest.fixture
def browser(client):
    """A factory of visitors. Depends on `client` so the app is started (and
    stopped) once per test by that fixture; the extra clients made here are
    plain cookie jars on top of the same app."""
    def _new():
        return TestClient(app, base_url="https://testserver")
    return _new


def _registration_payload(email="guest.buyer@example.com"):
    return {
        "email": email,
        "password": PASSWORD,
        "full_name": "Guest Buyer",
        "gdpr_consent": True,
        "privacy_policy_accepted": True,
        "marketing_consent": False,
    }


def _register(visitor, email="guest.buyer@example.com"):
    assert visitor.post(f"{USERS_PREFIX}/register", json=_registration_payload(email)).status_code == 200


def _login(visitor, email="guest.buyer@example.com", password=PASSWORD):
    return visitor.post(f"{USERS_PREFIX}/login", data={"username": email, "password": password})


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _make_category(db_session):
    category = Category(name="Electronics", parent_id=None, level=0, path=[])
    db_session.add(category)
    db_session.commit()
    db_session.refresh(category)
    return category


def _make_product(db_session, category_id, stock=10, price="9.99"):
    product = Product(
        name="Widget",
        description="A fine widget for all your widget needs",
        price=price,
        stock_quantity=stock,
        images=["https://example.com/widget.png"],
        characteristics={},
        category_id=category_id,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def _make_variant(db_session, product_id, sku="WIDGET-M-RED", stock=5, price_override=None):
    variant = ProductVariant(
        product_id=product_id,
        sku=sku,
        attributes={"size": "M", "color": "red"},
        price_override=price_override,
        stock_quantity=stock,
        is_active=True,
    )
    db_session.add(variant)
    db_session.commit()
    db_session.refresh(variant)
    return variant


def _products(db_session, count, **kwargs):
    category = _make_category(db_session)
    return [_make_product(db_session, category.id, **kwargs) for _ in range(count)]


def _add(visitor, product_id, quantity=1, variant_id=None, headers=None):
    params = {"product_id": product_id, "quantity": quantity}
    if variant_id is not None:
        params["variant_id"] = variant_id
    return visitor.post(f"{CART_PREFIX}/items", params=params, headers=headers)


def _cart(visitor, headers=None):
    response = visitor.get(CART_PREFIX, headers=headers)
    assert response.status_code == 200
    return response.json()["data"]


def _quantities(cart):
    return {(item["product_id"], item["variant_id"]): item["quantity"] for item in cart["items"]}


def _guest_keys(fake_redis):
    return sorted(fake_redis.scan_iter(f"{GUEST_CART_KEY_PREFIX}*"))


def _account_keys(fake_redis):
    return sorted(fake_redis.scan_iter(f"{CART_KEY_PREFIX}*"))


def _set_cookie(response):
    return response.headers.get("set-cookie")


def _assert_cookie_cleared(response):
    header = _set_cookie(response)
    assert header is not None
    assert header.startswith(f'{COOKIE}="";') or header.startswith(f"{COOKIE}=;")
    assert "Max-Age=0" in header


# ---------------------------------------------------------------------------
# The whole cart, as a guest
# ---------------------------------------------------------------------------

def test_guest_can_add_view_update_remove_clear_and_validate(browser, db_session):
    first, second = _products(db_session, 2)
    guest = browser()

    added = _add(guest, first.id, 2)
    assert added.status_code == 200
    assert _quantities(added.json()["data"]) == {(first.id, None): 2}
    assert _add(guest, second.id, 1).status_code == 200

    assert _quantities(_cart(guest)) == {(first.id, None): 2, (second.id, None): 1}

    updated = guest.put(f"{CART_PREFIX}/items/{first.id}", params={"quantity": 5})
    assert updated.status_code == 200
    assert _quantities(updated.json()["data"]) == {(first.id, None): 5, (second.id, None): 1}

    validated = guest.post(f"{CART_PREFIX}/validate")
    assert validated.status_code == 200
    assert validated.json()["data"] == {"valid": True, "issues": [], "price_changes": []}

    removed = guest.delete(f"{CART_PREFIX}/items/{second.id}")
    assert removed.status_code == 200
    assert _quantities(removed.json()["data"]) == {(first.id, None): 5}

    cleared = guest.delete(CART_PREFIX)
    assert cleared.status_code == 200
    assert cleared.json()["data"]["items"] == []
    assert _cart(guest)["items"] == []


def test_guest_cart_is_priced_live_like_an_account_cart(browser, db_session):
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="9.99")
    variant_product = _make_product(db_session, category.id, price="5.00")
    variant = _make_variant(db_session, variant_product.id, price_override="14.50")
    guest = browser()
    assert _add(guest, product.id, 2).json()["data"]["total_amount"] == "19.98"
    _add(guest, variant_product.id, 1, variant_id=variant.id)

    db_session.query(Product).filter(Product.id == product.id).update({"price": "12.50"})
    db_session.commit()

    cart = _cart(guest)
    line = next(item for item in cart["items"] if item["product_id"] == product.id)
    assert line["unit_price"] == "12.50"
    assert line["line_total"] == "25.00"
    assert line["price_when_added"] == "9.99"
    assert line["price_changed"] is True
    assert cart["total_amount"] == "39.50"

    validated = guest.post(f"{CART_PREFIX}/validate").json()["data"]
    assert validated["valid"] is True
    assert validated["price_changes"] == [{
        "product_id": product.id,
        "variant_id": None,
        "price_when_added": "9.99",
        "current_price": "12.50",
    }]


def test_guest_gets_the_same_rejections_as_an_account(browser, db_session):
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=2)
    guest = browser()

    assert _add(guest, 999999).status_code == 404
    assert _add(guest, product.id, 3).status_code == 400
    assert _add(guest, product.id, 0).status_code == 422
    assert _add(guest, product.id, 1).status_code == 200
    assert guest.put(f"{CART_PREFIX}/items/{product.id}", params={"quantity": 9}).status_code == 400

    db_session.query(Product).filter(Product.id == product.id).update({"stock_quantity": 0})
    db_session.commit()
    validated = guest.post(f"{CART_PREFIX}/validate").json()["data"]
    assert validated["valid"] is False
    assert [issue["code"] for issue in validated["issues"]] == ["insufficient_stock"]


def test_guest_mutation_returns_503_when_redis_fails(browser, db_session, monkeypatch):
    (product,) = _products(db_session, 1)
    guest = browser()
    _add(guest, product.id, 1)

    async def _raise(*args, **kwargs):
        raise RedisConnectionError("connection lost")
    monkeypatch.setattr(fakeredis.FakeAsyncRedis, "transaction", _raise)

    assert _add(guest, product.id, 1).status_code == 503
    assert _add(browser(), product.id, 1).status_code == 503
    assert guest.delete(CART_PREFIX).status_code == 503


# ---------------------------------------------------------------------------
# The identifier is created lazily
# ---------------------------------------------------------------------------

def test_looking_at_an_empty_cart_as_a_guest_stores_nothing_and_issues_no_identifier(
    browser, db_session, fake_redis
):
    (product,) = _products(db_session, 1)
    guest = browser()

    responses = [
        guest.get(CART_PREFIX),
        guest.post(f"{CART_PREFIX}/validate"),
        guest.delete(CART_PREFIX),
        guest.put(f"{CART_PREFIX}/items/{product.id}", params={"quantity": 1}),
        guest.delete(f"{CART_PREFIX}/items/{product.id}"),
        # Rejected adds do not create a cart either.
        _add(guest, 999999),
        _add(guest, product.id, 999),
    ]

    assert [response.status_code for response in responses] == [200, 200, 200, 404, 404, 404, 400]
    assert responses[0].json()["data"]["items"] == []
    assert responses[0].json()["data"]["total_amount"] == "0.00"
    assert responses[2].json()["data"]["items"] == []
    assert all(_set_cookie(response) is None for response in responses)
    assert len(guest.cookies) == 0
    assert list(fake_redis.scan_iter("*")) == []


def test_first_mutation_issues_the_identifier_and_the_next_request_sees_the_cart(
    browser, db_session, fake_redis
):
    (product,) = _products(db_session, 1)
    guest = browser()

    added = _add(guest, product.id, 2)

    guest_id = guest.cookies.get(COOKIE)
    # 32 random bytes from the server: 256 bits, 43 URL-safe characters.
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", guest_id)
    assert _guest_keys(fake_redis) == [f"{GUEST_CART_KEY_PREFIX}{guest_id}"]
    assert _account_keys(fake_redis) == []

    attributes = {part.strip().lower() for part in _set_cookie(added).split(";")[1:]}
    assert attributes == {"httponly", "secure", "samesite=lax", "path=/", f"max-age={SEVEN_DAYS}"}

    assert _quantities(_cart(guest)) == {(product.id, None): 2}
    # The identifier is not part of any response body.
    assert guest_id not in added.text


def test_the_stored_guest_cart_does_not_contain_its_own_identifier(browser, db_session, fake_redis):
    (product,) = _products(db_session, 1)
    guest = browser()
    _add(guest, product.id, 1)

    (key,) = _guest_keys(fake_redis)
    stored = fake_redis.get(key)

    assert guest.cookies.get(COOKIE) not in stored
    assert json.loads(stored)["user_id"] is None


def test_every_guest_write_renews_the_cookie_together_with_the_key(browser, db_session, fake_redis):
    first, second = _products(db_session, 2)
    guest = browser()
    issued = _set_cookie(_add(guest, first.id, 1))
    guest_id = guest.cookies.get(COOKIE)

    again = _add(guest, second.id, 1)
    updated = guest.put(f"{CART_PREFIX}/items/{first.id}", params={"quantity": 2})
    read = guest.get(CART_PREFIX)

    # Same identifier, lifetime started again; a read renews nothing.
    assert _set_cookie(again) == issued
    assert _set_cookie(updated) == issued
    assert _set_cookie(read) is None
    assert guest.cookies.get(COOKIE) == guest_id
    assert len(_guest_keys(fake_redis)) == 1


def test_an_emptied_guest_cart_is_not_kept_and_its_identifier_is_retired(browser, db_session, fake_redis):
    first, second = _products(db_session, 2)

    by_remove = browser()
    _add(by_remove, first.id, 1)
    removed = by_remove.delete(f"{CART_PREFIX}/items/{first.id}")
    assert removed.status_code == 200
    assert removed.json()["data"]["items"] == []
    _assert_cookie_cleared(removed)
    assert by_remove.cookies.get(COOKIE) is None

    by_clear = browser()
    _add(by_clear, first.id, 1)
    _add(by_clear, second.id, 1)
    cleared = by_clear.delete(CART_PREFIX)
    assert cleared.status_code == 200
    _assert_cookie_cleared(cleared)

    assert _guest_keys(fake_redis) == []


# ---------------------------------------------------------------------------
# One guest cannot reach another cart
# ---------------------------------------------------------------------------

def test_two_guests_never_see_each_others_carts(browser, db_session, fake_redis):
    first, second = _products(db_session, 2)
    alice, bob = browser(), browser()

    _add(alice, first.id, 2)
    assert _cart(bob)["items"] == []
    _add(bob, second.id, 3)

    assert _quantities(_cart(alice)) == {(first.id, None): 2}
    assert _quantities(_cart(bob)) == {(second.id, None): 3}
    assert alice.cookies.get(COOKIE) != bob.cookies.get(COOKIE)
    assert len(_guest_keys(fake_redis)) == 2

    bob.delete(CART_PREFIX)
    assert _quantities(_cart(alice)) == {(first.id, None): 2}


def _account_with_a_cart(browser, db_session, product_id, email="owner@example.com"):
    owner = browser()
    _register(owner, email)
    token = _login(owner, email).json()["data"]["access_token"]
    assert _add(owner, product_id, 4, headers=_auth_headers(token)).status_code == 200
    user_id = db_session.query(User).filter(User.email == email).one().id
    return token, user_id


@pytest.mark.parametrize("forged", [
    "{user_id}",                     # an account's cart key suffix
    "abc",
    "A" * 42,
    "A" * 44,
    "A" * 42 + "!",
    "A" * 42 + ":",
    "*" * 43,
])
def test_an_identifier_of_the_wrong_format_reads_and_creates_nothing(
    browser, db_session, fake_redis, forged
):
    product, other = _products(db_session, 2)
    token, user_id = _account_with_a_cart(browser, db_session, product.id)
    forged = forged.format(user_id=user_id)
    # A value sitting in Redis under the forged name, in both namespaces'
    # spelling, must stay out of reach as well.
    fake_redis.set(f"{GUEST_CART_KEY_PREFIX}{forged}", fake_redis.get(f"{CART_KEY_PREFIX}{user_id}"))
    before = {key: fake_redis.get(key) for key in fake_redis.scan_iter("*cart:*")}

    intruder = browser()
    intruder.cookies.set(COOKIE, forged, domain="testserver.local")

    assert _cart(intruder)["items"] == []
    assert intruder.post(f"{CART_PREFIX}/validate").json()["data"]["valid"] is True
    assert intruder.put(f"{CART_PREFIX}/items/{product.id}", params={"quantity": 1}).status_code == 404
    assert intruder.delete(f"{CART_PREFIX}/items/{product.id}").status_code == 404
    assert intruder.delete(CART_PREFIX).json()["data"]["items"] == []
    assert {key: fake_redis.get(key) for key in fake_redis.scan_iter("*cart:*")} == before

    added = _add(intruder, other.id, 1)

    assert added.status_code == 200
    assert _quantities(added.json()["data"]) == {(other.id, None): 1}
    issued = _set_cookie(added).split(";")[0].split("=", 1)[1]
    assert issued != forged
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", issued)
    assert fake_redis.get(f"{GUEST_CART_KEY_PREFIX}{forged}") == before[f"{GUEST_CART_KEY_PREFIX}{forged}"]
    assert _quantities(_cart(intruder, headers=_auth_headers(token))) == {(product.id, None): 4}


def test_a_well_formed_identifier_the_server_never_issued_creates_nothing(
    browser, db_session, fake_redis
):
    """An identifier is only ever stored under when the server made it up
    itself in that same request. A client that presents one of its own
    (or an expired one) gets a new identifier on its first add; nothing is
    ever stored under the one it chose."""
    (product,) = _products(db_session, 1)
    chosen = secrets.token_urlsafe(32)
    guest = browser()
    guest.cookies.set(COOKIE, chosen, domain="testserver.local")

    assert _cart(guest)["items"] == []
    assert guest.put(f"{CART_PREFIX}/items/{product.id}", params={"quantity": 1}).status_code == 404
    assert guest.delete(f"{CART_PREFIX}/items/{product.id}").status_code == 404
    assert guest.cookies.get(COOKIE) == chosen
    cleared = guest.delete(CART_PREFIX)
    assert cleared.status_code == 200
    # Clearing a cart that is not there stores nothing, and tells the client
    # to drop the cookie that names it.
    _assert_cookie_cleared(cleared)
    assert list(fake_redis.scan_iter("*")) == []

    # The add is made while still presenting the chosen identifier.
    guest.cookies.set(COOKIE, chosen, domain="testserver.local")
    added = _add(guest, product.id, 1)

    assert added.status_code == 200
    issued = _set_cookie(added).split(";")[0].split("=", 1)[1]
    assert issued != chosen
    assert _guest_keys(fake_redis) == [f"{GUEST_CART_KEY_PREFIX}{issued}"]
    assert guest.cookies.get(COOKIE) == issued


def test_a_guest_cart_expired_in_redis_starts_again_under_a_new_identifier(
    browser, db_session, fake_redis
):
    first, second = _products(db_session, 2)
    guest = browser()
    _add(guest, first.id, 1)
    old_id = guest.cookies.get(COOKIE)
    fake_redis.delete(f"{GUEST_CART_KEY_PREFIX}{old_id}")

    added = _add(guest, second.id, 1)

    new_id = guest.cookies.get(COOKIE)
    assert added.status_code == 200
    assert new_id != old_id
    assert _guest_keys(fake_redis) == [f"{GUEST_CART_KEY_PREFIX}{new_id}"]
    assert _quantities(_cart(guest)) == {(second.id, None): 1}


# ---------------------------------------------------------------------------
# Whose cart is this: an Authorization header is never a guest
# ---------------------------------------------------------------------------

def _expired_token(user_id):
    payload = {
        "exp": utc_now() - timedelta(minutes=5),
        "iat": utc_now() - timedelta(minutes=20),
        "sub": str(user_id),
        "type": "access",
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY.get_secret_value(), algorithm=settings.JWT_ALGORITHM)


def _refresh_typed_token(user_id):
    payload = {
        "exp": utc_now() + timedelta(minutes=5),
        "iat": utc_now(),
        "sub": str(user_id),
        "type": "refresh",
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY.get_secret_value(), algorithm=settings.JWT_ALGORITHM)


def _cart_calls(visitor, product_id, headers):
    return [
        visitor.get(CART_PREFIX, headers=headers),
        _add(visitor, product_id, 1, headers=headers),
        visitor.put(f"{CART_PREFIX}/items/{product_id}", params={"quantity": 2}, headers=headers),
        visitor.delete(f"{CART_PREFIX}/items/{product_id}", headers=headers),
        visitor.delete(CART_PREFIX, headers=headers),
        visitor.post(f"{CART_PREFIX}/validate", headers=headers),
    ]


@pytest.mark.parametrize("kind", [
    "garbage", "expired", "refresh_token", "blacklisted", "unknown_user",
    "basic_scheme", "bearer_without_token", "empty_header",
])
def test_a_bad_authorization_header_is_a_401_on_every_cart_route_never_a_guest_cart(
    browser, db_session, fake_redis, kind
):
    (product,) = _products(db_session, 1)
    visitor = browser()
    _register(visitor)
    user_id = db_session.query(User).one().id
    # The visitor also holds a perfectly good guest cart, which is what a
    # silent fallback would have served.
    _add(visitor, product.id, 3)
    guest_key = _guest_keys(fake_redis)[0]
    stored = fake_redis.get(guest_key)

    if kind == "blacklisted":
        token = create_access_token(user_id)
        blacklist_token(token, 600)
    header = {
        "garbage": "Bearer not-a-jwt",
        "expired": f"Bearer {_expired_token(user_id)}",
        "refresh_token": f"Bearer {_refresh_typed_token(user_id)}",
        "blacklisted": f"Bearer {token if kind == 'blacklisted' else ''}",
        "unknown_user": f"Bearer {create_access_token(user_id + 1000)}",
        "basic_scheme": "Basic dXNlcjpwYXNz",
        "bearer_without_token": "Bearer",
        "empty_header": "",
    }[kind]

    responses = _cart_calls(visitor, product.id, {"Authorization": header})

    assert [response.status_code for response in responses] == [401] * 6
    # A token that was presented and rejected is named (RFC 6750 section 3);
    # a header with no token in it gets the bare challenge.
    challenge = (
        'Bearer error="invalid_token"'
        if kind in ("garbage", "expired", "refresh_token", "blacklisted", "unknown_user")
        else "Bearer"
    )
    assert all(response.headers["www-authenticate"] == challenge for response in responses)
    assert all(_set_cookie(response) is None for response in responses)
    assert fake_redis.get(guest_key) == stored
    assert _account_keys(fake_redis) == []


def test_an_inactive_account_is_rejected_on_cart_routes_as_before(browser, db_session):
    (product,) = _products(db_session, 1)
    visitor = browser()
    _register(visitor)
    token = _login(visitor).json()["data"]["access_token"]
    db_session.query(User).update({User.is_active: False})
    db_session.commit()

    responses = _cart_calls(visitor, product.id, _auth_headers(token))

    assert [response.status_code for response in responses] == [400] * 6
    assert responses[0].json()["detail"] == "Inactive user"


def test_an_account_request_uses_the_account_cart_and_leaves_a_guest_cookie_alone(
    browser, db_session, fake_redis, monkeypatch
):
    """A logged-in client can still be holding a guest cookie (its merge
    failed at login). Its requests are account requests all the same."""
    guest_product, account_product = _products(db_session, 2)
    visitor = browser()
    _register(visitor)
    _add(visitor, guest_product.id, 2)
    guest_id = visitor.cookies.get(COOKIE)
    async def _not_merged(self, guest_id, user_id):
        return False

    monkeypatch.setattr(AsyncRedisService, "merge_guest_cart", _not_merged)
    token = _login(visitor).json()["data"]["access_token"]
    headers = _auth_headers(token)

    added = _add(visitor, account_product.id, 1, headers=headers)

    assert _set_cookie(added) is None
    assert _quantities(_cart(visitor, headers=headers)) == {(account_product.id, None): 1}
    assert visitor.delete(CART_PREFIX, headers=headers).status_code == 200
    assert visitor.cookies.get(COOKIE) == guest_id
    assert _quantities(_cart(visitor)) == {(guest_product.id, None): 2}


# ---------------------------------------------------------------------------
# Lifetimes
# ---------------------------------------------------------------------------

def test_guest_and_account_carts_each_have_their_own_ttl(browser, db_session, fake_redis):
    (product,) = _products(db_session, 1)
    guest = browser()
    _add(guest, product.id, 1)
    token, user_id = _account_with_a_cart(browser, db_session, product.id)

    guest_ttl = fake_redis.ttl(_guest_keys(fake_redis)[0])
    account_ttl = fake_redis.ttl(f"{CART_KEY_PREFIX}{user_id}")

    assert SEVEN_DAYS - 60 < guest_ttl <= SEVEN_DAYS
    assert THIRTY_DAYS - 60 < account_ttl <= THIRTY_DAYS


def test_guest_ttl_default_is_seven_days_and_the_account_default_is_unchanged():
    assert Settings.model_fields["REDIS_GUEST_CART_TTL_DAYS"].default == 7
    assert Settings.model_fields["REDIS_CART_TTL_DAYS"].default == 30


def test_guest_ttl_setting_is_the_one_source_for_key_cookie_and_expires_at(
    browser, db_session, fake_redis, monkeypatch
):
    monkeypatch.setattr(settings, "REDIS_GUEST_CART_TTL_DAYS", 2)
    monkeypatch.setattr(settings, "REDIS_CART_TTL_DAYS", 11)
    two_days = 2 * 24 * 3600
    (product,) = _products(db_session, 1)
    guest = browser()

    added = _add(guest, product.id, 1)

    (key,) = _guest_keys(fake_redis)
    assert two_days - 60 < fake_redis.ttl(key) <= two_days
    assert f"Max-Age={two_days}" in _set_cookie(added)
    expires_at = datetime.fromisoformat(json.loads(fake_redis.get(key))["expires_at"])
    assert abs((expires_at - utc_now()).total_seconds() - two_days) < 60

    _, user_id = _account_with_a_cart(browser, db_session, product.id)
    eleven_days = 11 * 24 * 3600
    assert eleven_days - 60 < fake_redis.ttl(f"{CART_KEY_PREFIX}{user_id}") <= eleven_days


def test_cookie_is_not_marked_secure_only_when_configured_so(client, db_session, monkeypatch):
    """For local development over plain http. The cookie then also loses
    the __Host- prefix, which a browser only accepts on a Secure cookie."""
    monkeypatch.setattr(settings, "GUEST_CART_COOKIE_SECURE", False)
    (product,) = _products(db_session, 1)

    added = _add(client, product.id, 1)

    header = _set_cookie(added)
    assert header.startswith("guest_cart=")
    attributes = {part.strip().lower() for part in header.split(";")[1:]}
    assert attributes == {"httponly", "samesite=lax", "path=/", f"max-age={SEVEN_DAYS}"}
    # `client` speaks plain http, and the cart is there on the next request.
    assert _quantities(_cart(client)) == {(product.id, None): 1}


def test_a_secure_cookie_is_not_carried_over_plain_http(client, db_session):
    (product,) = _products(db_session, 1)

    added = _add(client, product.id, 1)

    assert "secure" in _set_cookie(added).lower()
    assert _cart(client)["items"] == []


# ---------------------------------------------------------------------------
# Merge at login
# ---------------------------------------------------------------------------

def test_login_merges_the_guest_cart_into_the_account_cart(browser, db_session, fake_redis):
    in_both, guest_only, account_only = _products(db_session, 3)
    visitor = browser()
    token, user_id = _account_with_a_cart(browser, db_session, in_both.id)  # 4 of in_both
    _add(visitor, account_only.id, 1, headers=_auth_headers(token))
    _add(visitor, in_both.id, 2)
    _add(visitor, guest_only.id, 3)
    guest_id = visitor.cookies.get(COOKIE)
    assert len(_guest_keys(fake_redis)) == 1

    response = _login(visitor, "owner@example.com")

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["token_type"] == "bearer"
    # A line present in both carts: the quantities are added up.
    merged = _cart(visitor, headers=_auth_headers(body["access_token"]))
    assert _quantities(merged) == {
        (in_both.id, None): 6,
        (account_only.id, None): 1,
        (guest_only.id, None): 3,
    }
    assert merged["items_count"] == 3
    # The guest cart is gone and so is its identifier.
    assert _guest_keys(fake_redis) == []
    _assert_cookie_cleared(response)
    assert visitor.cookies.get(COOKIE) is None
    assert guest_id not in response.text
    assert _cart(visitor)["items"] == []
    # What was merged lives as long as an account cart does.
    assert THIRTY_DAYS - 60 < fake_redis.ttl(f"{CART_KEY_PREFIX}{user_id}") <= THIRTY_DAYS
    assert json.loads(fake_redis.get(f"{CART_KEY_PREFIX}{user_id}"))["user_id"] == user_id


def test_login_merges_into_an_account_that_had_no_cart(browser, db_session, fake_redis):
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="9.99")
    variant = _make_variant(db_session, product.id, price_override="14.50")
    visitor = browser()
    _register(visitor)
    _add(visitor, product.id, 2)
    _add(visitor, product.id, 1, variant_id=variant.id)

    token = _login(visitor).json()["data"]["access_token"]

    cart = _cart(visitor, headers=_auth_headers(token))
    assert _quantities(cart) == {(product.id, None): 2, (product.id, variant.id): 1}
    assert cart["total_amount"] == "34.48"
    assert _guest_keys(fake_redis) == []
    assert len(_account_keys(fake_redis)) == 1


def test_login_without_a_guest_cart_behaves_as_before(browser, db_session, fake_redis):
    (product,) = _products(db_session, 1)
    token, user_id = _account_with_a_cart(browser, db_session, product.id)
    stored = fake_redis.get(f"{CART_KEY_PREFIX}{user_id}")
    ttl = fake_redis.ttl(f"{CART_KEY_PREFIX}{user_id}")
    visitor = browser()

    response = _login(visitor, "owner@example.com")

    assert response.status_code == 200
    assert set(response.json()["data"]) == {"access_token", "refresh_token", "token_type"}
    assert _set_cookie(response) is None
    assert len(visitor.cookies) == 0
    # The account cart was not rewritten, nor its lifetime renewed.
    assert fake_redis.get(f"{CART_KEY_PREFIX}{user_id}") == stored
    assert fake_redis.ttl(f"{CART_KEY_PREFIX}{user_id}") <= ttl


@pytest.mark.parametrize("presented", ["unknown", "malformed"])
def test_login_with_an_identifier_that_has_no_cart_changes_no_cart(
    browser, db_session, fake_redis, presented
):
    (product,) = _products(db_session, 1)
    token, user_id = _account_with_a_cart(browser, db_session, product.id)
    stored = fake_redis.get(f"{CART_KEY_PREFIX}{user_id}")
    visitor = browser()
    value = secrets.token_urlsafe(32) if presented == "unknown" else str(user_id)
    visitor.cookies.set(COOKIE, value, domain="testserver.local")

    response = _login(visitor, "owner@example.com")

    assert response.status_code == 200
    assert fake_redis.get(f"{CART_KEY_PREFIX}{user_id}") == stored
    assert _guest_keys(fake_redis) == []
    # Either way the client is told to drop a cookie that names no cart.
    _assert_cookie_cleared(response)


def test_login_does_not_create_an_account_cart_out_of_nothing(browser, db_session, fake_redis):
    visitor = browser()
    _register(visitor)
    visitor.cookies.set(COOKIE, secrets.token_urlsafe(32), domain="testserver.local")

    assert _login(visitor).status_code == 200

    assert _account_keys(fake_redis) == []


def test_failed_login_leaves_the_guest_cart_and_its_cookie_alone(browser, db_session, fake_redis):
    (product,) = _products(db_session, 1)
    visitor = browser()
    _register(visitor)
    _add(visitor, product.id, 2)
    guest_id = visitor.cookies.get(COOKIE)

    wrong_password = _login(visitor, password="Wr0ngPass1")
    unknown_account = _login(visitor, email="nobody@example.com")

    assert wrong_password.status_code == 401
    assert unknown_account.status_code == 401
    assert _set_cookie(wrong_password) is None
    assert visitor.cookies.get(COOKIE) == guest_id
    assert _quantities(_cart(visitor)) == {(product.id, None): 2}
    assert _account_keys(fake_redis) == []


def test_login_to_a_deactivated_account_does_not_take_the_guest_cart(browser, db_session, fake_redis):
    """login refuses a deactivated account like a wrong password, so the
    guest cart stays the visitor's: a cart merged into that account would
    be lost to them."""
    (product,) = _products(db_session, 1)
    visitor = browser()
    _register(visitor)
    db_session.query(User).update({User.is_active: False})
    db_session.commit()
    _add(visitor, product.id, 2)
    guest_id = visitor.cookies.get(COOKIE)

    response = _login(visitor)

    assert response.status_code == 401
    assert _set_cookie(response) is None
    assert visitor.cookies.get(COOKIE) == guest_id
    assert _quantities(_cart(visitor)) == {(product.id, None): 2}
    assert _account_keys(fake_redis) == []


def test_merge_does_not_fail_a_login_over_stock(browser, db_session):
    """4 in the account cart and 3 in the guest cart of a product with 5 in
    stock: the login succeeds with 7 in the cart, and validation says what
    it has always said about a cart that asks for more than there is."""
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=5)
    visitor = browser()
    _account_with_a_cart(browser, db_session, product.id)
    _add(visitor, product.id, 3)

    response = _login(visitor, "owner@example.com")

    assert response.status_code == 200
    headers = _auth_headers(response.json()["data"]["access_token"])
    assert _quantities(_cart(visitor, headers=headers)) == {(product.id, None): 7}
    validated = visitor.post(f"{CART_PREFIX}/validate", headers=headers).json()["data"]
    assert validated["valid"] is False
    assert validated["issues"] == [{
        "product_id": product.id,
        "variant_id": None,
        "code": "insufficient_stock",
        "error": "Not enough stock",
        "available": 5,
        "requested": 7,
    }]
    # And the customer can bring the line back within stock.
    fixed = visitor.put(f"{CART_PREFIX}/items/{product.id}", params={"quantity": 5}, headers=headers)
    assert fixed.status_code == 200


def test_merged_line_records_the_price_last_seen_as_a_guest(browser, db_session):
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="9.99")
    visitor = browser()
    _account_with_a_cart(browser, db_session, product.id)
    db_session.query(Product).filter(Product.id == product.id).update({"price": "12.50"})
    db_session.commit()
    _add(visitor, product.id, 1)  # seen at 12.50, a moment before logging in

    token = _login(visitor, "owner@example.com").json()["data"]["access_token"]

    line = _cart(visitor, headers=_auth_headers(token))["items"][0]
    assert (line["quantity"], line["price_when_added"], line["price_changed"]) == (5, "12.50", False)


# --- Atomicity ---------------------------------------------------------------

def _inject_during_merge(monkeypatch, concurrent_write):
    """Run `concurrent_write` once, between the merge's read of both carts
    and its write - Cart.merge is exactly that point."""
    real_merge = Cart.merge
    calls = []

    def _merge_after_a_concurrent_write(self, other, max_lines):
        if not calls:
            concurrent_write()
        calls.append(1)
        return real_merge(self, other, max_lines)

    monkeypatch.setattr(Cart, "merge", _merge_after_a_concurrent_write)
    return calls


def _service_add(owner, product_id, quantity):
    cart = RedisService().add_to_cart_atomic(
        owner, product_id=product_id, quantity=quantity, price=Decimal("9.99"),
        name="Widget", image="", stock_quantity=100,
    )
    assert cart is not None


def test_an_add_to_the_account_cart_during_the_merge_is_not_lost(
    browser, db_session, fake_redis, monkeypatch
):
    guest_product, account_product, late = _products(db_session, 3)
    visitor = browser()
    token, user_id = _account_with_a_cart(browser, db_session, account_product.id)
    _add(visitor, guest_product.id, 2)
    # Another tab of the same account adds a line while the merge is between
    # its read and its write.
    calls = _inject_during_merge(monkeypatch, lambda: _service_add(user_id, late.id, 5))

    response = _login(visitor, "owner@example.com")

    assert response.status_code == 200
    assert len(calls) == 2  # the first attempt was thrown away and redone
    assert _quantities(_cart(visitor, headers=_auth_headers(token))) == {
        (account_product.id, None): 4,
        (guest_product.id, None): 2,
        (late.id, None): 5,
    }
    assert _guest_keys(fake_redis) == []


def test_an_add_to_the_guest_cart_during_the_merge_is_not_lost(
    browser, db_session, fake_redis, monkeypatch
):
    from app.schemas.cart import CartOwner

    guest_product, account_product, late = _products(db_session, 3)
    visitor = browser()
    token, user_id = _account_with_a_cart(browser, db_session, account_product.id)
    _add(visitor, guest_product.id, 2)
    guest = CartOwner.guest(visitor.cookies.get(COOKIE))

    def _another_tab_adds():
        _service_add(guest, late.id, 5)
        _service_add(guest, guest_product.id, 1)

    calls = _inject_during_merge(monkeypatch, _another_tab_adds)

    response = _login(visitor, "owner@example.com")

    assert response.status_code == 200
    assert len(calls) == 2
    assert _quantities(_cart(visitor, headers=_auth_headers(token))) == {
        (account_product.id, None): 4,
        (guest_product.id, None): 3,
        (late.id, None): 5,
    }
    # Nothing of the guest cart is left behind, and nothing was merged twice.
    assert _guest_keys(fake_redis) == []


def test_two_logins_carrying_the_same_guest_cart_merge_it_once(browser, db_session, fake_redis):
    (product,) = _products(db_session, 1)
    visitor = browser()
    _register(visitor)
    _add(visitor, product.id, 2)
    guest_id = visitor.cookies.get(COOKIE)

    first = _login(visitor)
    replay = browser()
    replay.cookies.set(COOKIE, guest_id, domain="testserver.local")
    second = _login(replay)

    assert (first.status_code, second.status_code) == (200, 200)
    token = second.json()["data"]["access_token"]
    assert _quantities(_cart(replay, headers=_auth_headers(token))) == {(product.id, None): 2}


# --- Redis failure -------------------------------------------------------------

def test_redis_failing_during_the_merge_does_not_fail_the_login(
    browser, db_session, fake_redis, monkeypatch
):
    guest_product, account_product = _products(db_session, 2)
    visitor = browser()
    token, user_id = _account_with_a_cart(browser, db_session, account_product.id)
    _add(visitor, guest_product.id, 2)
    guest_id = visitor.cookies.get(COOKIE)
    guest_key = f"{GUEST_CART_KEY_PREFIX}{guest_id}"
    stored_guest = fake_redis.get(guest_key)
    stored_account = fake_redis.get(f"{CART_KEY_PREFIX}{user_id}")

    async def _raise(*args, **kwargs):
        raise RedisConnectionError("connection lost")

    with monkeypatch.context() as broken:
        broken.setattr(fakeredis.FakeAsyncRedis, "transaction", _raise)
        response = _login(visitor, "owner@example.com")

    # Logged in, with working tokens.
    assert response.status_code == 200
    new_token = response.json()["data"]["access_token"]
    assert visitor.get(f"{USERS_PREFIX}/me", headers=_auth_headers(new_token)).status_code == 200
    # Both carts exactly where they were, and the identifier kept.
    assert fake_redis.get(guest_key) == stored_guest
    assert fake_redis.get(f"{CART_KEY_PREFIX}{user_id}") == stored_account
    assert _set_cookie(response) is None
    assert visitor.cookies.get(COOKIE) == guest_id

    # The merge happens on a later login.
    later = _login(visitor, "owner@example.com")

    assert later.status_code == 200
    _assert_cookie_cleared(later)
    assert _guest_keys(fake_redis) == []
    assert _quantities(_cart(visitor, headers=_auth_headers(new_token))) == {
        (account_product.id, None): 4,
        (guest_product.id, None): 2,
    }


def test_an_unexpected_error_in_the_merge_does_not_fail_the_login_either(
    browser, db_session, fake_redis, monkeypatch
):
    (product,) = _products(db_session, 1)
    visitor = browser()
    _register(visitor)
    _add(visitor, product.id, 2)

    async def _boom(self, guest_id, user_id):
        raise RuntimeError("not a Redis error")

    monkeypatch.setattr(AsyncRedisService, "merge_guest_cart", _boom)

    response = _login(visitor)

    assert response.status_code == 200
    assert _set_cookie(response) is None
    assert len(_guest_keys(fake_redis)) == 1


# ---------------------------------------------------------------------------
# Line cap
# ---------------------------------------------------------------------------

def test_line_cap_default():
    assert Settings.model_fields["CART_MAX_LINES"].default == 100


def test_guest_cart_refuses_a_line_beyond_the_cap(browser, db_session, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "CART_MAX_LINES", 3)
    category = _make_category(db_session)
    products = [_make_product(db_session, category.id) for _ in range(3)]
    variant = _make_variant(db_session, products[0].id)
    guest = browser()
    for product in products:
        assert _add(guest, product.id, 1).status_code == 200

    one_more = _make_product(db_session, category.id)
    refused = _add(guest, one_more.id, 1)
    # A variant of a product already in the cart is a line of its own.
    refused_variant = _add(guest, products[0].id, 1, variant_id=variant.id)

    assert refused.status_code == 400
    assert refused.json()["detail"] == "Cart cannot hold more than 3 different items"
    assert refused_variant.status_code == 400
    assert _cart(guest)["items_count"] == 3
    # More of a line already there is not a new line.
    assert _add(guest, products[0].id, 2).status_code == 200
    # And room freed is room again.
    guest.delete(f"{CART_PREFIX}/items/{products[1].id}")
    assert _add(guest, one_more.id, 1).status_code == 200


def test_account_cart_refuses_a_line_beyond_the_cap(browser, db_session, monkeypatch):
    monkeypatch.setattr(settings, "CART_MAX_LINES", 2)
    first, second, third = _products(db_session, 3)
    visitor = browser()
    _register(visitor)
    headers = _auth_headers(_login(visitor).json()["data"]["access_token"])
    _add(visitor, first.id, 1, headers=headers)
    _add(visitor, second.id, 1, headers=headers)

    refused = _add(visitor, third.id, 1, headers=headers)

    assert refused.status_code == 400
    assert refused.json()["detail"] == "Cart cannot hold more than 2 different items"
    assert _add(visitor, first.id, 1, headers=headers).status_code == 200
    assert _quantities(_cart(visitor, headers=headers)) == {(first.id, None): 2, (second.id, None): 1}


def test_merge_keeps_the_account_cart_within_the_cap(browser, db_session, fake_redis, monkeypatch):
    """The login cannot fail, so the merge cannot refuse: lines already in
    the account cart stay, a line present in both is added up (it is no new
    line), and guest-only lines fill what room is left, oldest first."""
    monkeypatch.setattr(settings, "CART_MAX_LINES", 3)
    shared, account_only, guest_first, guest_second = _products(db_session, 4)
    visitor = browser()
    token, user_id = _account_with_a_cart(browser, db_session, shared.id)
    _add(visitor, account_only.id, 1, headers=_auth_headers(token))
    _add(visitor, guest_first.id, 1)
    _add(visitor, shared.id, 2)
    _add(visitor, guest_second.id, 1)

    response = _login(visitor, "owner@example.com")

    assert response.status_code == 200
    assert _quantities(_cart(visitor, headers=_auth_headers(token))) == {
        (shared.id, None): 6,
        (account_only.id, None): 1,
        (guest_first.id, None): 1,
    }
    assert _guest_keys(fake_redis) == []


# ---------------------------------------------------------------------------
# Checkout stays account-only
# ---------------------------------------------------------------------------

def _make_checkout_ready(visitor, db_session, token):
    visitor.put(
        f"{USERS_PREFIX}/me",
        json={"full_name": "Guest Buyer", "phone": "+12025550123"},
        headers=_auth_headers(token),
    )
    db_session.query(User).update({User.is_email_verified: True})
    db_session.commit()
    address = visitor.post(
        f"{USERS_PREFIX}/addresses",
        json={
            "street": "123 Main Street",
            "city": "Springfield",
            "state": "Illinois",
            "postal_code": "62701",
            "country": "US",
        },
        headers=_auth_headers(token),
    )
    return address.json()["data"]["id"]


def test_a_guest_cannot_place_an_order(browser, db_session, fake_redis):
    (product,) = _products(db_session, 1)
    guest = browser()
    _add(guest, product.id, 2)
    stored = fake_redis.get(_guest_keys(fake_redis)[0])

    response = guest.post(
        ORDERS_PREFIX,
        params={"shipping_address_id": 1},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert fake_redis.get(_guest_keys(fake_redis)[0]) == stored
    db_session.refresh(product)
    assert product.stock_quantity == 10


def test_after_login_the_order_contains_the_merged_cart(browser, db_session, fake_redis):
    category = _make_category(db_session)
    in_both = _make_product(db_session, category.id, stock=10, price="10.00")
    guest_only = _make_product(db_session, category.id, stock=10, price="2.50")
    visitor = browser()
    _register(visitor)
    first_token = _login(visitor).json()["data"]["access_token"]
    address_id = _make_checkout_ready(visitor, db_session, first_token)
    _add(visitor, in_both.id, 1, headers=_auth_headers(first_token))
    # Back as a guest (the token is simply not sent), the visitor shops on.
    _add(visitor, in_both.id, 2)
    _add(visitor, guest_only.id, 4)

    token = _login(visitor).json()["data"]["access_token"]
    response = visitor.post(
        ORDERS_PREFIX,
        params={"shipping_address_id": address_id},
        headers={"Idempotency-Key": uuid.uuid4().hex, **_auth_headers(token)},
    )

    assert response.status_code == 200
    order = response.json()["data"]
    assert {item["product_id"]: item["quantity"] for item in order["items"]} == {
        in_both.id: 3,
        guest_only.id: 4,
    }
    assert Decimal(str(order["total_amount"])) == Decimal("40.00")
    db_session.refresh(in_both)
    db_session.refresh(guest_only)
    assert (in_both.stock_quantity, guest_only.stock_quantity) == (7, 6)
    assert _cart(visitor, headers=_auth_headers(token))["items"] == []
    assert _guest_keys(fake_redis) == []


# ---------------------------------------------------------------------------
# Carts already in Redis
# ---------------------------------------------------------------------------

def _store_cart_as_written_today(fake_redis, user_id, product_id, quantity, price):
    """The exact JSON an account cart has in Redis today: an integer
    user_id, a decimal-string price, no field this change introduces."""
    payload = (
        '{"user_id": %d, "items": {"%d": {"product_id": %d, "variant_id": null, '
        '"quantity": %d, "added_at": "2026-10-09T08:15:30.123456", "price_snapshot": "%s", '
        '"name_snapshot": "Widget", "image_snapshot": "https://example.com/widget.png"}}, '
        '"created_at": "2026-10-09T08:15:30.123456", "updated_at": "2026-10-09T08:15:30.123456", '
        '"expires_at": "2026-11-08T08:15:30.123456"}'
    ) % (user_id, product_id, product_id, quantity, price)
    fake_redis.set(f"{CART_KEY_PREFIX}{user_id}", payload, ex=20 * 24 * 3600)


def test_an_account_cart_stored_in_the_current_format_still_loads(browser, db_session, fake_redis):
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, price="9.99")
    other = _make_product(db_session, category.id, price="1.00")
    visitor = browser()
    _register(visitor)
    token = _login(visitor).json()["data"]["access_token"]
    user_id = db_session.query(User).one().id
    _store_cart_as_written_today(fake_redis, user_id, product.id, 2, "9.99")
    headers = _auth_headers(token)

    cart = _cart(visitor, headers=headers)

    assert _quantities(cart) == {(product.id, None): 2}
    assert cart["total_amount"] == "19.98"
    assert cart["items"][0]["added_at"].startswith("2026-10-09T08:15:30")
    assert cart["items"][0]["price_changed"] is False
    # Still a cart that can be changed, validated - and merged into.
    assert _add(visitor, other.id, 1, headers=headers).json()["data"]["total_amount"] == "20.98"
    assert visitor.post(f"{CART_PREFIX}/validate", headers=headers).json()["data"]["valid"] is True
    _add(visitor, product.id, 1)
    again = _login(visitor).json()["data"]["access_token"]
    assert _quantities(_cart(visitor, headers=_auth_headers(again))) == {
        (product.id, None): 3,
        (other.id, None): 1,
    }
