"""app/core/rate_limit.py, piece by piece: the "N/period" settings, the
client address behind proxies, the caller's user id, and the counter.
What a client sees over HTTP is in tests/integration/test_rate_limit_api.py.
"""

import asyncio
import time

import jwt
import pytest
from fastapi import HTTPException
from freezegun import freeze_time
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.requests import Request

import app.core.rate_limit as rate_limit
from app.core.config import settings
from app.core.rate_limit import (
    Rate,
    client_address,
    parse_rate,
    seconds_until_allowed,
    user_id_from_token,
)
from app.core.security import create_access_token, issue_refresh_token

RATE_SETTINGS = (
    "RATE_LIMIT_DEFAULT",
    "RATE_LIMIT_SEARCH",
    "RATE_LIMIT_AUTH",
    "RATE_LIMIT_CHECKOUT",
    "RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS",
    "RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL",
)


def make_request(peer="203.0.113.7", forwarded=(), authorization=None, raw_headers=()):
    headers = [(b"x-forwarded-for", value.encode("latin-1")) for value in forwarded]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("latin-1")))
    headers.extend(raw_headers)
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers,
        "client": (peer, 50000) if peer is not None else None,
    })


# --- "N/period" -------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("300/minute", Rate(300, 60)),
        ("5/second", Rate(5, 1)),
        ("2/hours", Rate(2, 3600)),
        ("1/day", Rate(1, 86400)),
        ("10/15 minutes", Rate(10, 900)),
        ("10/15minutes", Rate(10, 900)),
    ],
)
def test_parse_rate(text, expected):
    assert parse_rate(text) == expected


@pytest.mark.parametrize(
    "text", ["", "10", "ten/minute", "0/minute", "10/fortnight", "10/0 minutes", "-1/minute", "10 / minute"]
)
def test_parse_rate_rejects_what_is_not_a_rate(text):
    with pytest.raises(ValueError):
        parse_rate(text)


@pytest.mark.parametrize("name", RATE_SETTINGS)
def test_every_default_limit_parses(name):
    assert parse_rate(type(settings).model_fields[name].default).count >= 1


@pytest.mark.parametrize("name", RATE_SETTINGS)
def test_a_malformed_limit_stops_the_application_at_startup(name):
    # No silent fallback to some default: a typo in a security setting
    # must be noticed when the process starts, not never.
    with pytest.raises(ValidationError):
        type(settings)(**{name: "lots"})


def test_the_old_single_login_failure_setting_is_refused_not_ignored():
    # It was split in two. A deployment still setting it would otherwise
    # run with defaults it never chose, and nobody would notice.
    with pytest.raises(ValidationError, match="RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS"):
        type(settings)(RATE_LIMIT_LOGIN_FAILURES="10/15 minutes")


# --- client address -----------------------------------------------------------


