"""Deprecation signalling for the operations 4.0.0 removes or changes.

A route is deprecated by `deprecated=True` on its decorator (that is what puts
`deprecated: true` into OpenAPI). DeprecationMiddleware then adds, to every
response of such a route (error responses included), the three headers:

- Deprecation: @<unix time>  RFC 9745, section 2.1: the value is a Date
  (an Item Structured Field, RFC 9651 section 3.3.7), here the day 3.3.0
  announced it.
- Sunset: <HTTP-date>  RFC 8594, section 3. RFC 9745, section 4: it must not
  be earlier than the Deprecation date.
- Link: <docs>; rel="deprecation" (RFC 9745, section 3: human-readable
  deprecation information), plus <successor>; rel="successor-version"
  (RFC 5829) where a replacement exists today. RFC 8288 syntax.

Policy: an operation is announced here in a MINOR release and is removed no
earlier than SUNSET (about seven months later), in the next MAJOR release.
"""

from datetime import UTC, datetime
from email.utils import format_datetime

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings

DEPRECATED_SINCE = datetime(2026, 10, 23, tzinfo=UTC)
SUNSET = datetime(2027, 6, 1, tzinfo=UTC)
DEPRECATION_DOCS_URL = "https://github.com/nadeko0/fastapi_e-commerce_backend/releases"

# (METHOD, route path template as the routers declare it, i.e. without the
# API_V1_STR prefix, like app/core/rate_limit.py) -> successor URL, or None when 4.0.0 changes
# the same URL (data moves from the query string to the body, a GET becomes a
# POST) so there is no other address to point at.
API = settings.API_V1_STR

DEPRECATED_OPERATIONS: dict[tuple[str, str], str | None] = {
    ("GET", "/users/verify-email/{token}"): None,
    ("GET", "/users/password/reset/{token}"): None,
    ("GET", "/users/data/export"): f"{API}/legal/data-request",
    ("POST", "/users/data/delete"): f"{API}/legal/data-request",
    ("POST", "/users/consent"): f"{API}/legal/consent",
    ("POST", "/orders"): None,
    ("PUT", "/orders/{order_id}/status"): None,
    ("POST", "/cart/items"): None,
    ("PUT", "/cart/items/{product_id}"): None,
}


def deprecation_headers(successor: str | None) -> list[tuple[str, str]]:
    links = [f'<{DEPRECATION_DOCS_URL}>; rel="deprecation"; type="text/html"']
    if successor:
        links.append(f'<{successor}>; rel="successor-version"')
    return [
        ("Deprecation", f"@{int(DEPRECATED_SINCE.timestamp())}"),
        ("Sunset", format_datetime(SUNSET, usegmt=True)),
        ("Link", ", ".join(links)),
    ]


class DeprecationMiddleware:
    """Add the deprecation headers to the responses of deprecated routes.

    Plain ASGI, so the headers are also on 4xx/5xx responses raised inside the
    route. The route flag is the single source of truth: a route is deprecated
    when it is flagged `deprecated=True`; the successor comes from
    DEPRECATED_OPERATIONS.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            # By the time the response starts, routing has put the matched
            # route in the scope (the same dict this middleware was given).
            route = scope.get("route")
            if message["type"] == "http.response.start" and getattr(route, "deprecated", False):
                successor = DEPRECATED_OPERATIONS.get((scope["method"], route.path))
                headers = MutableHeaders(scope=message)
                for name, value in deprecation_headers(successor):
                    headers.append(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)
