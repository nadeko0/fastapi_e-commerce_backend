"""The rate limiter as a client meets it: which requests share a counter,
who is exempt, what a 429 looks like, and the failed-login count. The pieces
are tested one by one in tests/unit/test_rate_limit.py.

The suite runs with RATE_LIMIT_ENABLED=false (tests/conftest.py); every test
here that wants the limiter switches it on.
"""

import pytest
from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from freezegun import freeze_time
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import app.core.rate_limit as rate_limit
import app.main as main_module
from app.core.config import settings
from app.core.probes import PROBE_PATHS
from app.core.rate_limit import _EXEMPT, _ROUTE_LIMITS, parse_rate
from app.core.security import generate_password_reset_token
from app.main import app
from app.models.user import User

API = settings.API_V1_STR
UNLIMITED = "1000000/minute"


@pytest.fixture
def limits(monkeypatch):
    """Switch the limiter on with every limit out of the way; a test then
    lowers the one it is about: limits(RATE_LIMIT_SEARCH="2/minute")."""
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    for name in (
        "DEFAULT", "SEARCH", "AUTH", "CHECKOUT",
        "LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS", "LOGIN_FAILURES_PER_EMAIL",
    ):
        monkeypatch.setattr(settings, f"RATE_LIMIT_{name}", UNLIMITED)

    def lower(**values):
        for name, value in values.items():
            monkeypatch.setattr(settings, name, value)

    return lower


def register(client, payload, email=None):
    payload = {**payload, "email": email or payload["email"]}
    assert client.post(f"{API}/users/register", json=payload).status_code == 202
    return payload


def login(client, email, password):
    return client.post(f"{API}/users/login", data={"username": email, "password": password})


def _reset_token_for(email):
    db = TestingSessionLocal()
    try:
        user = db.query(User).filter(User.email == email).one()
        return generate_password_reset_token(user.email, user.hashed_password)
    finally:
        db.close()


def auth_header(client, payload, email=None):
    payload = register(client, payload, email)
    token = login(client, payload["email"], payload["password"]).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# --- the table ------------------------------------------------------------------


def test_every_route_named_in_the_limiter_exists():
    # A renamed route would silently drop to the default limit (or lose
    # its exemption) if its entry here were left behind.
    paths = app.openapi()["paths"]
    for method, template in list(_ROUTE_LIMITS) + list(_EXEMPT):
        if method == "HEAD":
            continue  # the probes serve HEAD but keep it out of the schema
        full_path = template if template in PROBE_PATHS else f"{API}{template}"
        assert method.lower() in paths.get(full_path, {}), (method, template)
    for setting in set(_ROUTE_LIMITS.values()):
        assert parse_rate(getattr(settings, setting))


# --- what shares a counter --------------------------------------------------------


def test_limit_answers_429_with_retry_after_and_recovers(client, limits):
    limits(RATE_LIMIT_DEFAULT="2/minute")
    with freeze_time("2026-01-01 00:00:00") as clock:
        assert client.get(f"{API}/categories").status_code == 200
        assert client.get(f"{API}/categories").status_code == 200
        clock.tick(10)

        blocked = client.get(f"{API}/categories")
        assert blocked.status_code == 429
        assert blocked.headers["Retry-After"] == "50"
        assert blocked.json()["detail"] == "Rate limit exceeded"
        assert blocked.json()["retry_after"] == 50

        clock.tick(51)
        assert client.get(f"{API}/categories").status_code == 200


def test_urls_of_one_route_share_a_counter(client, limits):
    # Keyed by the route template: walking product ids buys nothing.
    limits(RATE_LIMIT_DEFAULT="3/minute")
    for product_id in (1, 2, 3):
        assert client.get(f"{API}/products/{product_id}").status_code == 404
    assert client.get(f"{API}/products/4").status_code == 429
    # Another operation has its own counter.
    assert client.get(f"{API}/categories").status_code == 200


def test_search_has_its_own_limit(client, limits):
    limits(RATE_LIMIT_SEARCH="1/minute")
    assert client.get(f"{API}/products/search", params={"query": "shoe"}).status_code != 429
    assert client.get(f"{API}/products/search", params={"query": "boot"}).status_code == 429
    assert client.get(f"{API}/products").status_code == 200


