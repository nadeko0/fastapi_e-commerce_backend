"""A Redis that accepts the connection and then never answers.

tests/unit/test_redis_timeouts.py pins the configured socket timeouts as
numbers. This module proves what they are for, on a real socket: a TCP
server that accepts and stays silent (what a Redis stuck in a long command,
a half-open connection through a NAT or a frozen container looks like to the
client). Nothing here is mocked below the socket.

- The clients give up with redis.exceptions.TimeoutError after the
  configured socket_timeout (redis-py 8 default, 5 s), not never. Both
  clients, each with the pool settings the application builds.
- The application turns that into its documented answers: a token that
  cannot be checked is refused with 503 and Retry-After (never served, and
  never the 401 of a revoked token), the login count that cannot be made
  answers 503 with Retry-After, and the services degrade the way their
  docstrings say.

The first two tests wait the real 5 s each. The rest shorten the socket
timeout of the already-built client (the pool's connection kwargs) to a
fraction of a second, which changes how long they wait and nothing else.
"""

import asyncio
import socket
import threading
import time

import fakeredis
import pytest
import redis
from redis.exceptions import TimeoutError as RedisTimeoutError

import app.services.redis_async as redis_async
from app.core.config import settings
from app.core.security import get_password_hash
from app.models.category import Category
from app.models.enums import UserRole
from app.models.product import Product
from app.models.user import User
from app.services.redis import RedisService
from app.services.redis_async import (
    AsyncRedisService,
    TokenStateUnavailable,
    _create_client,
    is_token_blacklisted,
)

API = settings.API_V1_STR
SHORT = 0.3  # seconds: the socket timeout of the fast tests
CONFIGURED = 5  # seconds: redis-py 8's default, pinned in tests/unit/test_redis_timeouts.py
# A timeout fires at its time, never before; scheduling and the Windows timer
# add a little. The upper bound only has to separate "gave up" from "hung".
MARGIN = 2.5


class HungServer:
    """Listens, accepts, keeps every connection open and says nothing."""

    def __init__(self):
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.port = self._listener.getsockname()[1]
        self.accepted = 0
        self._connections = []
        self._thread = threading.Thread(target=self._accept_forever, daemon=True)
        self._thread.start()

    def _accept_forever(self):
        while True:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            self._connections.append(connection)
            self.accepted += 1

    def close(self):
        self._listener.close()
        for connection in self._connections:
            connection.close()
        self._thread.join(timeout=5)


