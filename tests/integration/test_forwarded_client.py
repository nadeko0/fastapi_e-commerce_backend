"""X-Forwarded-For is trusted per TRUSTED_PROXIES, however the server was
started (F-28).

`python main.py` used to configure uvicorn's own proxy-header handling from
TRUSTED_PROXIES; `uvicorn app.main:app` instead uses uvicorn's defaults,
which believe X-Forwarded-For from 127.0.0.1 and ::1. uvicorn rewrites the
client address before the application sees the request, so with the second
command a client on the same host (or anything relaying through it) picked
its own rate-limit identity per request, TRUSTED_PROXIES empty or not.
"""

import os
import runpy
from unittest import mock

import pytest
import uvicorn
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core.config import settings
from app.main import app

API = settings.API_V1_STR
MAIN_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "main.py")
UNLIMITED = "1000000/minute"


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    for name in ("DEFAULT", "SEARCH", "AUTH", "CHECKOUT", "LOGIN_FAILURES", "GUEST_CART"):
        monkeypatch.setattr(settings, f"RATE_LIMIT_{name}", UNLIMITED)
    monkeypatch.setattr(settings, "RATE_LIMIT_DEFAULT", "2/minute")


def _as_started_by_plain_uvicorn():
    # What `uvicorn app.main:app` does with no proxy options: Config's
    # defaults are proxy_headers=True and FORWARDED_ALLOW_IPS or
    # "127.0.0.1,::1" (uvicorn/config.py), applied around the app.
    return ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1,::1")


def test_a_local_client_cannot_pick_its_identity_under_plain_uvicorn(client, limits, monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", [])
    local = TestClient(_as_started_by_plain_uvicorn(), client=("127.0.0.1", 50000))

    codes = [
        local.get(f"{API}/categories", headers={"X-Forwarded-For": f"198.51.100.{i}"}).status_code
        for i in range(1, 5)
    ]

    assert codes == [200, 200, 429, 429]


def test_trusted_proxies_still_tell_clients_apart_under_plain_uvicorn(client, limits, monkeypatch):
    # The proxy is not uvicorn's 127.0.0.1, so uvicorn leaves the request
    # alone and the application applies TRUSTED_PROXIES itself.
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    proxy = TestClient(_as_started_by_plain_uvicorn(), client=("10.0.0.5", 50000))

    def get(forwarded_for):
        return proxy.get(f"{API}/categories", headers={"X-Forwarded-For": forwarded_for}).status_code

    assert [get("198.51.100.1"), get("198.51.100.1"), get("198.51.100.1")] == [200, 200, 429]
    assert get("198.51.100.2") == 200


def test_main_py_leaves_forwarded_headers_to_the_application(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    with mock.patch.object(uvicorn, "run") as run:
        runpy.run_path(MAIN_PY, run_name="__main__")
    kwargs = run.call_args.kwargs
    assert kwargs["proxy_headers"] is False
    assert "forwarded_allow_ips" not in kwargs
