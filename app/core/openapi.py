"""What the OpenAPI document says about the errors the API sends.

FastAPI describes only the 200 and a generic 422 by itself. Every error the
API answers with is an RFC 9457 problem details object, media type
application/problem+json (app/core/errors.py): the `Problem` schema, or
`ValidationProblem` where the 422 carries the error list. DocumentedRoute
adds them to each operation they apply to (OpenAPI 3.1.0, Operation Object:
"responses" maps an HTTP status code to a Response Object, with its headers
and its content per media type); a route's own `responses=` entry keeps its
description and gains the body and headers.

FastAPI files an additional response's model under the route's own media
type (application/json), and the 422 it adds by itself, with its
HTTPValidationError, only when the route lists none; install_problem_media_type
moves every error response to application/problem+json once the document is
built. The 422 is always listed here, so the default one never appears.
Additional Responses: https://fastapi.tiangolo.com/advanced/additional-responses/
"""

from typing import Any

from fastapi import FastAPI, params
from fastapi.routing import APIRoute
from fastapi.security.base import SecurityBase
from pydantic import BaseModel, Field

from app.core.errors import PROBLEM_MEDIA_TYPE, ProblemType
from app.core.rate_limit import _EXEMPT as _RATE_LIMIT_EXEMPT
from app.core.security import (
    get_current_active_user,
    get_current_admin_user,
    get_optional_active_user,
)


class Problem(BaseModel):
    """An error, as RFC 9457 problem details."""

    type: str = Field(
        "about:blank",
        description=(
            '"about:blank" when the problem has no meaning beyond its status code, '
            "else one of the problem types, which are identifiers, not links: "
            + ", ".join(problem_type.value for problem_type in ProblemType)
        ),
        # Published, not enforced: this model only describes what the
        # handlers in app/core/errors.py send (the same technique as the
        # status filters in app/api/v1/admin/orders.py).
        json_schema_extra={"enum": ["about:blank", *(problem_type.value for problem_type in ProblemType)]},
    )
    title: str = Field(description="The status phrase for about:blank, else a fixed title of the type.")
    status: int = Field(description="The HTTP status code.")
    detail: str | None = Field(None, description="This occurrence, for a human to read; absent on a 500.")
    instance: str = Field(description="The request path, without the query string.")
    request_id: str = Field(description="The X-Request-ID of the response, to quote to support.")
    retry_after: int | None = Field(
        None, description="Seconds until the request can be repeated; also the Retry-After header."
    )


class ValidationErrorItem(BaseModel):
    """One request validation error, without the submitted value."""

    type: str
    loc: list[str | int]
    msg: str
    url: str | None = None
    ctx: dict[str, Any] | None = Field(
        None, description="The constraint that was broken (a limit, a pattern), never the input."
    )


class ValidationProblem(Problem):
    """The 422 of request validation."""

    errors: list[ValidationErrorItem]


