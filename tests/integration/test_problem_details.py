"""Every error of the API is an RFC 9457 problem details object.

One test per source of error: an HTTPException of a route, Starlette's own
404 and 405, request validation, a malformed or wrongly typed body, the body
size limit, the rate limit, the in-flight limit and an unhandled exception.
Each answers application/problem+json with `instance` (the path),
`request_id` (the X-Request-ID header) and, where it applies, `errors` or
`retry_after`. The design is in app/core/errors.py.
"""

import json

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.core.config import settings
from app.core.errors import Problem, ProblemType
from app.core.overload import InFlightLimitMiddleware
from app.core.request_context import RequestContextMiddleware
from app.core.security_headers import SecurityHeadersMiddleware
from app.main import app

API = settings.API_V1_STR
PROBLEM_JSON = "application/problem+json"
BASE = "https://github.com/nadeko0/fastapi_e-commerce_backend/problems/"
SECRET = "Sup3rSecretValue"


def assert_problem(response, status, *, type="about:blank", title=None, path=None):
    """The members every problem has; returns the body for more checks."""
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM_JSON
    body = response.json()
    assert body["type"] == type
    assert body["status"] == status
    assert body["title"] == (title or _phrase(status))
    assert isinstance(body["request_id"], str)
    assert body["request_id"] == response.headers["X-Request-ID"]
    if path is not None:
        assert body["instance"] == path
    return body


def _phrase(status):
    from http import HTTPStatus

    return HTTPStatus(status).phrase


def test_an_http_exception_of_a_route_is_a_blank_problem_with_its_detail(client):
    response = client.get(f"{API}/products/99999999")

    body = assert_problem(response, 404, path=f"{API}/products/99999999")
    assert body["detail"] == "Product not found"
    assert set(body) == {"type", "title", "status", "detail", "instance", "request_id"}


def test_the_request_id_sent_by_the_client_is_the_one_in_the_problem(client):
    response = client.get(f"{API}/products/99999999", headers={"X-Request-ID": "abc-123"})

    assert response.headers["X-Request-ID"] == "abc-123"
    assert response.json()["request_id"] == "abc-123"


def test_the_instance_has_no_query_string(client):
    response = client.get(f"{API}/products/99999999?x=1&token=secret")

    assert response.json()["instance"] == f"{API}/products/99999999"


def test_starlettes_own_404_for_an_unknown_path_is_a_problem(client):
    response = client.get(f"{API}/no/such/path")

    body = assert_problem(response, 404, path=f"{API}/no/such/path")
    assert body["detail"] == "Not Found"


def test_405_is_a_problem_and_keeps_allow(client):
    response = client.delete(f"{API}/products")

    assert_problem(response, 405, path=f"{API}/products")
    assert "GET" in response.headers["Allow"]


def test_401_is_a_problem_and_keeps_the_challenge(client):
    response = client.get(f"{API}/users/me")

    body = assert_problem(response, 401, path=f"{API}/users/me")
    assert body["detail"]
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.headers["Cache-Control"] == "no-store"


def test_a_validation_error_lists_errors_and_never_the_input(client):
    response = client.post(
        f"{API}/users/register",
        json={"email": "jane.doe@example.com", "password": SECRET},
    )

    body = assert_problem(
        response, 422, type=BASE + "validation-error", title="Request validation failed",
        path=f"{API}/users/register",
    )
    assert "detail" in body
    assert {"type", "loc", "msg"} <= set(body["errors"][0])
    assert any(error["loc"][:2] == ["body", "gdpr_consent"] for error in body["errors"])
    assert "input" not in json.dumps(body)
    assert SECRET not in response.text


def test_a_validation_error_in_the_query_is_the_same_problem(client):
    response = client.get(f"{API}/products/search?query=ab")

    body = assert_problem(
        response, 422, type=BASE + "validation-error", title="Request validation failed",
    )
    assert body["errors"][0]["loc"] == ["query", "query"]


def test_malformed_json_is_400_malformed_request(client):
    response = client.post(
        f"{API}/users/register",
        content='{"password": "' + SECRET + '", ',
        headers={"Content-Type": "application/json"},
    )

    body = assert_problem(
        response, 400, type=BASE + "malformed-request", title="Malformed request",
        path=f"{API}/users/register",
    )
    assert "errors" not in body
    assert SECRET not in response.text


def test_a_body_that_is_not_utf8_is_400_malformed_request(client):
    response = client.post(
        f"{API}/users/register", content=b'{"a": "\xff\xfe"}',
        headers={"Content-Type": "application/json"},
    )

    assert_problem(
        response, 400, type=BASE + "malformed-request", title="Malformed request",
    )


