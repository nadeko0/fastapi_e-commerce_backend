"""Every error response of the API is an RFC 9457 problem details object.

Until 4.0.0 the API had five error shapes: {"detail": "..."} (HTTPException),
{"detail": {"error", "retry_after"}} (429), {"detail": [...]} (422), plain
text (500) and Starlette's own 404/405. A client needed a parser per shape.
Now all of them are `application/problem+json` (RFC 9457,
https://www.rfc-editor.org/rfc/rfc9457.html), one shape to parse:

    {"type": "about:blank", "title": "Not Found", "status": 404,
     "detail": "Product not found", "instance": "/api/v1/products/7",
     "request_id": "..."}

Members (RFC 9457 section 3):

- `type`: "about:blank" when the problem has no meaning beyond its status
  code (section 4.2.1), with `title` the status phrase. Where a client has
  to branch on a specific problem, a stable type URI (ProblemType below)
  with a fixed `title` (a title must not change between occurrences,
  section 3.1.3). The URIs are absolute and not dereferenceable, which
  section 3.1.1 allows: they are identifiers, clients must not fetch them.
- `status`: the HTTP status code, the same as the response's (3.1.2).
- `detail`: this occurrence, for a human; a client must not parse it
  (3.1.4). Absent on a 500: the text of an unhandled exception can carry
  implementation details (section 5).
- `instance`: the request path, percent-encoded, without the query string
  (a query can carry tokens or personal data).
- Extension members (3.2; a client ignores those it does not know):
  `request_id` on every problem (the X-Request-ID header's value, to quote
  to support); `errors` on a validation problem, a list of {type, loc, msg}
  and the constraint `ctx`, never `input`; `retry_after` (seconds, the
  Retry-After header's value) on a 429 or a 503 that has one.

Raising: a plain HTTPException(status, detail="...") stays valid everywhere
and becomes an about:blank problem with that detail. Only problems a client
branches on are raised as `Problem(status, type=ProblemType.X, detail=...)`,
a subclass of HTTPException, so `except HTTPException` code still sees it.
Starlette finds the handler through the exception's MRO, so the one handler
registered for Starlette's HTTPException (the class FastAPI's inherits from,
and the one Starlette raises for 404 and 405) answers both.

Statuses are the ones HTTP defines for the situation. Two requests that used
to be a 422 are not: a body that is not JSON at all is a 400 (RFC 9110
15.5.1: malformed syntax), and a body sent with a Content-Type that is not
JSON is a 415 (15.5.16), with `Accept` naming the media type that would have
been accepted. A request that parses but breaks a rule stays 422.

Input is never echoed: see _CONSTRAINT_KEYS (OWASP REST Security Cheat
Sheet, "Respond with generic error messages"; FastAPI's default 422 put the
submitted password in the response).
"""

from enum import StrEnum
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

PROBLEM_MEDIA_TYPE = "application/problem+json"
_TYPE_BASE = "https://github.com/nadeko0/fastapi_e-commerce_backend/problems/"

# The detail FastAPI gives to the 400 for a body it could not read as text
# (invalid UTF-8): fastapi/routing.py, get_request_handler.
_UNREADABLE_BODY_DETAIL = "There was an error parsing the body"


class ProblemType(StrEnum):
    """The problem types a client can branch on; the only place they are defined."""

    VALIDATION_ERROR = _TYPE_BASE + "validation-error"
    MALFORMED_REQUEST = _TYPE_BASE + "malformed-request"
    UNSUPPORTED_MEDIA_TYPE = _TYPE_BASE + "unsupported-media-type"
    BODY_TOO_LARGE = _TYPE_BASE + "body-too-large"
    RATE_LIMITED = _TYPE_BASE + "rate-limited"
    SERVICE_OVERLOADED = _TYPE_BASE + "service-overloaded"
    IDEMPOTENCY_KEY_REUSED = _TYPE_BASE + "idempotency-key-reused"
    IDEMPOTENCY_KEY_IN_USE = _TYPE_BASE + "idempotency-key-in-use"
    PAYMENT_DECLINED = _TYPE_BASE + "payment-declined"
    INSUFFICIENT_STOCK = _TYPE_BASE + "insufficient-stock"


PROBLEM_TITLES = {
    ProblemType.VALIDATION_ERROR: "Request validation failed",
    ProblemType.MALFORMED_REQUEST: "Malformed request",
    ProblemType.UNSUPPORTED_MEDIA_TYPE: "Unsupported media type",
    ProblemType.BODY_TOO_LARGE: "Request body too large",
    ProblemType.RATE_LIMITED: "Too many requests",
    ProblemType.SERVICE_OVERLOADED: "Service overloaded",
    ProblemType.IDEMPOTENCY_KEY_REUSED: "Idempotency key reused with a different request",
    ProblemType.IDEMPOTENCY_KEY_IN_USE: "Idempotency key in use",
    ProblemType.PAYMENT_DECLINED: "Payment declined",
    ProblemType.INSUFFICIENT_STOCK: "Insufficient stock",
}