def test_logged_in_callers_are_counted_per_user_and_others_per_address(
    client, limits, valid_registration_payload
):
    alice = auth_header(client, valid_registration_payload)
    bob = auth_header(client, valid_registration_payload, email="bob@example.com")
    limits(RATE_LIMIT_DEFAULT="2/minute")

    assert client.get(f"{API}/users/me", headers=alice).status_code == 200
    assert client.get(f"{API}/users/me", headers=alice).status_code == 200
    assert client.get(f"{API}/users/me", headers=alice).status_code == 429
    # Same address, another user: another counter.
    assert client.get(f"{API}/users/me", headers=bob).status_code == 200
    # No token (or a bad one): the address pays, not some user.
    assert client.get(f"{API}/users/me").status_code == 401
    assert client.get(f"{API}/users/me", headers={"Authorization": "Bearer junk"}).status_code == 401
    assert client.get(f"{API}/users/me").status_code == 429
    # A user is the same user from another address.
    elsewhere = TestClient(app, client=("198.51.100.9", 50000))
    assert elsewhere.get(f"{API}/users/me", headers=alice).status_code == 429
    assert elsewhere.get(f"{API}/users/me").status_code == 401


def test_checkout_is_limited_per_user(client, limits, valid_registration_payload):
    alice = auth_header(client, valid_registration_payload)
    bob = auth_header(client, valid_registration_payload, email="bob@example.com")
    limits(RATE_LIMIT_CHECKOUT="1/minute")

    # No Idempotency-Key, so the route itself answers 422: it was reached.
    assert client.post(f"{API}/orders", json={}, headers=alice).status_code == 400
    assert client.post(f"{API}/orders", json={}, headers=alice).status_code == 429
    assert client.post(f"{API}/orders", json={}, headers=bob).status_code == 400


def test_auth_routes_are_limited_per_address_whoever_is_named(client, limits):
    limits(RATE_LIMIT_AUTH="2/minute")
    assert login(client, "a@example.com", "wrong-password").status_code == 401
    assert login(client, "b@example.com", "wrong-password").status_code == 401
    blocked = login(client, "c@example.com", "wrong-password")
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "Rate limit exceeded"
    # Another address is not affected.
    elsewhere = TestClient(app, client=("198.51.100.9", 50000))
    assert login(elsewhere, "c@example.com", "wrong-password").status_code == 401


def test_auth_routes_stay_per_address_for_logged_in_callers(
    client, limits, valid_registration_payload
):
    # The one auth route that takes a token. Were it counted per user,
    # freshly registered accounts would each bring their own budget.
    alice = auth_header(client, valid_registration_payload)
    bob = auth_header(client, valid_registration_payload, email="bob@example.com")
    limits(RATE_LIMIT_AUTH="1/minute")

    assert client.post(f"{API}/users/verify-email/resend", headers=alice).status_code == 200
    assert client.post(f"{API}/users/verify-email/resend", headers=bob).status_code == 429


def test_clients_behind_a_trusted_proxy_are_told_apart(limits, monkeypatch):
    limits(RATE_LIMIT_DEFAULT="1/minute")
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    proxy = TestClient(app, client=("10.0.0.5", 50000))

    def get(forwarded_for):
        return proxy.get(f"{API}/categories", headers={"X-Forwarded-For": forwarded_for})

    assert get("198.51.100.1").status_code == 200
    assert get("198.51.100.2").status_code == 200
    assert get("198.51.100.1").status_code == 429
    # A made-up leftmost entry does not buy a fresh counter.
    assert get("6.6.6.6, 198.51.100.1").status_code == 429


