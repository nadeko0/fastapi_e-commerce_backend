from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection, Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.v1.cart import guest_cart_cookie_name
from app.core.config import settings

# Responses of these prefixes carry tokens, personal data or a payment
# client_secret: no cache may keep them (RFC 9111 section 5.2.2.5). So does
# every response to a request that is authenticated (Authorization header)
# or is a guest's own (guest-cart cookie), wherever it is routed.
_NO_STORE_PREFIXES = tuple(
    f"{settings.API_V1_STR}/{name}" for name in ("users", "orders", "cart", "admin")
)


# OWASP REST Security Cheat Sheet: a JSON API answers default-src 'none' and
# frame-ancestors 'none'. Only the interactive docs pages (FastAPI serves
# them, outside production only, see main.py) are HTML that loads Swagger UI
# and ReDoc from cdn.jsdelivr.net and runs an inline script: they keep the
# policy the app has always sent.
_API_CSP = "default-src 'none'; frame-ancestors 'none'"
_DOCS_CSP = (
    "default-src 'self';"
    "img-src 'self' data: https:;"
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;"
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;"
    "font-src 'self' https://cdn.jsdelivr.net;"
)
_DOCS_PATHS = frozenset(
    {f"{settings.API_V1_STR}/docs", "/redoc", "/docs/oauth2-redirect"}
)


def _needs_no_store(scope: Scope) -> bool:
    path = scope["path"]
    if any(path == p or path.startswith(p + "/") for p in _NO_STORE_PREFIXES):
        return True
    connection = HTTPConnection(scope)
    return (
        "authorization" in connection.headers
        or guest_cart_cookie_name() in connection.cookies
    )


def apply_security_headers(headers: MutableHeaders, scope: Scope) -> None:
    if "cache-control" not in headers and _needs_no_store(scope):
        headers["Cache-Control"] = "no-store"
    # Sent whatever the scheme the app sees: behind a TLS-terminating proxy
    # the app sees http (and learns https only from X-Forwarded-Proto, which
    # ForwardedClientMiddleware applies inside this layer and only for a
    # trusted proxy), so a scheme check here would drop the header exactly
    # where it matters. Browsers ignore it over http (RFC 6797 section 8.1).
    headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    headers["X-Content-Type-Options"] = "nosniff"
    headers["X-Frame-Options"] = "DENY"
    # OWASP Secure Headers Project values; an API has no use for referrers,
    # cross-origin no-cors embedding or a shared browsing-context group.
    # (CORP only gates no-cors requests; CORS reads are governed by CORS.)
    headers["Referrer-Policy"] = "no-referrer"
    headers["Cross-Origin-Resource-Policy"] = "same-origin"
    headers["Cross-Origin-Opener-Policy"] = "same-origin"
    # No X-XSS-Protection: deprecated, and it can create XSS (MDN, OWASP).
    headers["Content-Security-Policy"] = (
        _DOCS_CSP if scope["path"] in _DOCS_PATHS else _API_CSP
    )
    headers["Permissions-Policy"] = "geolocation=(), microphone=()"


class SecurityHeadersMiddleware:
    """Add the security headers to every response that passes through.

    A plain ASGI middleware that wraps `send`, not @app.middleware("http")
    (BaseHTTPMiddleware): it adds no task or buffering to a request, and it
    can be placed outside InFlightLimitMiddleware (add it last), so the 503
    that one sheds is covered too.

    What no user middleware can cover is the 500 for an unhandled exception:
    Starlette's ServerErrorMiddleware is always the outermost layer and
    sends that response itself. For that, see server_error_handler.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                apply_security_headers(MutableHeaders(scope=message), scope)
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def server_error_handler(request: Request, exc: Exception) -> Response:
    """The 500 for an unhandled exception: Starlette's own default response
    (plain text, "Internal Server Error"), with the security headers.

    Registered for Exception, which Starlette hands to ServerErrorMiddleware
    as its handler. The exception is still re-raised after this response is
    sent, so server logs and the test client see it as before."""
    response = PlainTextResponse("Internal Server Error", status_code=500)
    apply_security_headers(response.headers, request.scope)
    return response
