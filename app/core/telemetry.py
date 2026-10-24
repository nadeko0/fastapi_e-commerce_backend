"""OpenTelemetry tracing, off unless the standard OTEL_* variables turn it on.

Configuration is the OpenTelemetry SDK's own environment variables
(https://opentelemetry.io/docs/specs/otel/configuration/sdk-environment-variables/);
nothing project-specific is added. Tracing is ON when, and only when,
- OTEL_SDK_DISABLED is not "true" (the spec: "true" disables the SDK, and it
  wins over everything below), and
- an exporter is configured: OTEL_EXPORTER_OTLP_ENDPOINT or
  OTEL_EXPORTER_OTLP_TRACES_ENDPOINT is set, or OTEL_SDK_DISABLED is
  explicitly "false" (then the OTLP exporter's own default,
  http://localhost:4318, applies), and
- OTEL_TRACES_EXPORTER is not "none".
Spans go out as OTLP over HTTP (protobuf). OTEL_EXPORTER_OTLP_PROTOCOL other
than http/protobuf is refused with an error log and tracing stays off, because
only that exporter is installed. Sampler, headers, timeouts, resource
attributes: read by the SDK from OTEL_TRACES_SAMPLER, OTEL_EXPORTER_OTLP_*,
OTEL_RESOURCE_ATTRIBUTES. service.name is OTEL_SERVICE_NAME, else
service.name in OTEL_RESOURCE_ATTRIBUTES, else the project name; set
OTEL_SERVICE_NAME per container to tell the API from the worker.

The packages live in the optional extra `telemetry`. When tracing is off, this
module imports no opentelemetry module at all (no cost, no failure if the
extra is not installed). When it is on but the extra is missing, an error is
logged and the application runs untraced.

What is instrumented: FastAPI (one SERVER span per request, /health, /livez
and /readyz excluded), the sync and async SQLAlchemy engines, redis-py,
Celery (producer in the API, consumer in the worker) and `requests` (the
Stripe SDK's HTTP client; httpx is not used by the application).

Trace context: the FastAPI instrumentation extracts an incoming W3C
`traceparent` with the default propagator, so the request continues the
caller's trace. The Celery instrumentation injects `traceparent` into the task
message headers and the worker continues it. The ids in our JSON logs and in
the app_* Celery headers (app/core/celery_context.py) then come from the
current span, so log trace_id == span trace id; with tracing off they keep
coming from the parsed header (app/core/request_context.py).
"""

import logging
import os
from collections.abc import Mapping, Sequence
from typing import Any

from app.core import request_context
from app.core.config import settings

logger = logging.getLogger(__name__)

_EXCLUDED_URLS = r"/health(\?.*)?$,/livez(\?.*)?$,/readyz(\?.*)?$"

_state: dict[str, Any] = {}


def is_enabled(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    disabled = env.get("OTEL_SDK_DISABLED", "").strip().lower()
    if disabled == "true":
        return False
    if env.get("OTEL_TRACES_EXPORTER", "").strip().lower() == "none":
        return False
    return bool(
        env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        or env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
        or disabled == "false"
    )


def _protocol_supported(env: Mapping[str, str]) -> bool:
    protocol = (
        env.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") or env.get("OTEL_EXPORTER_OTLP_PROTOCOL") or ""
    ).strip()
    return protocol in ("", "http/protobuf")


def _resource():
    from opentelemetry.sdk.resources import Resource

    env = os.environ
    named = env.get("OTEL_SERVICE_NAME") or "service.name=" in env.get("OTEL_RESOURCE_ATTRIBUTES", "")
    return Resource.create({} if named else {"service.name": settings.PROJECT_NAME})


def _no_orphan_client_spans(sampler):
    """Wrap `sampler` so a CLIENT span (database, Redis, outbound HTTP) with no
    parent is dropped. The probes are excluded from tracing, but they query
    the database and Redis: without this each probe would leave unconnected
    one-span traces behind."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult

    class NoOrphanClientSpans(Sampler):
        def should_sample(self, parent_context, trace_id, name, kind=None, *args, **kwargs):
            parent = trace.get_current_span(parent_context).get_span_context()
            if kind == trace.SpanKind.CLIENT and not parent.is_valid:
                return SamplingResult(Decision.DROP)
            return sampler.should_sample(parent_context, trace_id, name, kind, *args, **kwargs)

        def get_description(self):
            return f"NoOrphanClientSpans({sampler.get_description()})"

    return NoOrphanClientSpans()


def _span_context_ids() -> tuple[str, str] | None:
    from opentelemetry import trace

    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return format(span_context.trace_id, "032x"), format(span_context.span_id, "016x")


def setup(app=None, engines: Sequence = (), *, span_exporter=None) -> bool:
    """Turn tracing on for this process when configured; True when it is on.

    `app`: the FastAPI app to instrument (API process). `engines`: sync
    SQLAlchemy engines (for an async engine pass `.sync_engine`). The provider
    and the process-wide instrumentations are created once; a later call only
    adds its app / engines. `span_exporter` replaces the OTLP exporter (tests)
    and bypasses the environment check.
    """
    if span_exporter is None:
        env = os.environ
        if not is_enabled(env):
            return False
        if not _protocol_supported(env):
            logger.error(
                "OpenTelemetry tracing stays off: only OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf is supported"
            )
            return False
    try:
        _init(span_exporter)
        _instrument(app, engines)
    except ImportError:
        logger.error(
            "OpenTelemetry tracing is configured but its packages are not installed; "
            "install the extra: uv sync --extra telemetry. Running without tracing."
        )
        _state.clear()
        request_context.set_trace_source(None)
        return False
    return True


def _init(span_exporter) -> None:
    if "provider" in _state:
        return
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    # A provider built only to read the sampler OTEL_TRACES_SAMPLER selects.
    sampler = _no_orphan_client_spans(TracerProvider().sampler)
    provider = TracerProvider(resource=_resource(), sampler=sampler)
    if span_exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        # Global only for the real exporter: set_tracer_provider works once per process.
        trace.set_tracer_provider(provider)
    _state["provider"] = provider
    _state["apps"] = []
    request_context.set_trace_source(_span_context_ids)
    logger.info("OpenTelemetry tracing enabled")


def _instrument(app, engines) -> None:
    from opentelemetry.instrumentation.celery import CeleryInstrumentor
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    provider = _state["provider"]
    if "global" not in _state:
        RedisInstrumentor().instrument(tracer_provider=provider)
        CeleryInstrumentor().instrument(tracer_provider=provider)
        RequestsInstrumentor().instrument(tracer_provider=provider)
        _state["global"] = True
    if engines:
        SQLAlchemyInstrumentor().instrument(
            tracer_provider=provider, engines=list(engines), skip_dep_check=True
        )
    if app is not None and app not in _state["apps"]:
        extra = os.environ.get("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "").strip()
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=provider,
            excluded_urls=f"{_EXCLUDED_URLS},{extra}" if extra else _EXCLUDED_URLS,
        )
        _state["apps"].append(app)


def shutdown() -> None:
    """Flush pending spans and undo the instrumentation (shutdown, tests)."""
    provider = _state.pop("provider", None)
    if provider is None:
        return
    apps = _state.pop("apps", [])
    was_global = _state.pop("global", False)
    request_context.set_trace_source(None)
    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        for app in apps:
            FastAPIInstrumentor.uninstrument_app(app)
        if was_global:
            RedisInstrumentor().uninstrument()
            CeleryInstrumentor().uninstrument()
            RequestsInstrumentor().uninstrument()
        SQLAlchemyInstrumentor().uninstrument()
    finally:
        provider.shutdown()