RETRY_AFTER_HEADER = {
    "Retry-After": {
        "description": "Seconds to wait before repeating the request.",
        "schema": {"type": "integer", "minimum": 0},
    }
}
# IETF draft-ietf-httpapi-ratelimit-headers (app/core/rate_limit.py,
# rate_limit_headers).
RATE_LIMIT_HEADERS = {
    "RateLimit-Policy": {
        "description": 'The limit: "<policy>";q=<requests>;w=<window seconds>.',
        "schema": {"type": "string"},
    },
    "RateLimit": {
        "description": 'What is left of it: "<policy>";r=<remaining>;t=<seconds until the window ends>.',
        "schema": {"type": "string"},
    },
}
# On the 201 of a POST (app/core/location.py).
LOCATION_HEADER = {
    "Location": {
        "description": "The URL of the resource that was created (RFC 9110 10.2.2).",
        "schema": {"type": "string"},
    }
}
# On the 200 and 304 of a product or variant that has a validator
# (app/core/etag.py): a hash of the JSON of the response body. Send it back
# in If-Match (PUT/PATCH) or If-None-Match (GET).
ETAG_HEADER = {
    "ETag": {
        "description": (
            "Strong entity tag of this representation (RFC 9110 8.8.3). Send it back as "
            "If-Match on PUT/PATCH, or as If-None-Match on GET."
        ),
        "schema": {"type": "string"},
    }
}
ETAG_RESPONSE = {"description": "Successful Response", "headers": ETAG_HEADER}
# The guest cart cookie (app/api/v1/cart.py, "Why a cookie, and why this cookie").
GUEST_CART_COOKIE_HEADER = {
    "Set-Cookie": {
        "description": (
            "The guest cart cookie, for a request without an Authorization header: "
            '"guest_cart" ("__Host-guest_cart" when GUEST_CART_COOKIE_SECURE is on), '
            "HttpOnly, SameSite=Lax, Path=/, Secure when GUEST_CART_COOKIE_SECURE is on. "
            "Max-Age is REDIS_GUEST_CART_TTL_DAYS days (7 by default) and is renewed by every write. "
            "When the write leaves the cart empty the cookie is cleared instead (Max-Age=0). "
            "Absent for a request made with an account."
        ),
        "schema": {"type": "string"},
    }
}
GUEST_CART_COOKIE_RESPONSE = {"description": "Successful Response", "headers": GUEST_CART_COOKIE_HEADER}
CLEAR_GUEST_CART_COOKIE_HEADER = {
    "Set-Cookie": {
        "description": (
            'Clears the guest cart cookie ("guest_cart", or "__Host-guest_cart" when '
            "GUEST_CART_COOKIE_SECURE is on; Max-Age=0, Path=/) when the request carried one and its cart "
            "was merged into the account's cart, or named no cart. Absent otherwise, or when the "
            "merge could not be done (the cookie is kept for the next login)."
        ),
        "schema": {"type": "string"},
    }
}
CLEAR_GUEST_CART_COOKIE_RESPONSE = {"description": "Successful Response", "headers": CLEAR_GUEST_CART_COOKIE_HEADER}
# The request id the RequestContextMiddleware puts on every response.
REQUEST_ID_HEADER = {
    "X-Request-ID": {
        "description": "Identifier of this request (the one sent in X-Request-ID if it was usable), to quote to support.",
        "schema": {"type": "string"},
    }
}
_WWW_AUTHENTICATE = {
    "WWW-Authenticate": {
        "description": 'The challenge, "Bearer" (RFC 6750, section 3).',
        "schema": {"type": "string"},
    }
}
_ACCEPT = {
    "Accept": {
        "description": "The media type the request body can be sent as (RFC 9110, section 15.5.16).",
        "schema": {"type": "string"},
    }
}

BAD_REQUEST = {"description": "The request cannot be served as sent", "model": Problem}
UNAUTHORIZED = {
    "description": "Missing, invalid, expired or revoked access token",
    "model": Problem,
    "headers": _WWW_AUTHENTICATE,
}
FORBIDDEN = {"description": "Not allowed for this account", "model": Problem}
# What an account that was deactivated while its token is still valid gets
# from get_current_active_user, on top of whatever else a route's 403 means.
ACCOUNT_INACTIVE_NOTE = "the account is inactive (problem type account-inactive)"
PAYLOAD_TOO_LARGE = {"description": "Request body is too large (MAX_REQUEST_BODY_BYTES, 1 MiB by default)", "model": Problem}
UNSUPPORTED_MEDIA_TYPE = {
    "description": "The request body is not sent as JSON",
    "model": Problem,
    "headers": _ACCEPT,
}
VALIDATION_ERROR = {
    "description": "Validation Error: the error list, or a message for a value rejected by the route",
    "model": ValidationProblem | Problem,
}
TOO_MANY_REQUESTS = {
    "description": (
        "Too many requests: wait Retry-After seconds. Limits are counted per route and per user (client "
        "address without a token): RATE_LIMIT_DEFAULT 300/minute, RATE_LIMIT_SEARCH 60/minute for "
        "product search, RATE_LIMIT_CHECKOUT 10/minute for creating an order, paying and opening a "
        "checkout session, RATE_LIMIT_AUTH 20/minute per client address for register, login, "
        "password reset, email verification and data requests (defaults shown). Every counted response, "
        "a success or an error, carries RateLimit-Policy and RateLimit for the route's limit. A 429 of a "
        "narrower limit is decided by Retry-After: the failed-login one names its own policy in these "
        "fields, the ones for new guest carts (RATE_LIMIT_GUEST_CART) and consent changes "
        "(RATE_LIMIT_CONSENT) carry the route's"
    ),
    "model": Problem,
    "headers": {**RETRY_AFTER_HEADER, **RATE_LIMIT_HEADERS},
}
SERVICE_UNAVAILABLE = {
    "description": (
        "Temporarily unavailable: the server is at capacity (Retry-After: 1), or a service it needs cannot "
        "answer (the token check or the cart store, Retry-After 5 to 30; the database; the payment "
        "provider, Retry-After: 5). Repeat the request after Retry-After seconds, with the same "
        "Idempotency-Key where the route takes one"
    ),
    "model": Problem,
    "headers": RETRY_AFTER_HEADER,
}
SERVER_ERROR = {"description": "Server error", "model": Problem}