@pytest.fixture
def hung_redis(monkeypatch):
    """Point the settings the clients are built from at a silent server."""
    server = HungServer()
    monkeypatch.setattr(settings, "REDIS_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "REDIS_PORT", server.port)
    yield server
    server.close()


@pytest.fixture
def real_sync_service(monkeypatch, hung_redis):
    """RedisService on a real pool and the real redis.Redis (the suite
    replaces both with fakeredis)."""
    monkeypatch.setattr("app.services.redis.Redis", redis.Redis)
    monkeypatch.setattr(RedisService, "_instance", None)
    monkeypatch.setattr(RedisService, "_pool", None)
    service = RedisService()
    yield service
    service._pool.disconnect()


def _shorten(connection_pool):
    connection_pool.connection_kwargs["socket_timeout"] = SHORT
    connection_pool.connection_kwargs["socket_connect_timeout"] = SHORT


# --- The clients, with the application's own settings ------------------------------------


def test_the_sync_client_gives_up_after_its_socket_timeout(hung_redis, real_sync_service):
    started = time.monotonic()
    with pytest.raises(RedisTimeoutError):
        real_sync_service._redis.get("anything")
    elapsed = time.monotonic() - started

    assert hung_redis.accepted >= 1  # it did connect: the wait was for the answer
    assert CONFIGURED - 0.2 <= elapsed < CONFIGURED + MARGIN


def test_the_async_client_gives_up_after_its_socket_timeout(hung_redis):
    async def scenario():
        client = _create_client()
        started = time.monotonic()
        try:
            with pytest.raises(RedisTimeoutError):
                await client.get("anything")
            return time.monotonic() - started
        finally:
            await client.aclose()

    elapsed = asyncio.run(scenario())

    assert hung_redis.accepted >= 1
    assert CONFIGURED - 0.2 <= elapsed < CONFIGURED + MARGIN


# --- What the application makes of it ------------------------------------------------------


def test_the_sync_service_degrades_as_documented_when_redis_hangs(hung_redis, real_sync_service):
    _shorten(real_sync_service._pool)

    started = time.monotonic()
    assert real_sync_service.get_cart(7) is None  # "no cart", not an exception
    assert real_sync_service.is_blacklisted("tok") is True  # fails closed
    assert real_sync_service.mark_once("once", 60) is True  # fails open
    assert real_sync_service.add_to_cart_atomic(7, 1, 1, None, "p", None, 5) is None  # nothing written
    assert time.monotonic() - started < 4 * (SHORT + MARGIN)


def test_the_async_service_degrades_as_documented_when_redis_hangs(hung_redis):
    async def scenario():
        client = _create_client()
        _shorten(client.connection_pool)
        service = AsyncRedisService(client)
        try:
            return (
                await service.get_cart(7),
                await service.mark_once("once", 60),
                await service.add_to_cart_atomic(7, 1, 1, None, "p", None, 5),
            )
        finally:
            await client.aclose()

    started = time.monotonic()
    assert asyncio.run(scenario()) == (None, True, None)
    assert time.monotonic() - started < 4 * (SHORT + MARGIN)


def test_the_token_checks_cannot_tell_when_redis_hangs(hung_redis):
    async def scenario():
        client = _create_client()
        _shorten(client.connection_pool)
        redis_async._clients[asyncio.get_running_loop()] = client
        try:
            with pytest.raises(TokenStateUnavailable):
                await is_token_blacklisted("tok")
            with pytest.raises(TokenStateUnavailable):
                await redis_async.is_token_superseded(7, 1.0)
        finally:
            await redis_async.close_async_redis()

    asyncio.run(scenario())


@pytest.fixture
def signed_in(client, valid_registration_payload):
    """A registered user with a valid access token, made while Redis is the
    suite's fake; returns the token."""
    client.post(f"{API}/users/register", json=valid_registration_payload)
    tokens = client.post(
        f"{API}/users/login",
        data={"username": valid_registration_payload["email"], "password": valid_registration_payload["password"]},
    ).json()
    return tokens["access_token"]


def _redis_now_hangs(monkeypatch):
    """From the next request on, the app's async client talks to the silent
    server (settings already point at it). The client of the loop the app
    runs on was built earlier; a new one replaces it, as after a restart."""
    original = _create_client

    def shortened():
        client = original()
        _shorten(client.connection_pool)
        return client

    monkeypatch.setattr(redis_async, "_create_client", shortened)
    monkeypatch.setattr(redis_async, "_clients", {})


def _assert_unavailable(response):
    """The answer for a token that cannot be checked: a problem+json 503 with
    the same Retry-After as the login's, and no 401 challenge."""
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "WWW-Authenticate" not in response.headers
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["status"] == 503
    assert body["retry_after"] == 30
    assert body["detail"] == "Token verification is temporarily unavailable, please retry"


def test_an_authenticated_request_is_refused_not_failed_when_redis_hangs(
    client, signed_in, hung_redis, monkeypatch, caplog
):
    headers = {"Authorization": f"Bearer {signed_in}"}
    assert client.get(f"{API}/users/me", headers=headers).status_code == 200
    _redis_now_hangs(monkeypatch)

    started = time.monotonic()
    response = client.get(f"{API}/users/me", headers=headers)

    # Whether the token was revoked cannot be known, so it is not served; but
    # its credentials are not at fault either, so it is not the 401 of a
    # revoked one: 503 with Retry-After (RFC 9110 15.6.4), not a 500, not a 200.
    _assert_unavailable(response)
    assert "is_token_blacklisted: Timeout reading from" in caplog.text
    assert time.monotonic() - started < 2 * (SHORT + MARGIN)
    assert hung_redis.accepted >= 1


def test_login_answers_503_with_retry_after_when_failed_attempts_cannot_be_counted(
    client, valid_registration_payload, hung_redis, monkeypatch
):
    client.post(f"{API}/users/register", json=valid_registration_payload)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    _redis_now_hangs(monkeypatch)

    started = time.monotonic()
    response = client.post(
        f"{API}/users/login",
        data={"username": valid_registration_payload["email"], "password": valid_registration_payload["password"]},
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"].isdigit() and int(response.headers["Retry-After"]) > 0
    assert response.json()["detail"] == "Login is temporarily unavailable"
    assert time.monotonic() - started < 4 * (SHORT + MARGIN)


def test_a_cart_change_answers_503_with_retry_after_when_redis_hangs(
    client, signed_in, hung_redis, monkeypatch, db_session
):
    category = Category(name="Hang", parent_id=None, level=0, path=[])
    db_session.add(category)
    db_session.commit()
    product = Product(
        name="Widget",
        description="A fine widget for all your widget needs",
        price="5.00",
        stock_quantity=5,
        images=[],
        characteristics={},
        category_id=category.id,
    )
    db_session.add(product)
    db_session.commit()
    headers = {"Authorization": f"Bearer {signed_in}"}
    _redis_now_hangs(monkeypatch)

    # The token check comes first and cannot be made, so a signed-in cart
    # request is the token check's 503; the cart's own 503 is the guest's.
    _assert_unavailable(
        client.post(f"{API}/cart/items", json={"product_id": product.id, "quantity": 1}, headers=headers)
    )
    response = client.post(f"{API}/cart/items", json={"product_id": product.id, "quantity": 1})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"


# --- A token that cannot be checked is 503 whatever the outage looks like -------------------


def _refuse_connections(monkeypatch):
    """From the next request on, the app's async client is refused
    (redis.exceptions.ConnectionError), as with Redis stopped."""
    server = fakeredis.FakeServer()
    server.connected = False
    monkeypatch.setattr(
        redis_async,
        "_create_client",
        lambda: fakeredis.FakeAsyncRedis(server=server, decode_responses=True),
    )
    monkeypatch.setattr(redis_async, "_clients", {})


@pytest.fixture(params=["hung", "refused"])
def redis_outage(request, monkeypatch):
    """A function that makes Redis unusable from the next request on: a
    server that accepts and says nothing, or one that refuses the connection."""
    if request.param == "hung":
        request.getfixturevalue("hung_redis")
        return lambda: _redis_now_hangs(monkeypatch)
    return lambda: _refuse_connections(monkeypatch)


@pytest.fixture
def signed_in_tokens(client, valid_registration_payload):
    client.post(f"{API}/users/register", json=valid_registration_payload)
    return client.post(
        f"{API}/users/login",
        data={"username": valid_registration_payload["email"], "password": valid_registration_payload["password"]},
    ).json()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_an_authenticated_request_is_503_when_redis_is_down(client, signed_in_tokens, redis_outage):
    redis_outage()

    _assert_unavailable(client.get(f"{API}/users/me", headers=_bearer(signed_in_tokens["access_token"])))


def test_a_refresh_is_503_when_redis_is_down(client, signed_in_tokens, redis_outage):
    redis_outage()

    response = client.post(f"{API}/users/refresh", json={"refresh_token": signed_in_tokens["refresh_token"]})

    _assert_unavailable(response)


def test_a_logout_is_503_when_redis_is_down(client, signed_in_tokens, redis_outage):
    redis_outage()

    response = client.post(
        f"{API}/users/logout",
        headers=_bearer(signed_in_tokens["access_token"]),
        json={"refresh_token": signed_in_tokens["refresh_token"]},
    )

    _assert_unavailable(response)


def test_a_cart_request_with_a_token_is_503_not_a_guest_cart_when_redis_is_down(
    client, signed_in_tokens, redis_outage
):
    redis_outage()

    _assert_unavailable(client.get(f"{API}/cart", headers=_bearer(signed_in_tokens["access_token"])))


def test_an_admin_route_is_503_when_redis_is_down(client, db_session, redis_outage):
    db_session.add(
        User(
            email="admin@example.com",
            hashed_password=get_password_hash("Str0ngPass"),
            full_name="Admin User",
            role=UserRole.ADMIN,
            gdpr_consent=True,
            privacy_policy_accepted=True,
            marketing_consent=False,
            consent_history=[],
            is_active=True,
        )
    )
    db_session.commit()
    token = client.post(
        f"{API}/users/login", data={"username": "admin@example.com", "password": "Str0ngPass"}
    ).json()["access_token"]
    assert client.get(f"{API}/admin/stats", headers=_bearer(token)).status_code == 200
    redis_outage()

    _assert_unavailable(client.get(f"{API}/admin/stats", headers=_bearer(token)))


def test_a_refresh_token_is_not_spent_by_an_outage(client, signed_in_tokens, monkeypatch):
    """The 503 comes before the token is redeemed, so the client keeps it and
    retries once Redis is back instead of being logged out."""
    create_client = redis_async._create_client
    _refuse_connections(monkeypatch)
    refresh = {"refresh_token": signed_in_tokens["refresh_token"]}
    assert client.post(f"{API}/users/refresh", json=refresh).status_code == 503

    monkeypatch.setattr(redis_async, "_create_client", create_client)
    monkeypatch.setattr(redis_async, "_clients", {})

    assert client.post(f"{API}/users/refresh", json=refresh).status_code == 200


def test_a_login_is_503_and_hands_out_no_token_when_the_refresh_token_cannot_be_registered(
    client, valid_registration_payload, redis_outage
):
    """A refresh token that Redis does not know fails at its first use with a
    401, which logs the user out: better no token and a 503 to retry."""
    client.post(f"{API}/users/register", json=valid_registration_payload)
    redis_outage()

    response = client.post(
        f"{API}/users/login",
        data={"username": valid_registration_payload["email"], "password": valid_registration_payload["password"]},
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "WWW-Authenticate" not in response.headers
    assert response.json()["detail"] == "Login is temporarily unavailable"
    assert "refresh_token" not in response.text
    assert "access_token" not in response.text


def test_a_refresh_that_cannot_register_the_new_token_is_503_and_spends_nothing(
    client, signed_in_tokens, fake_redis, monkeypatch
):
    """The new token is recorded before the old one is redeemed, so a Redis
    that fails between the two leaves the client with the old token, still
    good, and a 503 to retry; not a spent token and nothing to replace it."""
    old = {"refresh_token": signed_in_tokens["refresh_token"]}
    with monkeypatch.context() as patched:
        async def failing(service, jti, user_id, family_id, ttl_seconds):
            return False

        patched.setattr(AsyncRedisService, "register_refresh_token", failing)
        response = client.post(f"{API}/users/refresh", json=old)

    _assert_unavailable(response)
    assert "refresh_token" not in response.text

    retry = client.post(f"{API}/users/refresh", json=old)
    assert retry.status_code == 200
    assert client.post(f"{API}/users/refresh", json={"refresh_token": retry.json()["refresh_token"]}).status_code == 200


def test_a_revoked_token_is_still_401_and_a_good_one_200_when_redis_is_up(client, signed_in_tokens):
    headers = _bearer(signed_in_tokens["access_token"])
    assert client.get(f"{API}/users/me", headers=headers).status_code == 200

    assert client.post(f"{API}/users/logout", headers=headers, json={}).status_code == 200

    response = client.get(f"{API}/users/me", headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'
