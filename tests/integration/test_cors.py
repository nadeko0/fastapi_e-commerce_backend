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
