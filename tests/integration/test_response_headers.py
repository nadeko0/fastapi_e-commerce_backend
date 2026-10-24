"""Response headers the app adds on its own: Cache-Control: no-store on
responses that carry tokens or personal data (RFC 9111 section 5.2.2.5; OWASP
REST Security Cheat Sheet, "Security Headers"), WWW-Authenticate on every 401
(RFC 9110 section 15.5.2, RFC 6750 section 3) and the security headers on
responses the middleware stack does not normally see (500, 503)."""

from app.api.v1.cart import guest_cart_cookie_name
from app.core.security import get_password_hash
from app.models.enums import UserRole
from app.models.user import User

USERS = "/api/v1/users"
CART = "/api/v1/cart"
ORDERS = "/api/v1/orders"
ADMIN = "/api/v1/admin"
PRODUCTS = "/api/v1/products"
PASSWORD = "Str0ngPass1"


def _payload(email):
    return {
        "email": email,
        "password": PASSWORD,
        "full_name": "Header Tester",
        "gdpr_consent": True,
        "privacy_policy_accepted": True,
        "marketing_consent": False,
    }


def _login(client, email="headers@example.com", password=PASSWORD):
    return client.post(f"{USERS}/login", data={"username": email, "password": password})


def _registered_login(client, email="headers@example.com"):
    assert client.post(f"{USERS}/register", json=_payload(email)).status_code == 202
    response = _login(client, email)
    assert response.status_code == 200
    return response


def _bearer(response):
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _admin_headers(client, db_session):
    db_session.add(User(
        email="admin.headers@example.com",
        hashed_password=get_password_hash(PASSWORD),
        full_name="Admin",
        phone="+123456789",
        role=UserRole.ADMIN,
        gdpr_consent=True,
        privacy_policy_accepted=True,
        marketing_consent=False,
        consent_history=[],
        is_active=True,
    ))
    db_session.commit()
    return _bearer(_login(client, "admin.headers@example.com"))


# --- Cache-Control: no-store -------------------------------------------------

def test_login_and_refresh_responses_are_no_store(client):
    login = _registered_login(client)
    refresh = client.post(
        f"{USERS}/refresh",
        json={"refresh_token": login.json()["refresh_token"]},
    )

    assert refresh.status_code == 200
    assert login.headers["cache-control"] == "no-store"
    assert refresh.headers["cache-control"] == "no-store"


def test_me_and_pay_responses_are_no_store(client):
    headers = _bearer(_registered_login(client))

    me = client.get(f"{USERS}/me", headers=headers)
    pay = client.post(f"{ORDERS}/999999/pay", json={}, headers=headers)

    assert me.status_code == 200
    assert me.headers["cache-control"] == "no-store"
    # Whatever /pay answers (it carries the Stripe client_secret on success),
    # it answers with no-store.
    assert pay.headers["cache-control"] == "no-store"


def test_admin_response_is_no_store(client, db_session):
    headers = _admin_headers(client, db_session)

    response = client.get(f"{ADMIN}/orders", headers=headers)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_guest_cart_response_is_no_store(client):
    cart = client.get(CART)
    # The guest-cart cookie marks a request as the visitor's own even on a
    # route outside the private prefixes.
    catalog_with_cookie = client.get(PRODUCTS, headers={"Cookie": f"{guest_cart_cookie_name()}=x"})

    assert cart.headers["cache-control"] == "no-store"
    assert catalog_with_cookie.headers["cache-control"] == "no-store"


def test_authenticated_request_outside_the_private_prefixes_is_no_store(client):
    headers = _bearer(_registered_login(client))

    response = client.get(PRODUCTS, headers=headers)

    assert response.headers["cache-control"] == "no-store"


def test_public_catalog_response_is_unchanged(client):
    response = client.get(PRODUCTS)

    assert response.status_code == 200
    assert "cache-control" not in response.headers


def test_a_cache_control_set_by_the_route_is_kept(client, monkeypatch):
    from app.main import app

    @app.get(f"{USERS}/_cache_probe")
    def probe():
        from fastapi.responses import JSONResponse
        return JSONResponse({}, headers={"Cache-Control": "private, max-age=5"})

    try:
        response = client.get(f"{USERS}/_cache_probe")
    finally:
        app.router.routes.pop()

    assert response.headers["cache-control"] == "private, max-age=5"


# --- WWW-Authenticate on every 401 -------------------------------------------

BEARER = "Bearer"
INVALID_TOKEN = 'Bearer error="invalid_token"'


