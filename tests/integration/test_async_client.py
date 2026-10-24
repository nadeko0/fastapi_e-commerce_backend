"""The app under a real event loop, the way FastAPI's "Async Tests" page does it.

The rest of the suite uses TestClient, which runs the app on a portal thread
and is meant for plain `def` tests. FastAPI's documentation for `async def`
tests is different: mark the test for the anyio pytest plugin
(@pytest.mark.anyio), and talk to the app with an HTTP client over
ASGITransport, awaited on the test's own loop. These tests do that for a
public GET, an authenticated GET and an error answered as problem+json, to
show the app works without TestClient's thread portal.

The HTTP client is httpx2 (the package this project depends on; the docs'
`httpx` example is the same API). Lifespan: ASGITransport does not run the
app's lifespan (the docs' warning; they point at asgi-lifespan's
LifespanManager). The lifespan here has nothing to do at startup, and its
shutdown work (disposing the async engine, closing the async Redis client) is
not needed by a test: the suite's async engine keeps no pool and the Redis
client is a per-test fake. So no lifespan is driven; a route that needed
startup state would have to run it explicitly.

Sources: https://fastapi.tiangolo.com/advanced/async-tests/ and
https://anyio.readthedocs.io/en/stable/testing.html
"""
import httpx2
import pytest

from app.core.clock import utc_now
from app.core.security import create_access_token, get_password_hash
from app.main import app
from app.models.user import User, UserRole

pytestmark = pytest.mark.anyio

API = "/api/v1"


@pytest.fixture
def anyio_backend():
    # The plugin's default fixture would also offer trio; the app is asyncio.
    return "asyncio"


@pytest.fixture
async def async_client():
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


def _login_header(db_session) -> dict:
    user = User(
        email="async.user@example.com",
        hashed_password=get_password_hash("Str0ngPass"),
        full_name="Async User",
        gdpr_consent=True,
        privacy_policy_accepted=True,
        is_active=True,
        is_email_verified=True,
        role=UserRole.CLIENT,
        created_at=utc_now(),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


async def test_a_public_get_answers_on_the_test_loop(async_client):
    response = await async_client.get(f"{API}/categories")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json()["items"] == []


async def test_an_authenticated_get_reads_the_user_from_the_token(async_client, db_session):
    headers = _login_header(db_session)

    response = await async_client.get(f"{API}/users/me", headers=headers)

    assert response.status_code == 200
    assert response.json()["email"] == "async.user@example.com"
    # The same route without the header is refused.
    assert (await async_client.get(f"{API}/users/me")).status_code == 401


async def test_an_error_is_problem_json(async_client):
    response = await async_client.get(f"{API}/products/999999")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["status"] == 404
    assert body["title"] == "Not Found"
    assert body["instance"] == f"{API}/products/999999"
