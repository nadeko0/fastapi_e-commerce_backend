"""Text the API refuses to take in: NUL characters and lone surrogates.

Two kinds of string are not text a database, a log line or a JWT library can
carry, and both used to reach them from a client's input:

- U+0000 (NUL). PostgreSQL's text and jsonb types cannot store it: psycopg
  raised DataError and the route answered 500. SQLite stores it, so only a
  PostgreSQL run shows the failure.
- A lone surrogate, U+D800..U+DFFF (what the JSON escape "\\ud800" decodes
  to in Python). It is not a Unicode scalar value (Unicode 16, section 3.9:
  surrogate code points are not assigned to characters and have no UTF-8
  form), so `.encode("utf-8")` raises UnicodeEncodeError: PyJWT did that on a
  refresh token (500), and a response or a log line holding one cannot be
  written. A valid surrogate PAIR in JSON ("\\ud83c\\udf75") is decoded by the
  JSON parser into one astral character and is not affected.

One rule, one answer: a 422 validation problem (app/core/errors.py) whose
error has the loc of the string and a fixed msg, never the string itself.
It is applied once per kind of input, not per field:

- a JSON request body: RequestBody (app/schemas/common.py), recursively, so
  nested models, lists and free-form objects (`characteristics`,
  `attributes`) are covered;
- query, path and form parameters: the `reject_unusable_text` dependency,
  declared once on the application (app/main.py): a route reads them as
  plain parameters, with no schema of its own to put a rule in;
- JSON inside a query parameter (`characteristics`): the same walk, called
  where that JSON is parsed.
"""

import re
from collections.abc import Iterable
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.params import Form

NUL_TYPE = "nul_character"
NUL_MESSAGE = "String must not contain NUL characters"
SURROGATE_TYPE = "invalid_unicode"
SURROGATE_MESSAGE = "String must be valid Unicode"

_NUL = re.compile("\x00")
_SURROGATE = re.compile("[\ud800-\udfff]")



def _problem(text: str) -> tuple[str, str] | None:
    """(error type, msg) when `text` is unusable, else None."""
    if _NUL.search(text):
        return NUL_TYPE, NUL_MESSAGE
    if _SURROGATE.search(text):
        return SURROGATE_TYPE, SURROGATE_MESSAGE
    return None


def first_unusable_text(value: Any, loc: tuple[str | int, ...] = ()) -> dict | None:
    """The first unusable string in `value` (a str, or any nesting of dicts,
    lists and tuples, member names included), as {"type", "loc", "msg"}, or
    None. `loc` is where `value` sits; the error's loc continues it with the
    member names and list positions down to the string. A bad member NAME is
    reported at the object that holds it: the name must not be echoed, and
    it is what the loc would carry.

    Iterative, in document order: a body nested a thousand levels deep must
    not run out of stack here, and the first problem is the one reported
    (one error, bounded work, however many strings are bad)."""
    pending = [(value, loc)]
    while pending:
        item, where = pending.pop()
        if isinstance(item, str):
            found = _problem(item)
            if found:
                return {"type": found[0], "loc": where, "msg": found[1]}
        elif isinstance(item, dict):
            for name in item:
                found = isinstance(name, str) and _problem(name)
                if found:
                    return {"type": found[0], "loc": where, "msg": found[1]}
            pending.extend((child, (*where, name)) for name, child in reversed(item.items()))
        elif isinstance(item, (list, tuple)):
            pending.extend((child, (*where, index)) for index, child in reversed(list(enumerate(item))))
    return None


def _by_name(items: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """Query or form pairs as {name: value}, a name sent more than once as
    {name: [values]}: the loc of a bad one then reads like FastAPI's own for a
    list parameter, ("query", "tag", 1)."""
    grouped: dict[str, list] = {}
    for name, value in items:
        grouped.setdefault(name, []).append(value)
    return {name: values[0] if len(values) == 1 else values for name, values in grouped.items()}


async def reject_unusable_text(request: Request) -> None:
    """Application-wide dependency (FastAPI(dependencies=[...])): the path,
    query and form parameters of a request must be usable text.

    Every parameter is checked, declared or not, because FastAPI gives no
    public list of the names a route reads (a query parameter model hides its
    fields behind the model's own name), and a value nothing reads is no
    reason to accept it. Raised as RequestValidationError, so the answer is
    the same 422 problem as any other bad parameter. It runs ahead of the
    route's own dependencies, so such a request is a 422 before it is a 401."""
    sources = [("path", request.path_params), ("query", _by_name(request.query_params.multi_items()))]
    route = request.scope.get("route")
    body_field = getattr(route, "body_field", None)
    if body_field is not None and isinstance(body_field.field_info, Form):
        # FastAPI has read the form already; this is the parsed copy.
        form = await request.form()
        sources.append(("body", _by_name(form.multi_items())))
    for location, values in sources:
        error = first_unusable_text(values, (location,))
        if error:
            raise RequestValidationError([error])