class Problem(HTTPException):
    """An HTTPException that carries a problem type, and the extension
    members `errors` and `retry_after`. `retry_after` also sets the
    Retry-After header, so the two cannot disagree."""

    def __init__(
        self,
        status_code: int,
        *,
        type: ProblemType | None = None,
        detail: str | None = None,
        headers: dict[str, str] | None = None,
        retry_after: int | None = None,
        errors: list[dict] | None = None,
    ) -> None:
        if retry_after is not None:
            headers = {**(headers or {}), "Retry-After": str(retry_after)}
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        # Starlette fills a missing detail with the status phrase; a problem
        # without one (the 500) has no detail member.
        self.detail = detail
        self.problem_type = type
        self.retry_after = retry_after
        self.errors = errors


class ProblemResponse(JSONResponse):
    media_type = PROBLEM_MEDIA_TYPE


def problem_response(scope: Scope, exc: StarletteHTTPException) -> ProblemResponse:
    """The response for `exc`, from the ASGI scope alone so that middleware
    (the body and in-flight limits, ServerErrorMiddleware's handler) can send
    it where there is no Request to build."""
    problem_type = getattr(exc, "problem_type", None)
    body: dict[str, Any] = {
        "type": problem_type or "about:blank",
        "title": (
            PROBLEM_TITLES[problem_type] if problem_type else HTTPStatus(exc.status_code).phrase
        ),
        "status": exc.status_code,
    }
    if exc.detail is not None:
        body["detail"] = exc.detail
    body["instance"] = quote(scope["path"])
    # Set by RequestContextMiddleware, which every layer that answers sits
    # inside, except ServerErrorMiddleware, which reads it from the scope
    # state the middleware left behind.
    request_id = scope.get("state", {}).get("request_id")
    if request_id:
        body["request_id"] = request_id
    if getattr(exc, "errors", None) is not None:
        body["errors"] = exc.errors
    if getattr(exc, "retry_after", None) is not None:
        body["retry_after"] = exc.retry_after
    return ProblemResponse(body, status_code=exc.status_code, headers=exc.headers)


# ctx keys that describe the rule, not the submitted value.
_CONSTRAINT_KEYS = frozenset({
    "gt", "ge", "lt", "le", "multiple_of",
    "min_length", "max_length", "expected_length", "actual_length",
    "pattern", "expected",
})


def _without_input(error: dict) -> dict:
    clean = {key: value for key, value in error.items() if key not in ("input", "ctx")}
    ctx = {k: v for k, v in (error.get("ctx") or {}).items() if k in _CONSTRAINT_KEYS}
    if ctx:
        clean["ctx"] = ctx
    return clean


def sanitized_errors(errors: list[dict]) -> list[dict]:
    """`errors()` of a RequestValidationError or a pydantic ValidationError,
    without `input` and with `ctx` cut down to the constraint values."""
    return jsonable_encoder([_without_input(e) for e in errors])


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    if exc.status_code in {204, 304}:
        # No body is allowed on these (RFC 9110 15.3.5, 15.4.5).
        return Response(status_code=exc.status_code, headers=exc.headers)
    if exc.status_code == 400 and exc.detail == _UNREADABLE_BODY_DETAIL:
        exc = Problem(
            400, type=ProblemType.MALFORMED_REQUEST,
            detail="The request body could not be read as text",
        )
    return problem_response(request.scope, exc)


def _unsupported_media_type(request: Request) -> Problem:
    accepted = "application/json"
    if request.method == "PATCH":
        accepted = "application/merge-patch+json, application/json"
    return Problem(
        415, type=ProblemType.UNSUPPORTED_MEDIA_TYPE,
        detail="The request body must be JSON: send it with Content-Type: application/json",
        headers={"Accept": accepted},
    )


async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> Response:
    errors = exc.errors()
    if any(error["type"] == "json_invalid" and error["loc"][0] == "body" for error in errors):
        problem = Problem(
            400, type=ProblemType.MALFORMED_REQUEST,
            detail="The request body is not valid JSON",
        )
    elif isinstance(exc.body, bytes):
        # FastAPI parses a body as JSON only for a JSON Content-Type
        # (strict_content_type); for any other it hands over the raw bytes,
        # which no schema accepts.
        problem = _unsupported_media_type(request)
    else:
        problem = Problem(
            422, type=ProblemType.VALIDATION_ERROR,
            detail="The request is invalid; see errors",
            errors=sanitized_errors(errors),
        )
    return problem_response(request.scope, problem)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
