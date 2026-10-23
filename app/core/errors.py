"""Error responses that do not echo what the client sent.

FastAPI's default 422 handler puts each error's `input` (the submitted
value, or the whole body for a missing field) and its `ctx` in the
response: a refused registration returned the password in clear text, and
an oversized body came back whole. OWASP REST Security Cheat Sheet:
"Respond with generic error messages".

The handler below sends what the default sends - status 422,
{"detail": [...]}, every error's `type`, `loc`, `msg` and `url` - without
`input`, and with `ctx` cut down to the constraint values (the 8 of a
min_length, the pattern), never the exception object of a value_error or
anything else that may carry the input.
"""

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

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


async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder([_without_input(e) for e in exc.errors()])},
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
