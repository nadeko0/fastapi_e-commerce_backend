"""app/core/forwarded.py: the one place X-Forwarded-For becomes the
request's client address."""

import asyncio
import logging

import pytest

import app.core.forwarded as forwarded
from app.core.config import settings
from app.core.forwarded import ForwardedClientMiddleware


def _call(client, forwarded_for=None, proto=None, scope_type="http"):
    seen = {}

    async def downstream(scope, receive, send):
        seen.update(scope)

    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("latin-1")))
    if proto is not None:
        headers.append((b"x-forwarded-proto", proto.encode("latin-1")))
    scope = {"type": scope_type, "client": client, "headers": headers, "scheme": "http"}
    asyncio.run(ForwardedClientMiddleware(downstream)(scope, None, None))
    return seen


@pytest.fixture(autouse=True)
def _fresh_warning(monkeypatch):
    monkeypatch.setattr(forwarded, "_server_rewrite_reported", False)


def test_without_trusted_proxies_the_header_is_left_unread(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", [])
    seen = _call(("203.0.113.7", 40000), forwarded_for="198.51.100.1")
    assert seen["client"] == ("203.0.113.7", 40000)


def test_a_trusted_proxy_hands_over_the_client_and_scheme(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    seen = _call(("10.0.0.5", 40000), forwarded_for="6.6.6.6, 198.51.100.1", proto="https")
    assert seen["client"][0] == "198.51.100.1"
    assert seen["scheme"] == "https"


def test_an_untrusted_peer_is_taken_as_the_client(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.5"])
    seen = _call(("203.0.113.7", 40000), forwarded_for="198.51.100.1", proto="https")
    assert seen["client"] == ("203.0.113.7", 40000)
    assert seen["scheme"] == "http"


def test_a_network_written_with_host_bits_is_understood(monkeypatch):
    # client_address reads "10.0.0.5/8" as the network it lies in
    # (strict=False); so must this, or the two disagree about who is a proxy.
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.5/8"])
    seen = _call(("10.9.9.9", 40000), forwarded_for="198.51.100.1")
    assert seen["client"][0] == "198.51.100.1"


@pytest.mark.parametrize("trusted", [[], ["10.0.0.0/8"]])
@pytest.mark.parametrize(
    "client,forwarded_for",
    [
        (("198.51.100.1", 0), "198.51.100.1"),
        (("198.51.100.1", 0), "6.6.6.6, 198.51.100.1, 127.0.0.1"),
        (("198.51.100.1", 8080), "198.51.100.1:8080"),
        (("2001:db8::1", 0), "[2001:db8::1]:443"),
    ],
)
def test_a_client_the_server_already_took_from_the_header_is_dropped(
    monkeypatch, caplog, trusted, client, forwarded_for
):
    # The server (uvicorn's own proxy handling) has replaced the socket peer
    # with an entry of the header, by its own idea of which proxies to
    # believe. The real peer is gone, so the application cannot apply
    # TRUSTED_PROXIES: the client becomes unknown (one shared rate-limit
    # counter) rather than whatever the header said.
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", trusted)
    with caplog.at_level(logging.ERROR, logger="app.core.forwarded"):
        seen = _call(client, forwarded_for=forwarded_for)
        _call(client, forwarded_for=forwarded_for)
    assert seen["client"] is None
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1  # once per process, not once per request
    assert "--no-proxy-headers" in errors[0].getMessage()


def test_no_header_no_change(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    assert _call(("10.0.0.5", 40000))["client"] == ("10.0.0.5", 40000)
    assert _call(None, forwarded_for="198.51.100.1")["client"] is None


def test_lifespan_passes_through(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    seen = _call(("10.0.0.5", 40000), forwarded_for="198.51.100.1", scope_type="lifespan")
    assert seen["client"] == ("10.0.0.5", 40000)
