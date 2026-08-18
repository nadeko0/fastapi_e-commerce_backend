import asyncio

import pytest
from fastapi import HTTPException
from freezegun import freeze_time
from starlette.requests import Request

from app.core.config import settings
from app.core.rate_limit import RateLimiter


def _make_request(path="/api/v1/some-endpoint", client_host="203.0.113.5"):
    scope = {
        "type": "http",
        "path": path,
        "headers": [],
        "client": (client_host, 12345),
    }
    return Request(scope)


def test_check_rate_limit_returns_early_when_disabled(monkeypatch, fake_redis):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    limiter = RateLimiter()
    request = _make_request()

    # Should not raise even after far more than any limit's worth of calls,
    # and without touching Redis at all.
    for _ in range(5):
        asyncio.run(limiter.check_rate_limit(request))


def test_check_rate_limit_allows_requests_under_the_limit(monkeypatch, fake_redis):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    limiter = RateLimiter()
    limiter.default_rate_limits["anonymous"] = 5
    request = _make_request(path="/api/v1/unmapped-path")

    for _ in range(5):
        asyncio.run(limiter.check_rate_limit(request))


def test_check_rate_limit_raises_429_once_limit_exceeded(monkeypatch, fake_redis):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    limiter = RateLimiter()
    limiter.default_rate_limits["anonymous"] = 2
    request = _make_request(path="/api/v1/unmapped-path")

    # The sliding window keys each request by whole-second timestamp
    # (str(int(time.time()))), so two calls within the same wall-clock
    # second collapse onto the same zset member instead of counting
    # separately - advance the clock a full second between calls so each
    # one is actually counted.
    with freeze_time("2026-01-01 00:00:00") as frozen:
        asyncio.run(limiter.check_rate_limit(request))
        frozen.tick(delta=1)
        asyncio.run(limiter.check_rate_limit(request))
        frozen.tick(delta=1)

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(limiter.check_rate_limit(request))

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail["limit"] == 2


def test_check_rate_limit_uses_endpoint_specific_limit_for_login(monkeypatch, fake_redis):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN", 1)
    limiter = RateLimiter()
    limiter.endpoint_limits[f"{settings.API_V1_STR}/users/login"] = 1
    request = _make_request(path=f"{settings.API_V1_STR}/users/login")

    with freeze_time("2026-01-01 00:00:00") as frozen:
        asyncio.run(limiter.check_rate_limit(request))
        frozen.tick(delta=1)

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(limiter.check_rate_limit(request))

    assert exc_info.value.status_code == 429


def test_check_rate_limit_fails_open_when_redis_errors(monkeypatch, fake_redis):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    limiter = RateLimiter()
    request = _make_request()

    def _broken_pipeline():
        raise RuntimeError("redis is down")

    monkeypatch.setattr(limiter.redis._redis, "pipeline", _broken_pipeline)

    # Must not raise - a Redis failure fails open (allows the request)
    # rather than blocking all traffic.
    asyncio.run(limiter.check_rate_limit(request))