def test_a_json_body_with_another_content_type_is_415_with_accept(client):
    response = client.post(
        f"{API}/users/register", content='{"email": "a@example.com"}',
        headers={"Content-Type": "text/plain"},
    )

    body = assert_problem(
        response, 415, type=BASE + "unsupported-media-type", title="Unsupported media type",
        path=f"{API}/users/register",
    )
    assert body["detail"]
    assert "errors" not in body
    assert response.headers["Accept"] == "application/json"


def test_a_json_body_without_a_content_type_is_415(client):
    response = client.post(
        f"{API}/users/register", content='{"email": "a@example.com"}',
        headers={"Content-Type": ""},
    )

    assert response.status_code == 415
    assert response.json()["type"] == BASE + "unsupported-media-type"


def test_an_empty_body_is_still_a_validation_error(client):
    response = client.post(f"{API}/users/register")

    assert response.status_code == 422
    assert response.json()["type"] == BASE + "validation-error"


def test_413_is_a_problem_with_its_type(client):
    body_bytes = b'{"pad":"' + b"x" * settings.MAX_REQUEST_BODY_BYTES + b'"}'

    response = client.post(
        f"{API}/users/register", content=body_bytes,
        headers={"Content-Type": "application/json"},
    )

    body = assert_problem(
        response, 413, type=BASE + "body-too-large", title="Request body too large",
        path=f"{API}/users/register",
    )
    assert body["detail"] == "Request body is too large"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.fixture
def limited(monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_DEFAULT", "1000000/minute")
    monkeypatch.setattr(settings, "RATE_LIMIT_AUTH", "1/minute")


def test_429_has_the_type_retry_after_in_the_body_and_the_header(client, limited):
    login = {"username": "a@example.com", "password": "wrong-password"}
    client.post(f"{API}/users/login", data=login)
    response = client.post(f"{API}/users/login", data=login)

    body = assert_problem(
        response, 429, type=BASE + "rate-limited", title="Too many requests",
        path=f"{API}/users/login",
    )
    assert body["detail"] == "Rate limit exceeded"
    assert isinstance(body["retry_after"], int)
    assert body["retry_after"] == int(response.headers["Retry-After"])


def test_503_of_the_in_flight_limit_has_the_type_and_retry_after():
    async def index(request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/", index)])
    stack = SecurityHeadersMiddleware(
        RequestContextMiddleware(InFlightLimitMiddleware(inner, limit=0))
    )
    with TestClient(stack) as probe:
        response = probe.get("/")

    body = assert_problem(
        response, 503, type=BASE + "service-overloaded", title="Service overloaded", path="/",
    )
    assert body["retry_after"] == 1
    assert response.headers["Retry-After"] == "1"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.fixture
def exploding_route():
    @app.get("/_explode_problem")
    def explode():
        raise RuntimeError(SECRET)

    try:
        yield "/_explode_problem"
    finally:
        app.router.routes.pop()


def test_500_for_an_unhandled_exception_leaks_nothing(client, exploding_route):
    with TestClient(app, raise_server_exceptions=False) as lenient:
        response = lenient.get(exploding_route)

    body = assert_problem(response, 500, path=exploding_route)
    assert set(body) == {"type", "title", "status", "instance", "request_id"}
    assert SECRET not in response.text
    assert "RuntimeError" not in response.text
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.fixture
def typed_route():
    @app.get("/_typed_problem")
    def typed():
        raise Problem(
            409, type=ProblemType.IDEMPOTENCY_KEY_IN_USE, detail="Key is in use",
            headers={"X-Extra": "kept"},
        )

    try:
        yield "/_typed_problem"
    finally:
        app.router.routes.pop()


def test_a_typed_problem_reaches_the_handler_and_keeps_its_headers(client, typed_route):
    response = client.get(typed_route)

    body = assert_problem(
        response, 409, type=BASE + "idempotency-key-in-use", title="Idempotency key in use",
        path=typed_route,
    )
    assert body["detail"] == "Key is in use"
    assert response.headers["X-Extra"] == "kept"


def test_a_problem_is_an_http_exception():
    assert issubclass(Problem, Exception)
    from fastapi import HTTPException

    assert issubclass(Problem, HTTPException)


def test_every_problem_type_is_a_stable_uri_with_a_title():
    from app.core.errors import PROBLEM_TITLES

    assert {t.value.removeprefix(BASE) for t in ProblemType} == {
        "validation-error", "malformed-request", "unsupported-media-type", "body-too-large",
        "rate-limited", "service-overloaded", "idempotency-key-reused",
        "idempotency-key-in-use", "payment-declined", "insufficient-stock",
    }
    assert set(PROBLEM_TITLES) == set(ProblemType)
