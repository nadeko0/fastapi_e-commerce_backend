"""Request id (X-Request-ID) and W3C Trace Context (traceparent) handling:
app/core/request_context.py. Spec: https://www.w3.org/TR/trace-context/
section 3.2 (traceparent header) and 3.2.2 (processing model)."""

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.testclient import TestClient

from app.core.body_limit import BodySizeLimitMiddleware
from app.core.logging_config import JSONFormatter
from app.core.overload import InFlightLimitMiddleware
from app.core.request_context import (
    RequestContextMiddleware,
    get_request_context,
    parse_traceparent,
)

# Example from the spec, section 3.2.
TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SPAN_ID = "b7ad6b7169203331"
VALID = f"00-{TRACE_ID}-{SPAN_ID}-01"

logger = logging.getLogger("app.test_request_context")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(json.loads(JSONFormatter().format(record)))


@pytest.fixture
def captured():
    handler = _Capture()
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    yield handler.lines
    logger.removeHandler(handler)
    logger.setLevel(previous)


def _build():
    app = FastAPI()

    @app.get("/ctx")
    async def ctx():
        context = get_request_context()
        logger.info("in handler")
        return {
            "request_id": context.request_id,
            "trace_id": context.trace_id,
            "parent_span_id": context.parent_span_id,
        }

    @app.get("/thread")
    async def thread():
        await run_in_threadpool(lambda: logger.info("in thread"))
        return {}

    @app.get("/sync")
    def sync():
        logger.info("in sync route")
        return {}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    @app.post("/echo")
    async def echo(payload: dict):
        return payload

    inflight = InFlightLimitMiddleware(BodySizeLimitMiddleware(app, limit=10), limit=1)
    return inflight, RequestContextMiddleware(inflight)


@pytest.fixture
def stack():
    return _build()


@pytest.fixture
def client(stack):
    return TestClient(stack[1], raise_server_exceptions=False)


def test_request_id_is_generated_and_returned(client):
    response = client.get("/ctx")

    rid = response.headers["X-Request-ID"]
    assert len(rid) == 32 and int(rid, 16) >= 0
    assert response.json()["request_id"] == rid
    assert client.get("/ctx").headers["X-Request-ID"] != rid


@pytest.mark.parametrize("value", ["abc", "order-123_A.b:c", "a" * 64])
def test_valid_incoming_request_id_is_echoed(client, value):
    response = client.get("/ctx", headers={"X-Request-ID": value})

    assert response.headers["X-Request-ID"] == value
    assert response.json()["request_id"] == value


@pytest.mark.parametrize(
    "value", ["a" * 65, "has space", "semi;colon", "new\tline", "<script>", b"caf\xc3\xa9", ""]
)
def test_invalid_incoming_request_id_is_replaced(client, value):
    response = client.get("/ctx", headers={"X-Request-ID": value})

    rid = response.headers["X-Request-ID"]
    assert rid != value
    assert len(rid) == 32 and rid.isalnum()


def test_repeated_request_id_headers_are_ignored(client):
    response = client.get("/ctx", headers=[("X-Request-ID", "one"), ("X-Request-ID", "two")])

    assert response.headers["X-Request-ID"] not in ("one", "two")


@pytest.mark.parametrize(
    "header",
    [
        VALID,
        f"00-{TRACE_ID}-{SPAN_ID}-00",
        # Higher version: known fields, optionally followed by "-" and more data.
        f"01-{TRACE_ID}-{SPAN_ID}-01-extra",
        f"01-{TRACE_ID}-{SPAN_ID}-01",
    ],
)
def test_valid_traceparent_is_parsed(client, header):
    response = client.get("/ctx", headers={"traceparent": header})

    body = response.json()
    assert body["trace_id"] == TRACE_ID
    assert body["parent_span_id"] == SPAN_ID
    assert "traceparent" not in response.headers
    assert "traceresponse" not in response.headers