def test_login_with_wrong_credentials_is_401_with_a_challenge(client):
    _registered_login(client)

    wrong_password = _login(client, password="Wr0ngPass1")
    unknown_email = _login(client, email="nobody@example.com")

    assert wrong_password.status_code == 401
    assert unknown_email.status_code == 401
    assert wrong_password.headers["www-authenticate"] == BEARER
    assert unknown_email.headers["www-authenticate"] == BEARER


def test_token_presented_and_rejected_names_invalid_token(client):
    from app.core.security import create_access_token

    login = _registered_login(client)
    good = login.json()["access_token"]
    refresh = login.json()["refresh_token"]
    garbage = {"Authorization": "Bearer not-a-jwt"}
    wrong_type = {"Authorization": f"Bearer {refresh}"}
    tampered = {"Authorization": f"Bearer {good[:-3]}abc"}
    no_user = {"Authorization": f"Bearer {create_access_token(subject='999999')}"}

    for headers in (garbage, wrong_type, tampered, no_user):
        response = client.get(f"{USERS}/me", headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == INVALID_TOKEN

    # A logged-out (blacklisted) token is presented and rejected too.
    auth = {"Authorization": f"Bearer {good}"}
    assert client.post(f"{USERS}/logout", headers=auth).status_code == 200
    again = client.get(f"{USERS}/me", headers=auth)
    assert again.status_code == 401
    assert again.headers["www-authenticate"] == INVALID_TOKEN


def test_missing_token_keeps_the_bare_challenge(client):
    for response in (
        client.get(f"{USERS}/me"),
        client.get(f"{USERS}/me", headers={"Authorization": "Basic abc"}),
        client.get(f"{USERS}/me", headers={"Authorization": "Bearer"}),
        client.get(f"{ADMIN}/orders"),
    ):
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == BEARER


def test_every_401_the_app_sends_carries_a_challenge(client):
    """Walks the places that raise 401: login, refresh, the bearer
    dependency (missing and rejected token) and the password
    re-confirmations (status unchanged, header added)."""
    headers = _bearer(_registered_login(client))
    responses = [
        _login(client, password="Wr0ngPass1"),
        client.post(f"{USERS}/refresh", json={"refresh_token": "nope"}),
        client.get(f"{USERS}/me"),
        client.get(f"{USERS}/me", headers={"Authorization": "Bearer nope"}),
        client.post(
            "/api/v1/legal/data-request",
            json={"request_type": "deletion", "password": "Wr0ngPass1"},
            headers=headers,
        ),
    ]

    for response in responses:
        assert response.status_code == 401, response.text
        assert response.headers["www-authenticate"].startswith(BEARER)


# --- security headers on responses the middleware stack does not see ---------

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from app.core.overload import InFlightLimitMiddleware  # noqa: E402
from app.main import app  # noqa: E402

SECURITY_HEADERS = (
    "strict-transport-security",
    "x-content-type-options",
    "x-frame-options",
    "content-security-policy",
    "permissions-policy",
)


@pytest.fixture
def exploding_route():
    @app.get("/_explode")
    def explode():
        raise RuntimeError("boom")

    try:
        yield "/_explode"
    finally:
        app.router.routes.pop()


def test_unhandled_exception_500_carries_the_security_headers(client, exploding_route):
    # ServerErrorMiddleware answers this 500 outside every user middleware.
    with TestClient(app, raise_server_exceptions=False) as lenient:
        response = lenient.get(exploding_route)
        normal = lenient.get("/health")

    assert response.status_code == 500
    assert response.json()["title"] == "Internal Server Error"
    assert response.headers["content-type"] == "application/problem+json"
    for name in SECURITY_HEADERS:
        assert response.headers[name] == normal.headers[name]


def test_500_on_a_private_route_is_no_store_too(client):
    @app.get(f"{USERS}/_explode")
    def explode():
        raise RuntimeError("boom")

    try:
        with TestClient(app, raise_server_exceptions=False) as lenient:
            response = lenient.get(f"{USERS}/_explode")
    finally:
        app.router.routes.pop()

    assert response.status_code == 500
    assert response.headers["cache-control"] == "no-store"


def test_the_headers_layer_is_outside_the_in_flight_limit(client):
    from app.core.security_headers import SecurityHeadersMiddleware

    # The shed 503 is sent by InFlightLimitMiddleware, so the headers layer
    # has to wrap it: the outermost user middleware.
    assert app.user_middleware[0].cls is SecurityHeadersMiddleware

    async def index(request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/", index)])

    stack = SecurityHeadersMiddleware(InFlightLimitMiddleware(inner, limit=0))
    with TestClient(stack) as probe:
        response = probe.get("/")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    for name in SECURITY_HEADERS:
        assert name in response.headers
