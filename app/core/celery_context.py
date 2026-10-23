"""Carry the request context from the publisher into the Celery worker.

Celery signals (https://docs.celeryq.dev/en/stable/userguide/signals.html):
- before_task_publish runs in the publishing process and may modify the
  message `headers`: the current request_id / trace_id / caller span id are
  added there. Task signatures and arguments are untouched.
- task_prerun / task_postrun run around the task body in the worker: the ids
  are restored into the same contextvar the JSON log formatter reads and
  reset afterwards, so nothing leaks into the next task. A task without ids
  (beat, or published outside a request) gets a fresh request_id and no
  trace_id.
- A retry is published by the worker while the restored context is active, so
  it carries the original ids.
"""

import uuid

from celery.signals import before_task_publish, task_postrun, task_prerun

from app.core.request_context import (
    RequestContext,
    bind_request_context,
    get_request_context,
    reset_request_context,
)

# Prefixed so they cannot collide with Celery's own protocol headers.
HEADER_REQUEST_ID = "app_request_id"
HEADER_TRACE_ID = "app_trace_id"
HEADER_PARENT_SPAN_ID = "app_parent_span_id"

# task_id -> token; prerun and postrun of one run happen in the same thread.
_tokens: dict[str, object] = {}


def inject_context(headers=None, **_kwargs) -> None:
    context = get_request_context()
    if context is None or headers is None:
        return
    headers[HEADER_REQUEST_ID] = context.request_id
    if context.trace_id:
        headers[HEADER_TRACE_ID] = context.trace_id
    if context.parent_span_id:
        headers[HEADER_PARENT_SPAN_ID] = context.parent_span_id


def _header(request, name: str) -> str | None:
    value = getattr(request, name, None)
    if value is None:
        value = (getattr(request, "headers", None) or {}).get(name)
    return value if isinstance(value, str) and value else None


def restore_context(task_id=None, task=None, **_kwargs) -> None:
    request = getattr(task, "request", None)
    request_id = _header(request, HEADER_REQUEST_ID)
    if request_id is None and get_request_context() is not None:
        # Eager run inside a request: the context is already current.
        return
    context = RequestContext(
        request_id=request_id or uuid.uuid4().hex,
        trace_id=_header(request, HEADER_TRACE_ID),
        parent_span_id=_header(request, HEADER_PARENT_SPAN_ID),
    )
    _tokens[task_id] = bind_request_context(context)


def clear_context(task_id=None, **_kwargs) -> None:
    token = _tokens.pop(task_id, None)
    if token is not None:
        reset_request_context(token)


def register() -> None:
    before_task_publish.connect(inject_context, weak=False)
    task_prerun.connect(restore_context, weak=False)
    task_postrun.connect(clear_context, weak=False)