@pytest.mark.parametrize(
    "header",
    [
        "",
        "garbage",
        f"ff-{TRACE_ID}-{SPAN_ID}-01",  # version ff is invalid
        f"00-{'0' * 32}-{SPAN_ID}-01",  # all-zero trace-id
        f"00-{TRACE_ID}-{'0' * 16}-01",  # all-zero parent-id
        f"00-{TRACE_ID.upper()}-{SPAN_ID}-01",  # uppercase hex
        f"00-{TRACE_ID[:-1]}-{SPAN_ID}-01",  # short trace-id
        f"00-{TRACE_ID}-{SPAN_ID[:-1]}-01",  # short parent-id
        f"00-{TRACE_ID}-{SPAN_ID}-1",  # short flags
        f"00-{TRACE_ID}-{SPAN_ID}-0g",  # not hex
        f"00-{TRACE_ID}-{SPAN_ID}-01-extra",  # version 00 is exactly 55 chars
        f"01-{TRACE_ID}-{SPAN_ID}-01extra",  # extra data needs a dash
        f"0-{TRACE_ID}-{SPAN_ID}-01",
    ],
)
def test_invalid_traceparent_is_ignored(client, header):
    response = client.get("/ctx", headers={"traceparent": header})

    assert response.status_code == 200
    assert response.json()["trace_id"] is None
    assert response.json()["parent_span_id"] is None
    assert response.headers["X-Request-ID"]


def test_repeated_traceparent_headers_are_ignored(client):
    response = client.get("/ctx", headers=[("traceparent", VALID), ("traceparent", VALID)])

    assert response.json()["trace_id"] is None


def test_parse_traceparent():
    assert parse_traceparent("nope") is None
    assert parse_traceparent(VALID) == (TRACE_ID, SPAN_ID)


def test_log_records_carry_request_and_trace_ids(client, captured):
    response = client.get("/ctx", headers={"X-Request-ID": "req-1", "traceparent": VALID})

    line = next(r for r in captured if r["message"] == "in handler")
    assert line["request_id"] == "req-1" == response.headers["X-Request-ID"]
    assert line["trace_id"] == TRACE_ID


def test_log_records_without_trace_have_no_trace_id(client, captured):
    client.get("/ctx")

    line = next(r for r in captured if r["message"] == "in handler")
    assert "request_id" in line
    assert "trace_id" not in line


@pytest.mark.parametrize("path, message", [("/thread", "in thread"), ("/sync", "in sync route")])
def test_log_records_from_worker_threads_carry_the_ids(client, captured, path, message):
    client.get(path, headers={"X-Request-ID": "req-t", "traceparent": VALID})

    line = next(r for r in captured if r["message"] == message)
    assert line["request_id"] == "req-t"
    assert line["trace_id"] == TRACE_ID


def test_records_outside_a_request_have_no_ids(captured):
    logger.info("startup")

    assert "request_id" not in captured[0]
    assert "trace_id" not in captured[0]


def test_context_does_not_leak_between_requests(client, captured):
    client.get("/ctx", headers={"X-Request-ID": "first", "traceparent": VALID})
    client.get("/ctx")

    lines = [r for r in captured if r["message"] == "in handler"]
    assert lines[1]["request_id"] != "first"
    assert "trace_id" not in lines[1]
    assert get_request_context() is None


def test_every_kind_of_response_carries_the_request_id(client):
    json_type = {"content-type": "application/json"}
    cases = {
        200: client.get("/ctx", headers={"X-Request-ID": "r200"}),
        404: client.get("/missing", headers={"X-Request-ID": "r404"}),
        405: client.post("/ctx", headers={"X-Request-ID": "r405"}),
        422: client.post("/echo", content=b"[]", headers={"X-Request-ID": "r422", **json_type}),
        413: client.post("/echo", content=b"x" * 100, headers={"X-Request-ID": "r413"}),
        500: client.get("/boom", headers={"X-Request-ID": "r500"}),
    }
    for status, response in cases.items():
        assert response.status_code == status
        assert response.headers["X-Request-ID"] == f"r{status}"


def test_shed_503_carries_the_request_id(stack, client):
    stack[0].in_flight = 1

    response = client.get("/ctx", headers={"X-Request-ID": "r503"})

    assert response.status_code == 503
    assert response.headers["X-Request-ID"] == "r503"
