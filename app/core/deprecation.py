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
from typing import Any
from urllib.parse import parse_qsl

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings

DEPRECATED_SINCE = datetime(2026, 10, 23, tzinfo=UTC)
SUNSET = datetime(2027, 6, 1, tzinfo=UTC)
DEPRECATION_DOCS_URL = "https://github.com/nadeko0/fastapi_e-commerce_backend/releases"

# (METHOD, route path template as the routers declare it, i.e. without the
# API_V1_STR prefix, like app/core/rate_limit.py) -> successor URL. Only
# operations that go away belong here, and only with a successor a client can
# move to today (RFC 9745 section 3: the successor-version link).
API = settings.API_V1_STR

DEPRECATED_OPERATIONS: dict[tuple[str, str], str | None] = {
    ("GET", "/users/verify-email/{token}"): f"{API}/users/verify-email",
    ("GET", "/users/password/reset/{token}"): f"{API}/users/password/reset-token/validate",
    ("GET", "/users/data/export"): f"{API}/legal/data-request",
    ("POST", "/users/data/delete"): f"{API}/legal/data-request",
    ("POST", "/users/consent"): f"{API}/legal/consent",
}

# The operation stays; only these query parameters go away in 4.0.0, the same
# data being accepted in the JSON body since 3.3.0 (OpenAPI 3.1 Parameter
# Object `deprecated`: the parameter, not the operation, is declared
# obsolete). A response to a request that actually sends one of them carries
# the Deprecation and Sunset headers; a request that uses the body does not.
# Kept equal to the Query(..., deprecated=True) parameters of the handlers
# (a test compares both with OpenAPI).
DEPRECATED_QUERY_PARAMETERS: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "/orders"): frozenset({"shipping_address_id"}),
    ("PUT", "/orders/{order_id}/status"): frozenset({"status"}),
    ("POST", "/cart/items"): frozenset({"product_id", "quantity", "variant_id"}),
    ("PUT", "/cart/items/{product_id}"): frozenset({"quantity", "variant_id"}),
}

QUERY_PARAMETER_DEPRECATED = (
    "Deprecated, removed in 4.0.0: send this field in the JSON request body instead."
)


def body_or_query(body: BaseModel | None, required: tuple[str, ...], **query: Any) -> dict[str, Any]:
    """The fields of an operation that takes them in the JSON body or, until
    4.0.0, in the query string.

    Sending both is refused with a 422 rather than merged: which of two
    values for the same field wins would be a guess, and a client that sends
    both is mid-migration and better told. Neither sent: the same
    "Field required" 422 for the query parameter that the required query
    parameter gave before the body was accepted.
    """
    used = {name: value for name, value in query.items() if value is not None}
    if body is not None:
        if used:
            raise RequestValidationError([
                {
                    "type": "value_error",
                    "loc": ("query", name),
                    "msg": "Send the fields in the JSON body or in the (deprecated) query string, not both",
                }
                for name in used
            ])
        return body.model_dump()
    missing = [name for name in required if query[name] is None]
    if missing:
        raise RequestValidationError([
            {"type": "missing", "loc": ("query", name), "msg": "Field required"} for name in missing
        ])
    return query


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
            if message["type"] == "http.response.start":
                route = scope.get("route")
                if getattr(route, "deprecated", False):
                    add = deprecation_headers(DEPRECATED_OPERATIONS.get((scope["method"], route.path)))
                elif self._uses_deprecated_query(scope, route):
                    add = deprecation_headers(None)
                else:
                    add = []
                headers = MutableHeaders(scope=message)
                for name, value in add:
                    headers.append(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    def _uses_deprecated_query(scope: Scope, route: Any) -> bool:
        deprecated = DEPRECATED_QUERY_PARAMETERS.get((scope["method"], getattr(route, "path", "")))
        if not deprecated:
            return False
        sent = {name for name, _ in parse_qsl(scope["query_string"].decode("latin-1"), keep_blank_values=True)}
        return bool(deprecated & sent)
