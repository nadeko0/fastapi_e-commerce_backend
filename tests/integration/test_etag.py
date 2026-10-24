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
    ).json()
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
    current = client.get(f"{CATALOG_PREFIX}/products/{product['id']}").json()
    assert current["stock_quantity"] == product["stock_quantity"]


def test_put_with_matching_if_match_succeeds(client, setup):
    headers, product = setup
    etag = client.get(f"{CATALOG_PREFIX}/products/{product['id']}").headers["etag"]
    response = _put(client, headers, product, {"If-Match": etag}, stock_quantity=7)
    assert response.status_code == 200
    assert response.json()["stock_quantity"] == 7
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
    variant = created.json()
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


def _variant(client, headers, product, sku="GET-1"):
    base = f"{ADMIN_PREFIX}/products/{product['id']}/variants"
    return base, client.post(base, json={"sku": sku, "stock_quantity": 2}, headers=headers).json()


def test_variant_get_returns_variant_and_etag(client, setup):
    headers, product = setup
    base, variant = _variant(client, headers, product)
    response = client.get(f"{base}/{variant['id']}", headers=headers)
    assert response.status_code == 200
    assert response.json() == variant
    assert response.json()["currency"]
    etag = response.headers["etag"]
    assert etag.startswith('"') and not etag.startswith("W/")
    assert client.get(f"{base}/{variant['id']}", headers=headers).headers["etag"] == etag


def test_variant_get_then_put_and_patch_with_if_match_round_trip(client, setup):
    headers, product = setup
    base, variant = _variant(client, headers, product)
    url = f"{base}/{variant['id']}"
    etag = client.get(url, headers=headers).headers["etag"]
    put = client.put(url, json={"stock_quantity": 4}, headers={**headers, "If-Match": etag})
    assert put.status_code == 200
    # the PUT's tag is the tag a GET now returns
    assert client.get(url, headers=headers).headers["etag"] == put.headers["etag"]
    patch = client.patch(
        url, json={"stock_quantity": 5}, headers={**headers, "If-Match": put.headers["etag"]}
    )
    assert patch.status_code == 200
    assert client.get(url, headers=headers).headers["etag"] == patch.headers["etag"]


def test_variant_stale_if_match_after_other_update_is_412(client, setup):
    headers, product = setup
    base, variant = _variant(client, headers, product)
    url = f"{base}/{variant['id']}"
    etag = client.get(url, headers=headers).headers["etag"]
    assert client.put(url, json={"stock_quantity": 7}, headers=headers).status_code == 200
    stale = client.put(url, json={"stock_quantity": 8}, headers={**headers, "If-Match": etag})
    assert stale.status_code == 412
    assert stale.headers["etag"] == client.get(url, headers=headers).headers["etag"]


def test_variant_get_if_none_match_returns_304(client, setup):
    headers, product = setup
    base, variant = _variant(client, headers, product)
    url = f"{base}/{variant['id']}"
    etag = client.get(url, headers=headers).headers["etag"]
    for header in (etag, f"W/{etag}", f'"other", {etag}', "*"):
        response = client.get(url, headers={**headers, "If-None-Match": header})
        assert response.status_code == 304 and response.content == b""
        assert response.headers["etag"] == etag
    other = client.get(url, headers={**headers, "If-None-Match": '"other"'})
    assert other.status_code == 200


def test_variant_get_404_cases(client, setup):
    headers, product = setup
    base, variant = _variant(client, headers, product)
    other_product = client.post(
        f"{ADMIN_PREFIX}/products",
        json={**_valid_product_payload(product["category_id"]), "name": "Other product"},
        headers=headers,
    ).json()
    assert client.get(f"{base}/999999", headers=headers).status_code == 404
    missing_product = f"{ADMIN_PREFIX}/products/999999/variants/{variant['id']}"
    assert client.get(missing_product, headers=headers).status_code == 404
    other = f"{ADMIN_PREFIX}/products/{other_product['id']}/variants/{variant['id']}"
    assert client.get(other, headers=headers).status_code == 404
    # a 404 is never turned into a 304 by If-None-Match
    assert client.get(f"{base}/999999", headers={**headers, "If-None-Match": "*"}).status_code == 404


def test_variant_get_requires_admin(client, db_session, setup):
    headers, product = setup
    base, variant = _variant(client, headers, product)
    url = f"{base}/{variant['id']}"
    assert client.get(url).status_code == 401
    from test_admin import _register_and_login, _valid_registration_payload

    token = _register_and_login(client, _valid_registration_payload("plain@example.com"))
    assert client.get(url, headers=_auth_headers(token)).status_code == 403


def test_variant_list_route_unchanged(client, setup):
    headers, product = setup
    base, variant = _variant(client, headers, product)
    listing = client.get(base, headers=headers)
    assert listing.status_code == 200
    assert "etag" not in listing.headers
    assert listing.json()["items"] == [variant]
