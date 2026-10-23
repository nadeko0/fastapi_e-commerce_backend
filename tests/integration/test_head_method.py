"""HEAD on every GET route (RFC 9110 section 9.1: general-purpose servers MUST
support GET and HEAD; section 9.3.2: identical to GET except no content) and
HEAD in the Allow header of a 405 (section 10.2.1)."""

from test_admin import (
    ADMIN_PREFIX,
    CATALOG_PREFIX,
    _auth_headers,
    _login_as,
    _make_admin,
    _make_category,
    _valid_product_payload,
)
from test_rate_limit_api import auth_header, limits  # noqa: F401  (fixture)

from app.core.config import settings
from app.main import app

API = settings.API_V1_STR
ORIGIN = "http://localhost:3000"

# Set per request by the server or the client library, not part of the
# representation metadata compared here.
VOLATILE = {"date", "x-request-id", "traceparent"}


def _stable(response):
    return {k: v for k, v in response.headers.items() if k not in VOLATILE}


def assert_head_matches_get(client, url, headers=None):
    got = client.get(url, headers=headers)
    head = client.head(url, headers=headers)
    assert head.status_code == got.status_code
    assert head.content == b""
    assert _stable(head) == _stable(got)
    return got, head


def test_head_on_public_catalog_routes(client):
    for path in ("/products", "/categories", "/categories/tree"):
        got, _ = assert_head_matches_get(client, f"{API}{path}")
        assert got.status_code == 200, path
    assert "content-length" in client.head(f"{API}/categories").headers


def test_head_on_a_404_has_no_body(client):
    got, head = assert_head_matches_get(client, f"{API}/products/999999")
    assert got.status_code == 404 and got.content
    unknown = client.head(f"{API}/no/such/path")
    assert unknown.status_code == 404 and unknown.content == b""


def test_head_on_authenticated_route(client, valid_registration_payload):
    headers = auth_header(client, valid_registration_payload)
    got, _ = assert_head_matches_get(client, f"{API}/users/me", headers)
    assert got.status_code == 200
    anonymous = client.head(f"{API}/users/me")
    assert anonymous.status_code == 401 and anonymous.content == b""


def test_head_on_admin_list(client, db_session):
    admin = _make_admin(db_session)
    headers = _auth_headers(_login_as(client, admin.email, "AdminPass1"))
    got, _ = assert_head_matches_get(client, f"{ADMIN_PREFIX}/orders", headers)
    assert got.status_code == 200
    assert client.head(f"{ADMIN_PREFIX}/orders").status_code == 401


def test_head_gets_the_same_304_as_get(client, db_session):
    admin = _make_admin(db_session)
    headers = _auth_headers(_login_as(client, admin.email, "AdminPass1"))
    category = _make_category(db_session)
    product = client.post(
        f"{ADMIN_PREFIX}/products", json=_valid_product_payload(category.id), headers=headers
    ).json()["data"]
    url = f"{CATALOG_PREFIX}/products/{product['id']}"
    etag = client.get(url).headers["etag"]
    assert client.head(url).headers["etag"] == etag
    conditional = client.head(url, headers={"If-None-Match": etag})
    assert conditional.status_code == 304 and conditional.content == b""


def test_head_carries_deprecation_headers_of_the_get(client):
    _, head = assert_head_matches_get(client, f"{API}/users/verify-email/sometoken")
    assert "deprecation" in head.headers and "sunset" in head.headers


def test_head_carries_security_headers_and_request_id(client):
    head = client.head(f"{API}/categories", headers={"X-Request-ID": "head-1"})
    assert head.headers["x-request-id"] == "head-1"
    assert head.headers["x-content-type-options"] == "nosniff"


def test_probes_keep_their_own_head_routes(client):
    for path in ("/health", "/livez", "/readyz"):
        head = client.head(path)
        assert head.status_code in (200, 503) and head.content == b""
        assert head.headers["cache-control"] == client.get(path).headers["cache-control"]


def test_head_on_a_post_only_route_is_405_with_allow(client):
    response = client.head(f"{API}/users/login")
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    assert response.content == b""


# --- 405 Allow ---------------------------------------------------------------


def test_405_allow_lists_head_wherever_get_is_allowed(client):
    response = client.post(f"{API}/categories")
    assert response.status_code == 405
    assert {m.strip() for m in response.headers["allow"].split(",")} == {"GET", "HEAD"}

    response = client.delete(f"{API}/products/1")
    allow = {m.strip() for m in response.headers["allow"].split(",")}
    assert response.status_code == 405 and {"GET", "HEAD"} <= allow


def test_405_allow_without_get_does_not_gain_head(client):
    response = client.get(f"{API}/users/login")
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


# --- rate limiting -----------------------------------------------------------


def test_head_counts_against_the_budget_of_its_get(client, limits):  # noqa: F811
    limits(RATE_LIMIT_DEFAULT="3/minute")
    assert client.get(f"{API}/categories").status_code == 200
    assert client.head(f"{API}/categories").status_code == 200
    assert client.head(f"{API}/categories").status_code == 200
    blocked_head = client.head(f"{API}/categories")
    assert blocked_head.status_code == 429 and blocked_head.content == b""
    assert "retry-after" in blocked_head.headers
    assert client.get(f"{API}/categories").status_code == 429


def test_head_on_search_uses_the_search_limit(client, limits):  # noqa: F811
    limits(RATE_LIMIT_SEARCH="1/minute")
    assert client.head(f"{API}/products/search", params={"query": "shoe"}).status_code != 429
    assert client.get(f"{API}/products/search", params={"query": "shoe"}).status_code == 429


# --- CORS and OpenAPI --------------------------------------------------------


def test_cross_origin_head_gets_the_cors_header(client):
    head = client.head(f"{API}/categories", headers={"Origin": ORIGIN})
    assert head.headers["access-control-allow-origin"] == ORIGIN


def test_openapi_has_no_head_operations():
    for path, operations in app.openapi()["paths"].items():
        assert "head" not in operations, path
