"""One JSON access line per request, written by the application
(app/core/access_log.py) through its JSON formatter."""

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.logging_config import JSONFormatter
from app.main import app

API = settings.API_V1_STR


@pytest.fixture
def access_lines(caplog):
    caplog.set_level(logging.INFO, logger="app.access")

    def lines():
        return [record for record in caplog.records if record.name == "app.access"]

    return lines


def test_a_request_writes_one_access_record(client, access_lines):
    response = client.get(f"{API}/products/999999?token=secret-value")

    (record,) = access_lines()
    assert response.status_code == 404
    assert record.method == "GET"
    assert record.path == f"{API}/products/999999"  # no query string
    assert record.route == "/products/{product_id}"
    assert record.status == 404
    assert record.duration_ms >= 0
    assert record.client_ip == "testclient"
    assert "secret-value" not in record.getMessage() + str(record.__dict__)


def test_the_line_is_json_with_the_request_id(client):
    seen = []

    class Capture(logging.Handler):
        def emit(self, record):
            # Formatted while the request is handled, as the stdout handler
            # does: the formatter reads the request id from its context.
            seen.append(JSONFormatter().format(record))

    handler = Capture()
    access = logging.getLogger("app.access")
    access.addHandler(handler)
    try:
        client.get("/livez", headers={"X-Request-ID": "req-json-1"})
    finally:
        access.removeHandler(handler)

    (line,) = seen
    payload = json.loads(line)
    assert payload["logger"] == "app.access"
    assert payload["request_id"] == "req-json-1"
    assert payload["message"] == "GET /livez 200"
    assert payload["extra"]["status"] == 200
    assert set(payload["extra"]) == {"method", "path", "route", "status", "duration_ms", "client_ip"}


def test_an_unmatched_path_is_logged_as_requested_without_its_query(client, access_lines):
    client.get("/no/such/path?password=hunter2")

    (record,) = access_lines()
    assert record.path == "/no/such/path"
    assert record.route is None
    assert record.status == 404
    assert "hunter2" not in str(record.__dict__)


def test_head_is_logged_as_head(client, access_lines):
    client.head(f"{API}/categories")

    (record,) = access_lines()
    assert record.method == "HEAD"
    assert record.status == 200


def test_the_client_is_the_one_resolved_behind_a_trusted_proxy(client, access_lines, monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", ["10.0.0.0/8"])
    proxy = TestClient(app, client=("10.0.0.5", 50000))

    proxy.get(f"{API}/categories", headers={"X-Forwarded-For": "198.51.100.7"})

    (record,) = access_lines()
    assert record.client_ip == "198.51.100.7"


def test_an_unhandled_error_is_logged_as_500(access_lines, monkeypatch):
    import app.core.probes as probes

    def boom(request, content, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(probes, "probe_response", boom)
    with TestClient(app, raise_server_exceptions=False) as failing:
        assert failing.get("/livez").status_code == 500

    (record,) = access_lines()
    assert record.status == 500
