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
    assert client.post(f"{USERS}/register", json=_payload(email)).status_code == 200
    response = _login(client, email)
    assert response.status_code == 200
    return response


def _bearer(response):
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


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
        json={"refresh_token": login.json()["data"]["refresh_token"]},
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
