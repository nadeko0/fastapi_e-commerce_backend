import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

PJ = "application/problem+json"


def ct(r):
    return r.headers.get("content-type", "")


def test_matrix(client, monkeypatch):
    rows = {}
    rows["404 unknown"] = client.get("/api/v1/nope")
    rows["405"] = client.delete("/api/v1/products")
    rows["HEAD 404"] = client.head("/api/v1/nope")
    rows["422 validation"] = client.post("/api/v1/users/register", json={"email": "x"})
    rows["400 bad json"] = client.post("/api/v1/users/register", content=b"{", headers={"content-type": "application/json"})
    rows["415"] = client.post("/api/v1/users/register", content=b"a=b", headers={"content-type": "text/plain"})
    rows["401 no token"] = client.get("/api/v1/users/me")
    rows["401 bad token"] = client.get("/api/v1/users/me", headers={"Authorization": "Bearer x"})
    rows["413 declared"] = client.post("/api/v1/users/register", content=b"x" * (settings.MAX_REQUEST_BODY_BYTES + 1), headers={"content-type": "application/json"})

    def gen():
        for _ in range(3):
            yield b"x" * (settings.MAX_REQUEST_BODY_BYTES // 2 + 1)
    rows["413 chunked"] = client.post("/api/v1/users/register", content=gen(), headers={"content-type": "application/json"})
    rows["CORS preflight bad origin"] = client.options("/api/v1/products", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    rows["CORS preflight bad header"] = client.options("/api/v1/products", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "X-Evil"})
    rows["OPTIONS no preflight"] = client.options("/api/v1/products")
    for k, r in rows.items():
        print(f"ROW|{k}|{r.status_code}|{ct(r)}|{r.text[:80]!r}")


def test_unhandled_500(monkeypatch):
    import app.api.v1.legal as legal
    monkeypatch.setattr(legal, "PRIVACY_POLICY", None)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/api/v1/legal/privacy-policy")
        print(f"ROW|500 unhandled|{r.status_code}|{ct(r)}|{r.text[:120]!r}|hsts={r.headers.get('strict-transport-security')}")
