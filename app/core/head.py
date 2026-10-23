"""HEAD for every GET route (RFC 9110 section 9.1: "All general-purpose servers
MUST support the methods GET and HEAD"; section 9.3.2: HEAD is identical to
GET except that the server MUST NOT send content).

FastAPI routes answer only the methods they declare, so HEAD was a 405
everywhere. Declaring HEAD on each route would put a second operation (and a
second operation id) per path into OpenAPI and would give HEAD its own
rate-limit counter. Instead this middleware runs a HEAD request as the GET it
stands for - same route, same dependencies, same status, same headers - and
drops the body. Consequences:

- Rate limiting, deprecation headers, ETag/If-None-Match (304) and the
  request id all see a GET: HEAD counts against the budget of its GET, so it
  neither escapes the limit nor is counted twice. (The probes keep their own
  explicit HEAD routes and are passed through untouched.)
- Content-Length is the one the GET response declares (section 9.3.2 allows
  it: it is what a GET would have sent). A streamed GET declares none.
- A HEAD to a path with no GET route gets that route's 405, whose Allow
  lists what the path really offers.
- The 405 Allow header lists HEAD whenever it lists GET (section 10.2.1:
  the Allow of a 405 names the methods the resource supports).
- OpenAPI is untouched: no route changes.

Plain ASGI, innermost: the scope is mutated in place so the router, and the
middleware outside this one reading the route afterwards, see the same dict.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.probes import PROBE_PATHS


class HeadMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_head = scope["method"] == "HEAD" and scope["path"] not in PROBE_PATHS
        if is_head:
            scope["method"] = "GET"

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                if message["status"] == 405:
                    headers = MutableHeaders(scope=message)
                    allow = headers.get("allow")
                    if allow:
                        methods = [m.strip() for m in allow.split(",") if m.strip()]
                        if "GET" in methods and "HEAD" not in methods:
                            methods.append("HEAD")
                            headers["allow"] = ", ".join(methods)
            elif is_head and message["type"] == "http.response.body":
                message = {
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": message.get("more_body", False),
                }
            await send(message)

        await self.app(scope, receive, send_wrapper)
