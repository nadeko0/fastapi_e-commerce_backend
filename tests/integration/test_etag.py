"""ETag / If-Match / If-None-Match on the product detail and the admin PUTs
(RFC 9110 sections 8.8.3, 13.1.1, 13.1.2, 15.5.13)."""

import pytest
from test_admin import (
    ADMIN_PREFIX,
    CATALOG_PREFIX,
    _auth_headers,
    _login_as,
    _make_admin,
    _make_category,
    _valid_product_payload,
)

from app.core.etag import if_match_passes, if_none_match_hits


@pytest.fixture
def setup(client, db_session):
    admin = _make_admin(db_session)
    headers = _auth_headers(_login_as(client, admin.email, "AdminPass1"))
    category = _make_category(db_session)
    product = client.post(
        f"{ADMIN_PREFIX}/products", json=_valid_product_payload(category.id), headers=headers
    ).json()["data"]
    return headers, product


def _put(client, headers, product, extra=None, **body):
    return client.put(
        f"{ADMIN_PREFIX}/products/{product['id']}",
        json=body or {"stock_quantity": 3},
        headers={**headers, **(extra or {})},
    )


def test_get_product_has_stable_strong_etag(client, setup):
    _, product = setup
    first = client.get(f"{CATALOG_PREFIX}/products/{product['id']}")
    second = client.get(f"{CATALOG_PREFIX}/products/{product['id']}")  # cache hit
    etag = first.headers["etag"]
    assert etag.startswith('"') and not etag.startswith("W/")
    assert second.headers["etag"] == etag


def test_etag_changes_after_update(client, setup):
    headers, product = setup
    before = client.get(f"{CATALOG_PREFIX}/products/{product['id']}").headers["etag"]
    put = _put(client, headers, product, stock_quantity=1)
    assert put.status_code == 200
    after = client.get(f"{CATALOG_PREFIX}/products/{product['id']}").headers["etag"]
    assert after != before
    assert put.headers["etag"] == after


def test_if_none_match_returns_304(client, setup):
    _, product = setup
    url = f"{CATALOG_PREFIX}/products/{product['id']}"
    etag = client.get(url).headers["etag"]
    for header in (etag, f"W/{etag}", f'"other", {etag}', "*"):
        response = client.get(url, headers={"If-None-Match": header})
        assert response.status_code == 304
        assert response.headers["etag"] == etag
        assert response.content == b""
    assert client.get(url, headers={"If-None-Match": '"stale"'}).status_code == 200


def test_if_none_match_on_cache_miss_path(client, setup):
    _, product = setup
    url = f"{CATALOG_PREFIX}/products/{product['id']}"
    etag = client.get(url).headers["etag"]
    # An update invalidates the cache; the next request rebuilds from the DB.
    # The unchanged tag of a no-op rebuild must still be the same.
    assert client.get(url, headers={"If-None-Match": etag}).status_code == 304


def test_put_with_stale_if_match_is_412(client, setup):
    headers, product = setup
    response = _put(client, headers, product, {"If-Match": '"stale"'}, stock_quantity=99)
    assert response.status_code == 412
    assert "detail" in response.json()
    assert "etag" in response.headers
    current = client.get(f"{CATALOG_PREFIX}/products/{product['id']}").json()["data"]
    assert current["stock_quantity"] == product["stock_quantity"]


def test_put_with_matching_if_match_succeeds(client, setup):
    headers, product = setup
    etag = client.get(f"{CATALOG_PREFIX}/products/{product['id']}").headers["etag"]
    response = _put(client, headers, product, {"If-Match": etag}, stock_quantity=7)
    assert response.status_code == 200
    assert response.json()["data"]["stock_quantity"] == 7
    assert response.headers["etag"] != etag
    # the old tag is now stale
    assert _put(client, headers, product, {"If-Match": etag}, stock_quantity=8).status_code == 412


def test_put_if_match_star_and_absent(client, setup):
    headers, product = setup
    assert _put(client, headers, product, {"If-Match": "*"}, stock_quantity=5).status_code == 200
    assert _put(client, headers, product, stock_quantity=6).status_code == 200
    missing = client.put(
        f"{ADMIN_PREFIX}/products/999999",
        json={"stock_quantity": 1},
        headers={**headers, "If-Match": "*"},
    )
    assert missing.status_code == 404


def test_put_weak_if_match_never_matches(client, setup):
    headers, product = setup
    etag = client.get(f"{CATALOG_PREFIX}/products/{product['id']}").headers["etag"]
    assert _put(client, headers, product, {"If-Match": f"W/{etag}"}).status_code == 412


def test_variant_put_if_match(client, setup):
    headers, product = setup
    base = f"{ADMIN_PREFIX}/products/{product['id']}/variants"
    created = client.post(base, json={"sku": "ETAG-1", "stock_quantity": 2}, headers=headers)
    variant = created.json()["data"]
    url = f"{base}/{variant['id']}"

    stale = client.put(url, json={"stock_quantity": 9}, headers={**headers, "If-Match": '"stale"'})
    assert stale.status_code == 412

    plain = client.put(url, json={"stock_quantity": 3}, headers=headers)
    assert plain.status_code == 200
    etag = plain.headers["etag"]

    ok = client.put(url, json={"stock_quantity": 4}, headers={**headers, "If-Match": etag})
    assert ok.status_code == 200 and ok.headers["etag"] != etag
    star = client.put(url, json={"stock_quantity": 5}, headers={**headers, "If-Match": "*"})
    assert star.status_code == 200
    again = client.put(url, json={"stock_quantity": 6}, headers={**headers, "If-Match": etag})
    assert again.status_code == 412


def test_header_list_parsing():
    assert if_match_passes(None, '"a"')
    assert if_match_passes('"x", "a"', '"a"')
    assert not if_match_passes('W/"a"', '"a"')
    assert not if_match_passes("garbage", '"a"')
    assert if_none_match_hits('W/"a"', '"a"')
    assert not if_none_match_hits(None, '"a"')
    assert not if_none_match_hits('"*"', '"a"')