# Added to a route's own description of these codes: the general cause still
# applies to the route (a route's 503 for its cart store does not stop the
# in-flight limit from answering 503).
_ALSO_UNAUTHORIZED = "or a missing, invalid, expired or revoked access token"
_ALSO_BAD_REQUEST = "or a body that is not valid JSON (problem type malformed-request)"
_ALSO_SERVICE_UNAVAILABLE = (
    "or, as on any route: the server is at capacity (Retry-After: 1), or the token check or the "
    "database cannot answer (Retry-After 5 to 30)"
)

# The status codes the model of a response with no body of its own gets
# when a route lists only a description.
_MODEL_FOR_EXISTING = {
    400: Problem,
    401: Problem,
    402: Problem,
    403: Problem,
    404: Problem,
    409: Problem,
    412: Problem,
    413: Problem,
    422: ValidationProblem | Problem,
    429: Problem,
    503: Problem,
}


def _merge(
    responses: dict[int | str, dict[str, Any]], code: int, spec: dict[str, Any], also: str | None = None
) -> None:
    """Add `spec` as the response for `code`. A route's own entry keeps its
    description, followed by `also` when given, and gains the body and the
    headers it lacks."""
    own = responses.get(code)
    if own is None:
        responses[code] = spec
        return
    if also:
        own["description"] = f"{own['description']}; {also}"
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


def _depends_on(dependant, call) -> bool:
    return any(sub.call is call or _depends_on(sub, call) for sub in dependant.dependencies)


def _has_parameters(dependant) -> bool:
    return bool(
        dependant.path_params or dependant.query_params
        or dependant.header_params or dependant.cookie_params
    ) or any(_has_parameters(sub) for sub in dependant.dependencies)


