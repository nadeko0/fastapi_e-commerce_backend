"""RFC 9745 Deprecation, RFC 8594 Sunset and an RFC 8288 Link on the operations
4.0.0 removes or changes, and on nothing else."""
import re
from email.utils import parsedate_to_datetime

import pytest

from app.core.deprecation import (
    DEPRECATED_OPERATIONS,
    DEPRECATED_SINCE,
    SUNSET,
)

# Concrete URLs of DEPRECATED_OPERATIONS. Unauthenticated: the headers are on
# the 401/422 too.
CALLS = [
    ("GET", "/api/v1/users/verify-email/sometoken"),
    ("GET", "/api/v1/users/password/reset/sometoken"),
    ("GET", "/api/v1/users/data/export"),
    ("POST", "/api/v1/users/data/delete"),
    ("POST", "/api/v1/users/consent"),
]

LINK_ITEM = re.compile(r'^<[^<>\s]+>; rel="(deprecation|successor-version)"(; type="text/html")?$')


def test_every_deprecated_operation_is_covered():
    assert len(CALLS) == len(DEPRECATED_OPERATIONS)


@pytest.mark.parametrize(("method", "url"), CALLS)
def test_deprecated_route_carries_the_three_headers(client, method, url):
    response = client.request(method, url)

    deprecation = response.headers["Deprecation"]
    assert re.fullmatch(r"@\d+", deprecation)
    assert int(deprecation[1:]) == int(DEPRECATED_SINCE.timestamp())

    sunset = parsedate_to_datetime(response.headers["Sunset"])
    assert sunset == SUNSET
    assert sunset >= DEPRECATED_SINCE
    assert response.headers["Sunset"].endswith(" GMT")

    items = response.headers["Link"].split(", ")
    assert all(LINK_ITEM.match(item) for item in items), items
    assert any('rel="deprecation"' in item for item in items)


def test_successor_version_link_where_a_replacement_exists(client):
    link = client.post("/api/v1/users/consent").headers["Link"]
    assert '</api/v1/legal/consent>; rel="successor-version"' in link

    link = client.get("/api/v1/users/verify-email/x").headers["Link"]
    assert '</api/v1/users/verify-email>; rel="successor-version"' in link


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "/health"),
        ("GET", "/api/v1/products"),
        ("GET", "/api/v1/cart"),
        ("POST", "/api/v1/legal/consent"),
        ("POST", "/api/v1/legal/data-request"),
        ("POST", "/api/v1/users/login"),
        ("GET", "/api/v1/nonexistent"),
    ],
)
def test_other_routes_carry_none(client, method, url):
    response = client.request(method, url)

    for name in ("Deprecation", "Sunset", "Link"):
        assert name not in response.headers


def test_openapi_marks_exactly_these_operations_deprecated(client):
    schema = client.get("/api/v1/openapi.json").json()

    flagged = {
        (method.upper(), path)
        for path, item in schema["paths"].items()
        for method, op in item.items()
        if op.get("deprecated")
    }

    assert flagged == {(m, "/api/v1" + p) for m, p in DEPRECATED_OPERATIONS}


def test_deprecation_headers_are_exposed_to_browsers(client):
    response = client.get("/health", headers={"Origin": "http://localhost:3000"})

    exposed = {h.strip() for h in response.headers["access-control-expose-headers"].split(",")}
    assert {"Deprecation", "Sunset", "Link"} <= exposed
