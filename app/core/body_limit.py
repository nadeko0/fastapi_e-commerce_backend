from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodySizeLimitMiddleware:
    """Answer 413 to a request whose body is larger than `limit` bytes.

    Neither Starlette nor uvicorn bounds the body: /users/register parsed a
    30 MB JSON document, and the Stripe webhook reads the whole body into
    memory before it checks the signature, so an unauthenticated caller
    could make every worker allocate as much as it cared to send (the
    webhook is also exempt from rate limiting). 413 Content Too Large is
    the status for this (RFC 9110, section 15.5.14).

    Two cases, both without a buffer of our own:

    - Content-Length over the limit: answered at once, before the app is
      called, so not one byte of the body is read.
    - No Content-Length (chunked), or a body longer than it declared: the
      bytes are counted as the app pulls them through `receive`. When the
      total passes the limit the 413 is sent right there, and the app is
      handed an http.disconnect instead of the rest of the body, so it can
      never process a truncated body as if it were complete (Starlette's
      Request.body() raises ClientDisconnect on it). Whatever the app then
      tries to send is dropped; the 413 is the only response.

    Plain ASGI rather than BaseHTTPMiddleware for the same reason as
    app/core/overload.py: it must wrap `receive` itself. It is registered
    innermost, see app/main.py.
    """

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit

    @staticmethod
    def _too_large() -> JSONResponse:
        return JSONResponse(
            {"detail": "Request body is too large"},
            status_code=413,
            # The rest of an oversized body may still be on the wire; do not
            # try to read it on a reused connection.
            headers={"Connection": "close"},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.limit:
            await self._too_large()(scope, receive, send)
            return

        received = 0
        rejected = False
        response_started = False

        async def counting_receive() -> Message:
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    rejected = True
                    if not response_started:
                        await self._too_large()(scope, receive, send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if rejected:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except Exception:
            # Once the 413 is out, the app failing on the disconnect it was
            # handed (ClientDisconnect, or whatever it makes of it) is the
            # expected end of the request, not a server error to log.
            if not rejected:
                raise
