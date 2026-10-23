"""CORS as a browser sees it: the Origin header carries scheme://host[:port]
with no path (Fetch standard, "serializing a request origin"), and Starlette's
CORSMiddleware compares it to allow_origins as an exact string. conftest sets
BACKEND_CORS_ORIGINS to http://localhost:3000."""

ORIGIN = "http://localhost:3000"


def test_preflight_from_the_configured_origin_is_allowed(client):
    response = client.options(
        "/api/v1/products",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN


def test_simple_request_from_the_configured_origin_gets_the_header(client):
    response = client.get("/health", headers={"Origin": ORIGIN})

    assert response.headers["access-control-allow-origin"] == ORIGIN


def test_other_origins_get_no_header(client):
    simple = client.get("/health", headers={"Origin": "http://evil.example"})
    preflight = client.options(
        "/api/v1/products",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )

    assert "access-control-allow-origin" not in simple.headers
    assert preflight.status_code == 400
    assert "access-control-allow-origin" not in preflight.headers


# --- explicit method and header lists (Starlette CORSMiddleware) -------------

def _preflight(client, method, headers):
    return client.options(
        "/api/v1/products",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )


def test_preflight_allows_the_methods_and_headers_the_api_uses(client):
    for method in ("GET", "POST", "PUT", "DELETE"):
        response = _preflight(client, method, "Authorization, Content-Type, Idempotency-Key")

        assert response.status_code == 200, method
        assert response.headers["access-control-allow-credentials"] == "true"
        assert response.headers["access-control-allow-origin"] == ORIGIN


def test_preflight_lists_are_explicit_not_reflected(client):
    response = _preflight(client, "GET", "Authorization")

    assert response.headers["access-control-allow-methods"] == "GET, POST, PUT, DELETE"
    allowed = {h.strip() for h in response.headers["access-control-allow-headers"].split(",")}
    assert {"Authorization", "Idempotency-Key", "Content-Type"} <= allowed
    assert "*" not in allowed


def test_preflight_for_a_header_the_api_does_not_read_is_refused(client):
    response = _preflight(client, "GET", "X-Foo")

    assert response.status_code == 400
    assert "Disallowed CORS headers" in response.text


def test_preflight_for_a_method_the_api_does_not_serve_is_refused(client):
    for method in ("PATCH", "QUERY"):
        response = _preflight(client, method, "Authorization")

        assert response.status_code == 400, method
        assert "Disallowed CORS method" in response.text


def test_retry_after_is_readable_by_the_browser(client):
    response = client.get("/health", headers={"Origin": ORIGIN})

    assert response.headers["access-control-expose-headers"] == "Retry-After, X-Request-ID, RateLimit, RateLimit-Policy, ETag"