def test_forwarded_header_buys_nothing_without_a_trusted_proxy(limits):
    limits(RATE_LIMIT_DEFAULT="1/minute")
    direct = TestClient(app, client=("203.0.113.7", 50000))
    assert direct.get(f"{API}/categories", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert direct.get(f"{API}/categories", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 429


# --- exemptions, kill switch, Redis outage -----------------------------------------


def test_health_webhook_and_docs_are_never_limited(client, limits, monkeypatch, fake_redis, request):
    limits(RATE_LIMIT_DEFAULT="1/minute")
    # /health talks to app.main.engine directly: see tests/integration/test_health.py.
    health_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    request.addfinalizer(health_engine.dispose)
    monkeypatch.setattr(main_module, "engine", health_engine)
    for _ in range(3):
        assert client.get("/health").status_code == 200
        assert client.post(f"{API}/orders/webhooks/stripe", content=b"{}").status_code == 400
        assert client.get(f"{API}/openapi.json").status_code == 200
        assert client.get(f"{API}/docs").status_code == 200
    # Not only "not rejected": not counted at all, so no Redis round trip.
    assert fake_redis.keys("rl:*") == []


def test_kill_switch_turns_everything_off(client, limits, monkeypatch, fake_redis, valid_registration_payload):
    limits(
        RATE_LIMIT_DEFAULT="1/minute",
        RATE_LIMIT_AUTH="1/minute",
        RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS="1/minute",
        RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL="1/minute",
    )
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)

    for _ in range(3):
        assert client.get(f"{API}/categories").status_code == 200
        assert login(client, valid_registration_payload["email"], "wrong-password").status_code == 401
    assert fake_redis.keys("rl:*") == []


def test_requests_are_let_through_when_redis_is_down(
    client, limits, monkeypatch, valid_registration_payload
):
    payload = register(client, valid_registration_payload)
    limits(
        RATE_LIMIT_DEFAULT="1/minute",
        RATE_LIMIT_AUTH="1/minute",
        RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS="1/minute",
        RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL="1/minute",
    )

    def unreachable():
        raise RedisConnectionError("Redis is down")

    monkeypatch.setattr(rate_limit, "get_async_redis", unreachable)

    for _ in range(3):
        assert client.get(f"{API}/categories").status_code == 200
        # An auth route other than login: its per-address limit fails open too.
        reset = client.post(f"{API}/users/password/reset", json={"email": payload["email"]})
        assert reset.status_code == 200


def test_login_is_refused_while_its_failure_count_cannot_be_kept(
    client, limits, monkeypatch, valid_registration_payload
):
    # The one exception to failing open: with nothing counting, a password
    # guesser would get unlimited attempts for exactly as long as Redis is
    # down (or made to look down). Login answers 503 instead, before any
    # password is looked at, wrong one or right one.
    payload = register(client, valid_registration_payload)
    limits(RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS="3/15 minutes")

    def unreachable():
        raise RedisConnectionError("Redis is down")

    monkeypatch.setattr(rate_limit, "get_async_redis", unreachable)

    async def must_not_run(*args, **kwargs):
        raise AssertionError("bcrypt was run while the attempt could not be counted")

    monkeypatch.setattr("app.api.v1.users.verify_password_async", must_not_run)

    for password in ["wrong-password"] * 5 + [payload["password"]]:
        refused = login(client, payload["email"], password)
        assert refused.status_code == 503
        assert refused.headers["Retry-After"] == "30"
        assert refused.json()["detail"] == "Login is temporarily unavailable"
        assert refused.json()["retry_after"] == 30


# --- failed logins, per account ----------------------------------------------------


def test_failing_on_purpose_from_one_address_does_not_lock_the_owner_out(
    client, monkeypatch, valid_registration_payload
):
    # With the default settings: anybody who knows an email could fail 10
    # logins for it every 15 minutes, from one address and well inside
    # RATE_LIMIT_AUTH, and keep its owner out for as long as they liked.
    # The hard lock is per (email, address) now: it stops the guesser, not
    # the owner.
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    payload = register(client, valid_registration_payload)
    attacker = TestClient(app, client=("203.0.113.66", 50000))
    owner = TestClient(app, client=("198.51.100.9", 50000))

    with freeze_time("2026-01-01 00:00:00") as clock:
        statuses = []
        for _ in range(12):
            statuses.append(login(attacker, payload["email"], "wrong-password").status_code)
            clock.tick(6)  # 10 a minute: inside RATE_LIMIT_AUTH (20/minute)
        assert statuses == [401] * 10 + [429] * 2

        assert login(owner, payload["email"], payload["password"]).status_code == 200


def test_the_backstop_counts_failures_per_email_across_addresses(
    client, limits, valid_registration_payload
):
    # Guessing spread over many addresses: each address stays under its
    # own lock, the email's count across all of them does not.
    payload = register(client, valid_registration_payload)
    limits(RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL="3/15 minutes")
    elsewhere = TestClient(app, client=("198.51.100.9", 50000))

    with freeze_time("2026-01-01 00:00:00") as clock:
        assert login(client, payload["email"], "wrong-1").status_code == 401
        assert login(elsewhere, payload["email"], "wrong-2").status_code == 401
        assert login(client, payload["email"], "wrong-3").status_code == 401
        clock.tick(60)

        for caller in (client, elsewhere):
            blocked = login(caller, payload["email"], "wrong-4")
            assert blocked.status_code == 429
            assert blocked.headers["Retry-After"] == "840"
            assert blocked.json()["detail"] == "Too many failed login attempts"
            assert blocked.json()["retry_after"] == 840
        # The right password is refused as well until the window is over...
        assert login(client, payload["email"], payload["password"]).status_code == 429
        # ...and another account is not affected, from the same address.
        assert login(client, "someone.else@example.com", "wrong").status_code == 401

        clock.tick(14 * 60 + 1)
        assert login(client, payload["email"], payload["password"]).status_code == 200


def test_a_successful_login_clears_the_count(client, limits, valid_registration_payload):
    payload = register(client, valid_registration_payload)
    limits(RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS="3/15 minutes")

    for _ in range(3):  # more rounds than the limit: each success starts over
        assert login(client, payload["email"], "wrong").status_code == 401
        assert login(client, payload["email"], "wrong").status_code == 401
        assert login(client, payload["email"], payload["password"]).status_code == 200


def test_spelling_variants_of_an_email_share_the_count(client, limits, valid_registration_payload):
    payload = register(client, valid_registration_payload)
    limits(RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS="3/15 minutes")

    for spelling in (payload["email"].upper(), f"  {payload['email']} ", payload["email"].title()):
        assert login(client, spelling, "wrong").status_code == 401
    assert login(client, payload["email"], payload["password"]).status_code == 429


def test_refusal_looks_the_same_for_an_email_without_an_account(
    client, limits, valid_registration_payload
):
    payload = register(client, valid_registration_payload)
    limits(RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS="3/15 minutes")

    def transcript(email):
        answers = []
        for _ in range(5):
            response = login(client, email, "wrong-password")
            # request_id is per request; everything else must match.
            body = {k: v for k, v in response.json().items() if k != "request_id"}
            answers.append((response.status_code, body, response.headers.get("Retry-After")))
        return answers

    with freeze_time("2026-01-01 00:00:00"):
        known = transcript(payload["email"])
        unknown = transcript("nobody.here@example.com")
    assert known == unknown
    assert [status for status, _, _ in known] == [401, 401, 401, 429, 429]


def test_a_refused_attempt_is_not_judged_at_all(client, limits, monkeypatch, valid_registration_payload):
    payload = register(client, valid_registration_payload)
    limits(RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS="1/15 minutes")
    assert login(client, payload["email"], "wrong").status_code == 401

    async def must_not_run(*args, **kwargs):
        raise AssertionError("bcrypt was run for a refused attempt")

    monkeypatch.setattr("app.api.v1.users.verify_password_async", must_not_run)
    assert login(client, payload["email"], payload["password"]).status_code == 429


def test_a_completed_password_reset_clears_the_count(client, limits, valid_registration_payload):
    payload = register(client, valid_registration_payload)
    limits(RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS="2/15 minutes")
    assert login(client, payload["email"], "wrong").status_code == 401
    assert login(client, payload["email"], "wrong").status_code == 401
    assert login(client, payload["email"], payload["password"]).status_code == 429

    reset = client.post(
        f"{API}/users/password/reset/confirm",
        json={"token": _reset_token_for(payload['email']), "new_password": "An0therStrongPass"},
    )
    assert reset.status_code == 200
    assert login(client, payload["email"], "An0therStrongPass").status_code == 200


def test_attempts_refused_by_the_address_lock_do_not_count_against_the_email(
    client, limits, fake_redis, valid_registration_payload
):
    # Otherwise one address hammering on after its own lock would run the
    # email's count up and lock the owner out everywhere after all.
    payload = register(client, valid_registration_payload)
    limits(
        RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS="2/15 minutes",
        RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL="5/15 minutes",
    )
    attacker = TestClient(app, client=("203.0.113.66", 50000))
    owner = TestClient(app, client=("198.51.100.9", 50000))

    statuses = [login(attacker, payload["email"], "wrong").status_code for _ in range(20)]

    assert statuses == [401, 401] + [429] * 18
    (email_key,) = [key for key in fake_redis.keys("rl:login-failures:*") if key.count(":") == 2]
    assert fake_redis.get(email_key) == "2"
    assert login(owner, payload["email"], payload["password"]).status_code == 200


def test_a_successful_login_clears_only_its_own_address(
    client, limits, valid_registration_payload
):
    payload = register(client, valid_registration_payload)
    limits(RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS="2/15 minutes")
    attacker = TestClient(app, client=("203.0.113.66", 50000))
    owner = TestClient(app, client=("198.51.100.9", 50000))

    for _ in range(2):
        assert login(attacker, payload["email"], "wrong").status_code == 401
    assert login(owner, payload["email"], payload["password"]).status_code == 200

    # The owner's success says nothing about the other address's guesses.
    assert login(attacker, payload["email"], payload["password"]).status_code == 429


def test_a_completed_password_reset_clears_the_backstop_but_not_other_addresses(
    client, limits, valid_registration_payload
):
    payload = register(client, valid_registration_payload)
    limits(
        RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS="2/15 minutes",
        RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL="3/15 minutes",
    )
    attackers = [TestClient(app, client=(f"203.0.113.{n}", 50000)) for n in (1, 2)]
    for attacker in attackers:
        for _ in range(2):
            login(attacker, payload["email"], "wrong")
    # The backstop is used up: the owner is refused too.
    assert login(client, payload["email"], payload["password"]).status_code == 429

    reset = client.post(
        f"{API}/users/password/reset/confirm",
        json={"token": _reset_token_for(payload['email']), "new_password": "An0therStrongPass"},
    )
    assert reset.status_code == 200

    assert login(client, payload["email"], "An0therStrongPass").status_code == 200
    assert login(attackers[0], payload["email"], "An0therStrongPass").status_code == 429


# --- RateLimit-Policy and RateLimit (IETF httpapi ratelimit-headers-11) -----------


def test_ratelimit_fields_follow_the_counter_on_200_and_429(client, limits):
    limits(RATE_LIMIT_DEFAULT="2/minute")
    with freeze_time("2026-01-01 00:00:00") as clock:
        first = client.get(f"{API}/categories")
        assert first.headers["RateLimit-Policy"] == '"default";q=2;w=60'
        assert first.headers["RateLimit"] == '"default";r=1;t=60'
        clock.tick(10)
        second = client.get(f"{API}/categories")
        assert second.headers["RateLimit"] == '"default";r=0;t=50'
        blocked = client.get(f"{API}/categories")
        assert blocked.status_code == 429
        assert blocked.headers["Retry-After"] == "50"
        assert blocked.headers["RateLimit-Policy"] == '"default";q=2;w=60'
        assert blocked.headers["RateLimit"] == '"default";r=0;t=50'


def test_no_ratelimit_fields_on_exempt_routes(client, limits):
    response = client.get("/health")
    assert "RateLimit" not in response.headers
    assert "RateLimit-Policy" not in response.headers


def test_ratelimit_fields_are_exposed_to_browsers(client, limits):
    response = client.get(f"{API}/categories", headers={"Origin": "http://localhost:3000"})
    exposed = response.headers["access-control-expose-headers"]
    assert "RateLimit" in exposed and "RateLimit-Policy" in exposed and "Retry-After" in exposed
