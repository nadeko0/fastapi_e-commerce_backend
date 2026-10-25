"""The Allow header of a 405 lists every method the path supports.

RFC 9110 section 15.5.6: "The origin server MUST generate an Allow header
field in a 405 response containing a list of the target resource's currently
supported methods". Where several routes share one path (GET /users/me, PUT
/users/me and PATCH /users/me are three routes), Starlette answers a method
none of them has with the Allow of the FIRST route it finds for the path:
"GET, HEAD" for /users/me, though PUT and PATCH exist. app/core/errors.py now
builds it from all of them. HEAD is listed wherever GET is (app/core/head.py).
OPTIONS is not: the application answers it only as a CORS preflight, which
the CORS middleware handles before routing, and a plain OPTIONS is a 405 too.

The test takes the methods from the OpenAPI document, so a route added later
is held to it with no change here.
"""
import re

import pytest

from app.main import app

API = "/api/v1"
HTTP_METHODS = ("get", "put", "post", "delete", "patch")


def _paths():
    paths = app.openapi()["paths"]
    return sorted(paths), paths


def _declared(operations):
    methods = {m.upper() for m in operations if m in HTTP_METHODS}
    return methods | {"HEAD"} if "GET" in methods else methods


def _allow(response):
    return [m.strip() for m in response.headers["allow"].split(",")]


@pytest.mark.parametrize("path", _paths()[0])
def test_405_allow_is_what_the_path_supports(client, path):
    # BREW is a method nothing implements, so the answer is a 405 on every path.
    url = re.sub(r"\{[^}]+\}", "1", path)
    response = client.request("BREW", url)
    assert response.status_code == 405
    allow = _allow(response)
    assert len(allow) == len(set(allow))
    assert set(allow) == _declared(_paths()[1][path])


def test_405_allow_of_a_path_with_one_route_per_method(client):
    response = client.put(f"{API}/cart")
    assert response.status_code == 405
    assert set(_allow(response)) == _declared(_paths()[1][f"{API}/cart"])


def test_405_allow_of_users_me_lists_get_put_patch_and_head(client):
    response = client.delete(f"{API}/users/me")
    assert response.status_code == 405
    assert set(_allow(response)) == {"GET", "HEAD", "PUT", "PATCH"}


def test_a_method_the_path_supports_is_not_a_405(client):
    assert client.head(f"{API}/products").status_code == 200
    assert client.put(f"{API}/users/me", json={}).status_code != 405
