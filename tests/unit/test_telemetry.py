"""OpenTelemetry tracing: off by default, on through OTEL_* variables, one trace
from the request through the database, Redis and Celery into the task, and
log trace_id equal to the span trace id."""

import json
import logging
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import fakeredis
import pytest
from celery import Celery
from celery import _state as _celery_state
from celery.signals import before_task_publish
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.core import request_context, telemetry
from app.core.logging_config import JSONFormatter
from app.core.request_context import RequestContextMiddleware

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
PARENT_ID = "b7ad6b7169203331"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_ID}-01"


# --- configuration -----------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, False),
        ({"OTEL_SERVICE_NAME": "x"}, False),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}, True),
        ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://collector:4318/v1/traces"}, True),
        ({"OTEL_SDK_DISABLED": "false"}, True),
        ({"OTEL_SDK_DISABLED": "FALSE"}, True),
        # the spec: "true" disables the SDK whatever else is set
        ({"OTEL_SDK_DISABLED": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318"}, False),
        ({"OTEL_TRACES_EXPORTER": "none", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318"}, False),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "  "}, False),
    ],
)
def test_is_enabled_follows_the_standard_variables(env, expected):
    assert telemetry.is_enabled(env) is expected


def test_off_by_default_instruments_nothing(monkeypatch):
    for name in list(os.environ):
        if name.startswith("OTEL_"):
            monkeypatch.delenv(name)
    app = FastAPI()
    assert telemetry.setup(app) is False
    assert "provider" not in telemetry._state
    assert request_context._trace_source is None


def test_importing_the_app_with_tracing_off_loads_no_instrumentation_or_trace_sdk():
    # FastAPI and redis-py import the OpenTelemetry API (and redis the metrics
    # SDK) themselves when it is installed; what this module must not load is
    # the trace SDK, the exporter and the instrumentations.
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    code = (
        "import sys, app.main\n"
        "prefixes = ('opentelemetry.instrumentation', 'opentelemetry.exporter', 'opentelemetry.sdk.trace')\n"
        "loaded = sorted(m for m in sys.modules if m.startswith(prefixes))\n"
        "assert not loaded, loaded\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]


def test_unsupported_protocol_keeps_tracing_off(monkeypatch, caplog):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    with caplog.at_level(logging.ERROR, logger="app.core.telemetry"):
        assert telemetry.setup(FastAPI()) is False
    assert "http/protobuf" in caplog.text
    assert "provider" not in telemetry._state


def test_missing_packages_log_an_error_and_run_untraced(monkeypatch, caplog):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")

    def boom(_exporter):
        raise ImportError("no opentelemetry")

    monkeypatch.setattr(telemetry, "_init", boom)
    with caplog.at_level(logging.ERROR, logger="app.core.telemetry"):
        assert telemetry.setup(FastAPI()) is False
    assert "uv sync --extra telemetry" in caplog.text
    assert request_context._trace_source is None


def test_service_name_defaults_to_the_project_name_and_yields_to_the_env(monkeypatch):
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
    assert telemetry._resource().attributes["service.name"] == telemetry.settings.PROJECT_NAME
    monkeypatch.setenv("OTEL_SERVICE_NAME", "shop-api")
    assert telemetry._resource().attributes["service.name"] == "shop-api"
    monkeypatch.delenv("OTEL_SERVICE_NAME")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.name=from-attrs")
    assert telemetry._resource().attributes["service.name"] == "from-attrs"


# --- tracing on, in-memory exporter -----------------------------------------


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.setFormatter(JSONFormatter())
        self.lines: list[dict] = []

    def emit(self, record):
        self.lines.append(json.loads(self.format(record)))


# One Celery app and one task for the whole module: a worker thread resolves the
# task through the process-wide default app, so apps made per test would mix up.
celery_app = Celery("otel_test", broker="memory://")
logger = logging.getLogger("otel_test")
task_ran = threading.Event()


@celery_app.task(name="otel_test.work")
def work():
    logger.info("in task")
    task_ran.set()
    return "ok"


@pytest.fixture
def traced():
    exporter = InMemorySpanExporter()
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async_engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    fake_redis = fakeredis.FakeRedis()
    capture = _Capture()
    logger.setLevel(logging.INFO)
    logger.addHandler(capture)
    published: list[dict] = []
    task_ran.clear()

    def grab(headers=None, **_kw):
        published.append(dict(headers or {}))

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/work")
    def do_work():
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        fake_redis.set("k", "v")
        logger.info("in request")
        work.delay()
        return {"ok": True}

    @app.get("/async-work")
    async def do_async_work():
        async with async_engine.connect() as conn:
            await conn.execute(text("select 1"))
        return {"ok": True}

    @app.get("/health")
    def health():
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        return {"ok": True}

    assert telemetry.setup(app, engines=[engine, async_engine.sync_engine], span_exporter=exporter)
    before_task_publish.connect(grab, weak=False)  # after OTel's receiver: sees its headers
    try:
        yield SimpleNamespace(
            client=TestClient(app),
            exporter=exporter,
            capture=capture,
            published=published,
        )
    finally:
        before_task_publish.disconnect(grab)
        logger.removeHandler(capture)
        telemetry.shutdown()



def _by_scope(spans, fragment):
    return [s for s in spans if fragment in s.instrumentation_scope.name]


def test_request_makes_a_server_span_with_database_and_redis_children(traced):
    response = traced.client.get("/work")
    assert response.status_code == 200
    spans = traced.exporter.get_finished_spans()
    server = [s for s in _by_scope(spans, "fastapi") if s.kind.name == "SERVER"]
    assert len(server) == 1
    assert server[0].attributes["http.route"] == "/work"
    assert server[0].resource.attributes["service.name"]
    trace_ids = {s.context.trace_id for s in spans}
    assert trace_ids == {server[0].context.trace_id}
    assert _by_scope(spans, "sqlalchemy")
    assert _by_scope(spans, "redis")


def test_async_engine_is_traced(traced):
    assert traced.client.get("/async-work").status_code == 200
    spans = traced.exporter.get_finished_spans()
    assert any("select" in str(s.attributes).lower() for s in _by_scope(spans, "sqlalchemy"))


def test_probes_are_excluded(traced):
    assert traced.client.get("/health").status_code == 200
    assert traced.exporter.get_finished_spans() == ()


def test_incoming_traceparent_continues_the_trace_and_logs_carry_its_trace_id(traced):
    traced.client.get("/work", headers={"traceparent": TRACEPARENT})
    spans = traced.exporter.get_finished_spans()
    server = next(s for s in _by_scope(spans, "fastapi") if s.kind.name == "SERVER")
    assert format(server.context.trace_id, "032x") == TRACE_ID
    assert format(server.parent.span_id, "016x") == PARENT_ID
    line = next(entry for entry in traced.capture.lines if entry["message"] == "in request")
    assert line["trace_id"] == TRACE_ID


def test_log_trace_id_is_the_span_trace_id_without_an_incoming_header(traced):
    traced.client.get("/work")
    spans = traced.exporter.get_finished_spans()
    server = next(s for s in _by_scope(spans, "fastapi") if s.kind.name == "SERVER")
    line = next(entry for entry in traced.capture.lines if entry["message"] == "in request")
    assert line["trace_id"] == format(server.context.trace_id, "032x")


def test_celery_task_published_in_the_request_continues_the_same_trace(traced):
    # A real (solo-pool, in-process thread) worker on the memory broker, so the
    # task runs in another thread with its own context, as in production.
    from celery.contrib.testing.worker import start_worker

    # Declare the queue first: the memory transport drops a message published
    # to an exchange nothing is bound to yet, and the worker thread may not
    # have bound its consumer when the request publishes.
    with celery_app.connection_for_write() as connection:
        queue = celery_app.amqp.queues[celery_app.conf.task_default_queue](connection.default_channel)
        queue.declare()
        queue.purge()  # messages published by the other tests
    # The solo pool runs the task in the worker thread, whose current_app is the
    # process-wide default app: make that this test's app, not the first one made.
    previous_default = _celery_state.default_app
    celery_app.set_default()
    try:
        with start_worker(
            celery_app, pool="solo", perform_ping_check=False, loglevel="ERROR"
        ):
            traced.client.get("/work", headers={"traceparent": TRACEPARENT})
            assert task_ran.wait(timeout=20)
    finally:
        _celery_state.set_default_app(previous_default)


def _assert_continued(traced):
    assert len(traced.published) == 1
    headers = traced.published[0]
    # OTel's own propagation, and B3's app_* headers agree with it.
    assert headers["traceparent"].split("-")[1] == TRACE_ID
    assert headers["app_trace_id"] == TRACE_ID

    spans = traced.exporter.get_finished_spans()
    run = next(s for s in _by_scope(spans, "celery") if s.kind.name == "CONSUMER")
    assert format(run.context.trace_id, "032x") == TRACE_ID
    line = next(entry for entry in traced.capture.lines if entry["message"] == "in task")
    assert line["trace_id"] == TRACE_ID


def test_shutdown_restores_the_untraced_state(traced):
    telemetry.shutdown()
    assert request_context._trace_source is None
    assert "provider" not in telemetry._state
