"""The exact values of the response security headers (OWASP Secure Headers
Project, OWASP REST Security Cheat Sheet), on a JSON response, a 404, a 500
and the interactive docs pages."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

API_HEADERS = {
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
    "referrer-policy": "no-referrer",
    "cross-origin-resource-policy": "same-origin",
    "cross-origin-opener-policy": "same-origin",
    "permissions-policy": "geolocation=(), microphone=()",
}
DOCS_CSP = (
    "default-src 'self';"
    "img-src 'self' data: https:;"
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;"
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;"
    "font-src 'self' https://cdn.jsdelivr.net;"
)


def _assert_headers(response, expected):
    for name, value in expected.items():
        assert response.headers[name] == value, name
    assert "x-xss-protection" not in response.headers


def test_json_response_carries_the_exact_headers(client):
    response = client.get("/health")

    assert response.status_code == 200
    _assert_headers(response, API_HEADERS)


def test_404_carries_the_exact_headers(client):
    response = client.get("/api/v1/nothing-here")

    assert response.status_code == 404
    _assert_headers(response, API_HEADERS)


def test_500_carries_the_exact_headers():
    @app.get("/_explode_values")
    def explode():
        raise RuntimeError("boom")

    try:
        with TestClient(app, raise_server_exceptions=False) as lenient:
            response = lenient.get("/_explode_values")
    finally:
        app.router.routes.pop()

    assert response.status_code == 500
    _assert_headers(response, API_HEADERS)


def test_openapi_json_keeps_the_strict_policy(client):
    response = client.get("/api/v1/openapi.json")

    assert response.status_code == 200
    _assert_headers(response, API_HEADERS)


@pytest.mark.parametrize("path", ["/api/v1/docs", "/redoc", "/docs/oauth2-redirect"])
def test_docs_pages_keep_the_cdn_policy(client, path):
    response = client.get(path)

    assert response.status_code == 200
    _assert_headers(response, {**API_HEADERS, "content-security-policy": DOCS_CSP})