def test_forwarded_header_is_ignored_without_a_trusted_proxy(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", [])
    request = make_request(peer="203.0.113.7", forwarded=["198.51.100.1"])
    assert client_address(request) == "203.0.113.7"


def test_forwarded_header_is_ignored_when_the_peer_is_not_the_trusted_proxy(monkeypatch):
    # Somebody reaching the application around the proxy cannot pick an
    # identity by sending the header themselves.
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.5"])
    request = make_request(peer="203.0.113.7", forwarded=["198.51.100.1"])
    assert client_address(request) == "203.0.113.7"


@pytest.mark.parametrize("trusted", [["10.0.0.5"], ["10.0.0.0/8"], ["192.168.0.0/16", "10.0.0.5/8"]])
def test_client_is_taken_from_the_header_when_the_peer_is_a_trusted_proxy(monkeypatch, trusted):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", trusted)
    request = make_request(peer="10.0.0.5", forwarded=["198.51.100.1"])
    assert client_address(request) == "198.51.100.1"


def test_the_rightmost_entry_that_is_not_a_proxy_wins(monkeypatch):
    # "6.6.6.6" is what the client wrote into the header itself; the real
    # client is what our outermost proxy appended after it.
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    request = make_request(peer="10.0.0.5", forwarded=["6.6.6.6, 198.51.100.1, 10.0.0.9"])
    assert client_address(request) == "198.51.100.1"


def test_several_header_lines_are_read_as_one_list(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    request = make_request(peer="10.0.0.5", forwarded=["6.6.6.6", "198.51.100.1", "10.0.0.9"])
    assert client_address(request) == "198.51.100.1"


def test_a_request_from_inside_is_the_first_hop(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    request = make_request(peer="10.0.0.5", forwarded=["10.0.0.77, 10.0.0.9"])
    assert client_address(request) == "10.0.0.77"


def test_a_trusted_proxy_without_the_header_is_itself_the_client(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    assert client_address(make_request(peer="10.0.0.5")) == "10.0.0.5"


@pytest.mark.parametrize("garbage", ["not-an-address", "", "999.1.1.1", "1.2.3.4:80", "x" * 5000])
def test_a_malformed_entry_never_mints_an_identity(monkeypatch, garbage):
    # Whatever is unreadable ends the walk: the caller is then counted as
    # the proxy (shared with everybody else doing this), never as a fresh
    # address of its choosing.
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    request = make_request(peer="10.0.0.5", forwarded=[f"198.51.100.1, {garbage}"])
    assert client_address(request) == "10.0.0.5"


def test_ipv6_clients_are_counted_per_64(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", [])
    first = client_address(make_request(peer="2001:db8:1:2:aaaa::1"))
    second = client_address(make_request(peer="2001:db8:1:2:bbbb::2"))
    other = client_address(make_request(peer="2001:db8:1:3::1"))
    assert first == second == "2001:db8:1:2::/64"
    assert other != first


def test_ipv4_mapped_ipv6_is_the_ipv4_address(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8", "fd00::/8"])
    request = make_request(peer="::ffff:10.0.0.5", forwarded=["::ffff:198.51.100.1"])
    assert client_address(request) == "198.51.100.1"


def test_a_peer_that_is_not_an_address_is_used_as_a_name(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    assert client_address(make_request(peer="testclient", forwarded=["1.2.3.4"])) == "testclient"
    assert client_address(make_request(peer=None, forwarded=["1.2.3.4"])) == "unknown"


def test_an_unreadable_trusted_proxy_entry_is_ignored_and_reported(monkeypatch, caplog):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["nonsense-entry", "10.0.0.5"])
    with caplog.at_level("WARNING", logger="app.core.rate_limit"):
        request = make_request(peer="10.0.0.5", forwarded=["198.51.100.1"])
        assert client_address(request) == "198.51.100.1"
    assert "nonsense-entry" in caplog.text


@hypothesis_settings(max_examples=200, deadline=None)
@given(header=st.binary(max_size=200), peer=st.sampled_from(["10.0.0.5", "203.0.113.7", "::1", "x"]))
def test_client_address_never_raises(header, peer):
    original = settings.TRUSTED_PROXIES
    settings.TRUSTED_PROXIES = ["10.0.0.0/8", "::1"]
    try:
        request = make_request(peer=peer, raw_headers=[(b"x-forwarded-for", header)])
        assert isinstance(client_address(request), str)
    finally:
        settings.TRUSTED_PROXIES = original


# --- user id from the access token -----------------------------------------------


def test_user_id_comes_from_a_valid_access_token():
    request = make_request(authorization=f"Bearer {create_access_token(42)}")
    assert user_id_from_token(request) == "42"
    request = make_request(authorization=f"bearer {create_access_token(42)}")
    assert user_id_from_token(request) == "42"


def _token(**claims):
    payload = {"sub": "42", "type": "access", "exp": int(time.time()) + 600, **claims}
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer",
        "Bearer not.a.token",
        "Basic dXNlcjpwYXNz",
        "Bearer \xff\xfe",
    ],
)
def test_no_user_id_without_a_usable_bearer_token(authorization):
    assert user_id_from_token(make_request(authorization=authorization)) is None


@hypothesis_settings(max_examples=300, deadline=None)
@given(header=st.binary(max_size=300))
def test_reading_the_token_never_raises(header):
    # The header is the caller's to fill; nothing in it may turn into a 500.
    for value in (header, b"Bearer " + header):
        request = make_request(raw_headers=[(b"authorization", value)])
        assert user_id_from_token(request) is None


def test_no_user_id_from_tokens_that_are_not_valid_access_tokens():
    expired = _token(exp=int(time.time()) - 5)
    foreign = jwt.encode(
        {"sub": "42", "type": "access", "exp": int(time.time()) + 600},
        "somebody-else's-key-of-32-bytes!",
        algorithm=settings.JWT_ALGORITHM,
    )
    no_subject = jwt.encode(
        {"type": "access", "exp": int(time.time()) + 600},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    for token in (expired, foreign, no_subject, _token(type="reset"), issue_refresh_token(42)):
        assert user_id_from_token(make_request(authorization=f"Bearer {token}")) is None


@pytest.mark.parametrize("subject", ["abc", "", "01", " 42", "4.2", "٤٢", "9" * 5000])
def test_no_user_id_from_a_signed_token_whose_subject_is_not_a_user_id(subject):
    # Such a token authenticates nobody (get_current_user answers 401), so
    # it must not open a per-user counter either: the address pays.
    request = make_request(authorization=f"Bearer {_token(sub=subject)}")
    assert user_id_from_token(request) is None


# --- the counter -----------------------------------------------------------------


def hit(key, rate, times=1):
    """`times` requests against `key`, one after another: their answers."""

    async def run():
        return [await seconds_until_allowed(key, rate) for _ in range(times)]

    return asyncio.run(run())


def test_the_limit_admits_exactly_n_and_then_tells_how_long_to_wait():
    with freeze_time("2026-01-01 00:00:00"):
        assert hit("k", Rate(3, 60), times=5) == [0, 0, 0, 60, 60]


def test_the_wait_shrinks_with_the_clock_and_the_window_ends():
    with freeze_time("2026-01-01 00:00:00") as clock:
        assert hit("k", Rate(2, 60), times=2) == [0, 0]
        clock.tick(20.5)
        assert hit("k", Rate(2, 60)) == [40]  # 39.5 s left, rounded up
        clock.tick(39)
        assert hit("k", Rate(2, 60)) == [1]
        clock.tick(0.6)
        # A new window: the full budget again.
        assert hit("k", Rate(2, 60), times=3) == [0, 0, 60]


def test_rejected_requests_do_not_extend_the_wait():
    # Hammering while blocked changes nothing: the Retry-After that was
    # sent stays true however often the client retries.
    with freeze_time("2026-01-01 00:00:00") as clock:
        hit("k", Rate(1, 60))
        for elapsed in range(1, 60):
            clock.tick(1)
            assert hit("k", Rate(1, 60), times=3) == [60 - elapsed] * 3
        clock.tick(1.1)
        assert hit("k", Rate(1, 60)) == [0]


def test_keys_are_counted_separately():
    with freeze_time("2026-01-01 00:00:00"):
        assert hit("a", Rate(1, 60), times=2) == [0, 60]
        assert hit("b", Rate(1, 60), times=2) == [0, 60]


def test_exactly_n_are_admitted_when_requests_arrive_together():
    async def volley():
        return await asyncio.gather(
            *[seconds_until_allowed("hot-key", Rate(20, 60)) for _ in range(200)]
        )

    answers = asyncio.run(volley())
    assert answers.count(0) == 20
    assert all(answer >= 1 for answer in answers if answer != 0)


def test_one_small_key_per_counter_that_expires_with_its_window(fake_redis):
    with freeze_time("2026-01-01 00:00:00"):
        hit("k", Rate(5, 60), times=50)
        assert fake_redis.keys("*") == ["k"]
        assert fake_redis.get("k") == "50"
        assert 0 < fake_redis.pttl("k") <= 60_000


def test_the_script_is_reloaded_when_redis_has_forgotten_it(fake_redis):
    # A Redis restart or SCRIPT FLUSH empties the script cache; the next
    # call must repair that by itself and not fail open.
    assert hit("k", Rate(2, 60)) == [0]
    fake_redis.script_flush()
    assert hit("k", Rate(2, 60), times=2) == [0, 60]


def test_a_counter_that_lost_its_expiry_gets_a_new_window(fake_redis):
    # Cannot happen through this code (the script sets both atomically);
    # if it happens anyway, the key must not block its caller for ever.
    fake_redis.set("k", 100)
    with freeze_time("2026-01-01 00:00:00"):
        assert hit("k", Rate(2, 60)) == [60]
        assert 0 < fake_redis.pttl("k") <= 60_000


def test_fails_open_when_redis_cannot_be_reached(monkeypatch, caplog):
    def unreachable():
        raise RedisConnectionError("Redis is down")

    monkeypatch.setattr(rate_limit, "get_async_redis", unreachable)
    with caplog.at_level("ERROR", logger="app.core.rate_limit"):
        assert hit("k", Rate(1, 60), times=3) == [0, 0, 0]
    assert "request allowed" in caplog.text


def test_fails_open_when_the_redis_call_itself_fails(monkeypatch):
    class BrokenScript:
        async def __call__(self, **kwargs):
            raise RedisConnectionError("connection reset")

    monkeypatch.setattr(rate_limit, "_count_script", BrokenScript())
    assert hit("k", Rate(1, 60), times=3) == [0, 0, 0]


def _attempts(email, times):
    """`times` login attempts for `email`: the status of each refusal, or
    None for an attempt let through."""

    async def run():
        answers = []
        for _ in range(times):
            try:
                await rate_limit.check_login_attempt(email, make_request())
                answers.append(None)
            except HTTPException as refusal:
                answers.append(refusal.status_code)
        return answers

    return asyncio.run(run())


def test_the_login_attempt_count_fails_closed_when_redis_cannot_be_reached(monkeypatch, caplog):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)

    def unreachable():
        raise RedisConnectionError("Redis is down")

    monkeypatch.setattr(rate_limit, "get_async_redis", unreachable)
    with caplog.at_level("ERROR", logger="app.core.rate_limit"):
        assert _attempts("victim@example.com", 50) == [503] * 50
    assert "login refused" in caplog.text


def test_the_login_attempt_count_fails_closed_when_the_redis_call_itself_fails(monkeypatch):
    class BrokenScript:
        async def __call__(self, **kwargs):
            raise RedisConnectionError("connection reset")

    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(rate_limit, "_count_script", BrokenScript())
    assert _attempts("victim@example.com", 3) == [503] * 3


# --- the per-request check, outside a route ----------------------------------------


def test_a_request_that_matched_no_route_is_not_counted(monkeypatch, fake_redis):
    # The check runs as a route dependency, so this does not happen in the
    # application; the guard is for anything else that might call it.
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    asyncio.run(rate_limit.limiter.check_rate_limit(make_request()))
    assert fake_redis.keys("*") == []
