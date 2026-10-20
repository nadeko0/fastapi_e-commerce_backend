"""Who the client is, behind a reverse proxy: the one place
X-Forwarded-For turns into the request's client address.

Behind a proxy the socket peer is the proxy, and the client is only known
from X-Forwarded-For, which anyone can send. It is believed only when the
peer is one of settings.TRUSTED_PROXIES. Everything that asks for
request.client (the rate limiter's client_address, the consent records)
then sees the client.

This used to be uvicorn's job, configured by main.py from TRUSTED_PROXIES.
But uvicorn applies its proxy handling before the application, from its
own settings, and a server started another way (`uvicorn app.main:app`)
uses uvicorn's defaults: X-Forwarded-For believed from 127.0.0.1 and ::1,
whatever TRUSTED_PROXIES says. A client on the same host could then pick a
new identity, and a fresh rate-limit counter, with every request. So the
application does it, here, and main.py turns uvicorn's handling off.

A server that still does its own (started without --no-proxy-headers) is
detected: the client address it hands over is one of the header's entries.
The real peer is gone by then, so TRUSTED_PROXIES cannot be applied to it;
the client is made unknown instead (all such requests share one rate-limit
counter) and the misconfiguration is logged. A client that puts its own
address in the header is treated the same way, which costs it only its own
counter.
"""

import ipaddress
import logging
from functools import lru_cache

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core.config import settings

logger = logging.getLogger(__name__)

_server_rewrite_reported = False


def _forwarded_hosts(headers) -> set:
    """Every entry of every X-Forwarded-For header, as written and, when it
    carries a port ("1.2.3.4:80", "[2001:db8::1]:443"), without it: the
    forms uvicorn can hand over as the client's host."""
    hosts = set()
    for name, value in headers:
        if name != b"x-forwarded-for":
            continue
        for hop in value.decode("latin-1").split(","):
            hop = hop.strip()
            if not hop:
                continue
            hosts.add(hop)
            if hop.startswith("[") and "]" in hop:
                hosts.add(hop[1:hop.index("]")])
            elif hop.count(":") == 1:
                hosts.add(hop.rsplit(":", 1)[0])
    return hosts


def _as_networks(entries: tuple) -> list:
    # "10.0.0.5/8" means the network it lies in, as in client_address
    # (app/core/rate_limit.py); uvicorn would take it for a hostname.
    networks = []
    for entry in entries:
        try:
            networks.append(str(ipaddress.ip_network(entry.strip(), strict=False)))
        except ValueError:
            networks.append(entry)  # not an address; rate_limit logs it
    return networks


@lru_cache(maxsize=8)
def _trusting(app, entries: tuple) -> ProxyHeadersMiddleware:
    # uvicorn's own implementation, so the result (rightmost entry that is
    # not a trusted proxy; X-Forwarded-Proto for the scheme) is exactly what
    # main.py's configuration used to produce. Per TRUSTED_PROXIES value,
    # read on each request, so a changed setting takes effect.
    return ProxyHeadersMiddleware(app, trusted_hosts=_as_networks(entries))


def _report_server_rewrite() -> None:
    global _server_rewrite_reported
    if _server_rewrite_reported:
        return
    _server_rewrite_reported = True
    logger.error(
        "The server applied X-Forwarded-For before the application did, by its own list "
        "of trusted proxies; such requests are counted as from an unknown client. Start "
        "the application with `python main.py`, or pass uvicorn --no-proxy-headers: "
        "TRUSTED_PROXIES is the only list that is meant to apply."
    )


class ForwardedClientMiddleware:
    """Pure ASGI middleware. See the module docstring."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            hosts = _forwarded_hosts(scope["headers"])
            if client and hosts:
                if client[0] in hosts:
                    _report_server_rewrite()
                    scope = {**scope, "client": None}
                elif settings.TRUSTED_PROXIES:
                    proxy = _trusting(self.app, tuple(settings.TRUSTED_PROXIES))
                    return await proxy(scope, receive, send)
        await self.app(scope, receive, send)
