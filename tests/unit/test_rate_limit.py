from starlette.requests import Request

from app.core.config import settings
from app.core.rate_limit import RateLimiter


def _make_request(client_host: str, forwarded_for: str | None = None) -> Request:
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    scope = {
        "type": "http",
        "headers": headers,
        "client": (client_host, 12345),
    }
    return Request(scope)


def test_endpoint_limits_match_real_login_and_register_routes():
    # Regression test: endpoint_limits used to key off "/auth/login" and
    # "/auth/register", but the actual routes (users.py, prefix="/users")
    # are "/users/login" and "/users/register" - the strict limits silently
    # never applied and only the generic anonymous limit was enforced.
    limiter = RateLimiter()
    login_limit = limiter.endpoint_limits[f"{settings.API_V1_STR}/users/login"]
    register_limit = limiter.endpoint_limits[f"{settings.API_V1_STR}/users/register"]

    assert login_limit == settings.RATE_LIMIT_LOGIN
    assert register_limit == settings.RATE_LIMIT_REGISTER


def test_get_rate_limit_applies_strict_login_limit_for_login_path():
    limiter = RateLimiter()

    limit = limiter._get_rate_limit(f"{settings.API_V1_STR}/users/login", "anonymous")

    assert limit == settings.RATE_LIMIT_LOGIN


def test_client_identifier_ignores_forwarded_for_from_untrusted_peer(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", [])
    limiter = RateLimiter()
    request = _make_request("203.0.113.5", forwarded_for="9.9.9.9")

    client_ip, _ = limiter._get_client_identifier(request)

    assert client_ip == "203.0.113.5"


def test_client_identifier_honors_forwarded_for_from_trusted_proxy(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["203.0.113.5"])
    limiter = RateLimiter()
    request = _make_request("203.0.113.5", forwarded_for="9.9.9.9, 10.0.0.1")

    client_ip, _ = limiter._get_client_identifier(request)

    assert client_ip == "9.9.9.9"
