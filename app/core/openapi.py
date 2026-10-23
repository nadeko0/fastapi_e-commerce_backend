"""What the OpenAPI document says about the errors the API sends.

FastAPI describes only the 200 and a generic 422 by itself. The bodies the
API really answers with are these (OpenAPI 3.1.0, Operation Object:
"responses" maps an HTTP status code to a Response Object, with its
headers and its content per media type):

- {"detail": "..."}: every HTTPException (400, 401, 402, 403, 404, 409,
  422 for a bad filter value, 503), the 413 of the body size limit, the
  503 of the in-flight limit and the 500 for a database error.
- {"detail": {"error": "...", "retry_after": 30}}: the 429 of the rate
  limits and the login 503, both with a Retry-After header.
- {"detail": [{"type", "loc", "msg", "url"}, ...]}: the 422 of request
  validation (app/core/errors.py: the error list without `input`).
- plain text "Internal Server Error": the 500 for any other exception
  (app/core/security_headers.py).

DocumentedRoute adds them to each operation they apply to; a route's own
`responses=` entry keeps its description and gains the body and headers.
Additional Responses: https://fastapi.tiangolo.com/advanced/additional-responses/
"""

from typing import Any

from fastapi.routing import APIRoute
from fastapi.security.base import SecurityBase
from pydantic import BaseModel, Field


class ErrorMessage(BaseModel):
    """The error body of an HTTPException."""

    detail: str


class ValidationErrorItem(BaseModel):
    """One request validation error, without the submitted value."""

    type: str
    loc: list[str | int]
    msg: str
    url: str | None = None
    ctx: dict[str, Any] | None = Field(
        None, description="The constraint that was broken (a limit, a pattern), never the input."
    )


class ValidationErrorMessage(BaseModel):
    """The 422 of request validation."""

    detail: list[ValidationErrorItem]


class RateLimitDetail(BaseModel):
    error: str
    retry_after: int = Field(description="Seconds until the request can be repeated.")


class RateLimitMessage(BaseModel):
    """The body of a 429, and of the 503 of the login check."""

    detail: RateLimitDetail


_RETRY_AFTER = {
    "Retry-After": {
        "description": "Seconds to wait before repeating the request.",
        "schema": {"type": "integer", "minimum": 0},
    }
}
_WWW_AUTHENTICATE = {
    "WWW-Authenticate": {
        "description": 'The challenge, "Bearer" (RFC 6750, section 3).',
        "schema": {"type": "string"},
    }
}

BAD_REQUEST = {"description": "The request cannot be served as sent", "model": ErrorMessage}
UNAUTHORIZED = {
    "description": "Missing, invalid, expired or revoked access token",
    "model": ErrorMessage,
    "headers": _WWW_AUTHENTICATE,
}
FORBIDDEN = {"description": "Not allowed for this account", "model": ErrorMessage}
NOT_FOUND = {"description": "Not found", "model": ErrorMessage}
CONFLICT = {"description": "Conflicts with the current state", "model": ErrorMessage}
PAYLOAD_TOO_LARGE = {"description": "Request body is too large", "model": ErrorMessage}
VALIDATION_ERROR = {
    "description": "Validation Error: the error list, or a message for a value rejected by the route",
    "model": ValidationErrorMessage | ErrorMessage,
}
TOO_MANY_REQUESTS = {
    "description": "Too many requests",
    "model": RateLimitMessage,
    "headers": _RETRY_AFTER,
}
SERVICE_UNAVAILABLE = {
    "description": "Temporarily unavailable, or the server is at capacity",
    "model": ErrorMessage | RateLimitMessage,
    "headers": _RETRY_AFTER,
}
SERVER_ERROR = {
    "description": (
        "Server error: a JSON message for a database error, plain text "
        '"Internal Server Error" for anything else'
    ),
    "model": ErrorMessage,
    "content": {"text/plain": {"schema": {"type": "string"}}},
}

# The status codes the model of a response with no body of its own gets
# when a route lists only a description.
_MODEL_FOR_EXISTING = {
    400: ErrorMessage,
    401: ErrorMessage,
    402: ErrorMessage,
    403: ErrorMessage,
    404: ErrorMessage,
    409: ErrorMessage,
    413: ErrorMessage,
    422: ValidationErrorMessage | ErrorMessage,
    429: RateLimitMessage,
    503: ErrorMessage | RateLimitMessage,
}


def _merge(responses: dict, code: int, spec: dict) -> None:
    """Add `spec` as the response for `code`. A route's own entry keeps its
    description, and gains the body and the headers it lacks."""
    own = responses.get(code)
    if own is None:
        responses[code] = spec
        return
    for key, value in spec.items():
        if key != "description":
            own.setdefault(key, value)


def _security_schemes(dependant) -> list[SecurityBase]:
    found = []
    for sub in dependant.dependencies:
        if isinstance(sub.call, SecurityBase):
            found.append(sub.call)
        found.extend(_security_schemes(sub))
    return found


def _has_parameters(dependant) -> bool:
    return bool(
        dependant.path_params or dependant.query_params
        or dependant.header_params or dependant.cookie_params
    ) or any(_has_parameters(sub) for sub in dependant.dependencies)


class DocumentedRoute(APIRoute):
    """An APIRoute whose operation lists the error responses that apply to
    it: 401 where it depends on a security scheme (the cart's optional one
    included: an Authorization header that is sent must be good; a scheme
    that never rejects makes the operation's security optional, `{}`), 413 where
    a body is accepted, 422 where there is anything to validate, 429 and
    503 bodies where the route lists them, and 503 and 500 everywhere (the
    in-flight limit and the unhandled exception can answer any route)."""

    def __init__(self, path: str, endpoint, *, responses=None, **kwargs) -> None:
        super().__init__(path, endpoint, responses=responses, **kwargs)
        schemes = _security_schemes(self.dependant)
        if schemes and not any(getattr(scheme, "auto_error", True) for scheme in schemes):
            # Every scheme the operation reads is one that never rejects a
            # request for lacking credentials (the cart serves guests): the
            # empty Security Requirement Object means "no credentials".
            kwargs["openapi_extra"] = {
                **(kwargs.get("openapi_extra") or {}),
                "security": [{}],
            }
        merged = {code: dict(spec) for code, spec in (self.responses or {}).items()}
        if schemes:
            _merge(merged, 401, UNAUTHORIZED)
        if self.methods & {"POST", "PUT", "PATCH"}:
            _merge(merged, 413, PAYLOAD_TOO_LARGE)
        if self.body_field is not None or _has_parameters(self.dependant):
            _merge(merged, 422, VALIDATION_ERROR)
        _merge(merged, 503, SERVICE_UNAVAILABLE)
        _merge(merged, 500, SERVER_ERROR)
        for code, model in _MODEL_FOR_EXISTING.items():
            if code in merged:
                merged[code].setdefault("model", model)
        if 429 in merged:
            merged[429].setdefault("headers", _RETRY_AFTER)
        # A second pass with the full list: FastAPI builds the response
        # fields (the models) in the constructor.
        super().__init__(path, endpoint, responses=merged, **kwargs)
