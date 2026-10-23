from celery import Celery

from app.core import celery_context as cc
from app.core.request_context import (
    RequestContext,
    bind_request_context,
    get_request_context,
    reset_request_context,
)

app = Celery("ctx_test", broker="memory://")
seen = []


@app.task(bind=True, name="ctx_test.probe")
def probe(self):
    seen.append(get_request_context())
    if self.request.retries == 0:
        headers = {}
        cc.inject_context(headers=headers)  # what a retry publish does in the worker
        seen.append(headers)
    return "ok"


def _run(headers=None):
    seen.clear()
    probe.apply(headers=headers)
    return list(seen)


def test_inject_puts_ids_in_headers_inside_a_request():
    token = bind_request_context(RequestContext("rid", "a" * 32, "b" * 16))
    try:
        headers = {}
        cc.inject_context(headers=headers)
    finally:
        reset_request_context(token)
    assert headers == {
        cc.HEADER_REQUEST_ID: "rid",
        cc.HEADER_TRACE_ID: "a" * 32,
        cc.HEADER_PARENT_SPAN_ID: "b" * 16,
    }


def test_inject_without_trace_and_without_context():
    token = bind_request_context(RequestContext("rid"))
    try:
        headers = {}
        cc.inject_context(headers=headers)
    finally:
        reset_request_context(token)
    assert headers == {cc.HEADER_REQUEST_ID: "rid"}
    other = {}
    cc.inject_context(headers=other)
    assert other == {}
    cc.inject_context(headers=None)


def test_task_restores_ids_and_resets_afterwards():
    context, republished = _run(
        {
            cc.HEADER_REQUEST_ID: "rid",
            cc.HEADER_TRACE_ID: "a" * 32,
            cc.HEADER_PARENT_SPAN_ID: "b" * 16,
        }
    )
    assert context == RequestContext("rid", "a" * 32, "b" * 16)
    # a retry published from inside the task keeps the original ids
    assert republished[cc.HEADER_REQUEST_ID] == "rid"
    assert republished[cc.HEADER_TRACE_ID] == "a" * 32
    assert get_request_context() is None
    assert cc._tokens == {}


def test_beat_style_run_gets_fresh_request_id_and_no_trace():
    first, _ = _run()
    second, _ = _run()
    assert first.request_id and first.trace_id is None
    assert first.request_id != second.request_id
    assert get_request_context() is None


def test_eager_run_inside_a_request_keeps_the_current_context():
    token = bind_request_context(RequestContext("live"))
    try:
        context, _ = _run()
        assert context.request_id == "live"
        assert get_request_context().request_id == "live"
    finally:
        reset_request_context(token)


def test_register_connects_handlers():
    from celery.signals import before_task_publish, task_postrun, task_prerun

    assert before_task_publish.receivers and task_prerun.receivers and task_postrun.receivers
    cc.clear_context(task_id="unknown")


def test_header_read_from_request_attribute():
    from types import SimpleNamespace

    request = SimpleNamespace(**{cc.HEADER_REQUEST_ID: "attr"})
    assert cc._header(request, cc.HEADER_REQUEST_ID) == "attr"
    assert cc._header(None, cc.HEADER_REQUEST_ID) is None
