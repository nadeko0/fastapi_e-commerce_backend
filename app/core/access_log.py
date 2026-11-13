"""One structured access log line per HTTP request.

uvicorn's access log is plain text, written before the application has
resolved the client behind the proxy (so it showed the proxy's address, port
0) and without the request id, so main.py turns it off and this middleware
writes the line instead, through the application's JSON logging
(app/core/logging_config.py), which adds request_id and trace_id.

Fields: method, path, route, status, duration_ms, client_ip. `path` is the
request path without the query string (path parameters are ids, never
tokens: those travel in the query or the body, which are not logged), and
`route` the matched route's template as FastAPI and the rate limiter name it
(relative to the router's prefix: "/products/{product_id}"), or null when no
route matched. No header is logged.

Placed just inside RequestContextMiddleware (the request id is bound) and
outside everything else, so shed 503s and 404s are logged too. Read after
the request: the router writes the matched route into the scope, and
ForwardedClientMiddleware rewrites scope["client"] by TRUSTED_PROXIES, both
in place. A request shed by the in-flight limit never reached that rewrite
and is logged with the peer's address.
"""

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("app.access")


class AccessLogMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        method = scope["method"]  # before HeadMiddleware turns HEAD into GET
        path = scope["path"]
        # An exception that escapes here becomes ServerErrorMiddleware's 500.
        status = 500

        async def send_and_note_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_note_status)
        finally:
            route = scope.get("route")
            client = scope.get("client")
            logger.info(
                "%s %s %s",
                method,
                path,
                status,
                extra={
                    "method": method,
                    "path": path,
                    "route": route.path if route is not None else None,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "client_ip": client[0] if client else None,
                },
            )
