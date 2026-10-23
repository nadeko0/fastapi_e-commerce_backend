"""Request id and W3C Trace Context for every HTTP request.

A pure ASGI middleware (no BaseHTTPMiddleware, so no extra task or
buffering) that
- takes an incoming X-Request-ID if it is a sane token, else generates one
  (uuid4 hex), and returns it in the X-Request-ID response header;
- parses an incoming `traceparent` (W3C Trace Context, section 3.2) and keeps
  the trace-id and the caller's span id;
- keeps both in a contextvar that the JSON log formatter reads, so every log
  record written while handling the request carries request_id / trace_id.
  anyio's worker threads (run_in_threadpool, sync routes) run in a copy of the
  caller's context, so they see the same values.

Not done on purpose: no traceparent is sent on outgoing calls or echoed back
(the spec requires neither; there is no tracing SDK in the app), and the
`traceresponse` header is a separate W3C draft, not implemented. Celery tasks
do not receive the context.
"""

import re
import uuid
from contextvars import ContextVar
from dataclasses import dataclass

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"

# Token charset and length bounded: the value is echoed into a response header
# and into logs, so no whitespace, controls, separators or non-ASCII.
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")

# https://www.w3.org/TR/trace-context/#traceparent-header-field-values
# Lowercase hex only. Version 00 has exactly these four fields (55 chars);
# a higher version may append "-" and more data, which is not interpreted.
_TRACEPARENT_RE = re.compile(
    r"(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<parent_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})(?P<rest>.*)",
    re.DOTALL,
)


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    trace_id: str | None = None
    parent_span_id: str | None = None


_context: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)


def get_request_context() -> RequestContext | None:
    return _context.get()


def parse_traceparent(value: str) -> tuple[str, str] | None:
    """Return (trace_id, parent_span_id), or None when the header is invalid
    (the spec says to ignore it then)."""
    match = _TRACEPARENT_RE.fullmatch(value)
    if match is None:
        return None
    version, rest = match["version"], match["rest"]
    if version == "ff":
        return None
    if version == "00":
        if rest:
            return None
    elif rest and not rest.startswith("-"):
        return None
    if match["trace_id"] == "0" * 32 or match["parent_id"] == "0" * 16:
        return None
    return match["trace_id"], match["parent_id"]


def _single_header(headers: Headers, name: str) -> str | None:
    values = headers.getlist(name)
    return values[0] if len(values) == 1 else None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        incoming = _single_header(headers, REQUEST_ID_HEADER)
        if incoming is not None and _REQUEST_ID_RE.fullmatch(incoming):
            request_id = incoming
        else:
            request_id = uuid.uuid4().hex

        trace = None
        traceparent = _single_header(headers, "traceparent")
        if traceparent is not None:
            trace = parse_traceparent(traceparent)

        context = RequestContext(
            request_id=request_id,
            trace_id=trace[0] if trace else None,
            parent_span_id=trace[1] if trace else None,
        )
        # Also on the scope: the 500 for an unhandled exception is sent by
        # ServerErrorMiddleware after this layer has been left.
        scope.setdefault("state", {})["request_id"] = request_id
        token = _context.set(context)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            _context.reset(token)
