"""X-Request-ID on the real application stack (404, 500, CORS exposure)."""

from fastapi.testclient import TestClient
from route_support import probe_routes

from app.main import app

ORIGIN = "http://localhost:3000"


def test_real_app_returns_a_request_id_on_200_and_404(client):
    assert len(client.get("/health").headers["X-Request-ID"]) == 32
    missing = client.get("/nope", headers={"X-Request-ID": "abc"})
    assert missing.status_code == 404
    assert missing.headers["X-Request-ID"] == "abc"


def test_real_app_500_carries_the_request_id():
    with probe_routes():
        @app.get("/_explode_rid")
        def explode():
            raise RuntimeError("boom")

        with TestClient(app, raise_server_exceptions=False) as lenient:
            response = lenient.get("/_explode_rid", headers={"X-Request-ID": "r500"})

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "r500"
    assert response.headers["Strict-Transport-Security"]


def test_request_id_is_readable_by_the_browser(client):
    response = client.get("/health", headers={"Origin": ORIGIN})

    exposed = response.headers["access-control-expose-headers"]
    assert "X-Request-ID" in exposed and "Retry-After" in exposed