class DocumentedRoute(APIRoute):
    """An APIRoute whose operation lists the error responses that apply to
    it: 403 account-inactive where it needs an active user, 401 where it
    depends on a security scheme (the cart's optional one
    included: an Authorization header that is sent must be good; a scheme
    that never rejects makes the operation's security optional, `{}`), 413 where
    a body is accepted, 422 where there is anything to validate, 429 and
    503 bodies where the route lists them, and 503 and 500 everywhere (the
    in-flight limit and the unhandled exception can answer any route), and
    400 and 415 where a JSON body is accepted."""

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
        if _depends_on(self.dependant, get_current_admin_user):
            # One marker for every admin operation, the ones outside /admin
            # (PUT /orders/{id}/status, the refund) included.
            kwargs["openapi_extra"] = {**(kwargs.get("openapi_extra") or {}), "x-admin-only": True}
        merged = {code: dict(spec) for code, spec in (self.responses or {}).items()}
        if schemes:
            _merge(merged, 401, UNAUTHORIZED, _ALSO_UNAUTHORIZED)
        if _depends_on(self.dependant, get_current_active_user) or _depends_on(
            self.dependant, get_optional_active_user
        ):
            # An inactive account is a 403 on every route that needs an
            # active user, the cart's optional one included once an
            # Authorization header is sent. A route's own 403 keeps its
            # description and gains the note.
            own = merged.get(403)
            if own is None:
                _merge(merged, 403, {**FORBIDDEN, "description": ACCOUNT_INACTIVE_NOTE.capitalize()})
            else:
                own["description"] = f"{own['description']}; {ACCOUNT_INACTIVE_NOTE}"
        if not any((method, self.path) in _RATE_LIMIT_EXEMPT for method in self.methods or ()):
            # The rate limit is an application-wide dependency
            # (app/core/rate_limit.py): any route can answer 429, but the
            # exempt ones (the Stripe webhook).
            _merge(merged, 429, TOO_MANY_REQUESTS)
        if self.methods and self.methods & {"POST", "PUT", "PATCH"}:
            _merge(merged, 413, PAYLOAD_TOO_LARGE)
        if self.body_field is not None and not isinstance(self.body_field.field_info, params.Form):
            # A JSON body: a text that is not JSON is a 400, another
            # Content-Type a 415 (app/core/errors.py).
            _merge(merged, 400, BAD_REQUEST, _ALSO_BAD_REQUEST)
            _merge(merged, 415, UNSUPPORTED_MEDIA_TYPE)
        if self.body_field is not None or _has_parameters(self.dependant):
            _merge(merged, 422, VALIDATION_ERROR)
        _merge(merged, 503, SERVICE_UNAVAILABLE, _ALSO_SERVICE_UNAVAILABLE)
        _merge(merged, 500, SERVER_ERROR)
        for code, model in _MODEL_FOR_EXISTING.items():
            if code in merged:
                merged[code].setdefault("model", model)
        if 429 in merged:
            merged[429].setdefault("headers", RETRY_AFTER_HEADER)
        if 401 in merged:
            # Every 401 carries the challenge, a wrong password included
            # (app/core/security.py, and the routes that check one).
            merged[401].setdefault("headers", _WWW_AUTHENTICATE)
        # A second pass with the full list: FastAPI builds the response
        # fields (the models) in the constructor.
        super().__init__(path, endpoint, responses=merged, **kwargs)


# Functions that serve two methods (a PUT and a PATCH decorating one handler):
# their name alone would be the operationId of both.
_SHARED_BY_METHODS = frozenset({
    "update_current_user",
    "update_address",
    "update_product",
    "update_product_variant",
})


def operation_id(route: APIRoute) -> str:
    """The operationId: the handler's name (get_product), which FastAPI's
    default builds from the name and the whole path instead
    (get_product_api_v1_products__product_id__get). A handler that serves a
    PUT and a PATCH gets the method as a suffix. Unique by construction;
    tests/unit/test_openapi_operation_ids.py checks that it stays so."""
    if route.name in _SHARED_BY_METHODS:
        return f"{route.name}_{sorted(route.methods or ())[0].lower()}"
    return route.name


def install_problem_media_type(app: FastAPI) -> None:
    """Make the document say application/problem+json for every error
    response (status 400 and up): FastAPI puts a response's model under the
    route's own media type, application/json.

    FastAPI rebuilds its cached document only when a route is ADDED (the
    router's routes version, bumped by add_api_route, add_route,
    include_router and the like, is compared with the one the cache was built
    at; removing a route, or editing the `routes` list, does not change it).
    The conversion runs on every document handed out, not only on the first
    build, so a rebuilt document is converted too. It is idempotent: a
    converted response has no application/json left to move."""
    build = app.openapi

    def openapi() -> dict[str, Any]:
        schema = build()
        for path in schema["paths"].values():
            for operation in path.values():
                for code, response in operation.get("responses", {}).items():
                    # On every response, the 404 of an unknown path included
                    # (app/core/request_context.py): stated here, once.
                    response.setdefault("headers", {}).setdefault("X-Request-ID", REQUEST_ID_HEADER["X-Request-ID"])
                    content = response.get("content", {})
                    if code.isdigit() and int(code) >= 400 and "application/json" in content:
                        content[PROBLEM_MEDIA_TYPE] = content.pop("application/json")
        return schema

    app.openapi = openapi  # type: ignore[method-assign]  # FastAPI documents overriding the openapi method on the app instance
