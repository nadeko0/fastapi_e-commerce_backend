"""GET /products/{id} lists the product's active variants.

A product sold by its variants (the cart refuses a line without variant_id)
used to answer with its own price and stock only, and a storefront had no
public call to learn the variants. The detail now carries has_variants and the
active variants with their effective price and stock. They are read on every
request, not kept in the Redis copy: an order for a variant takes the variant's
stock without dropping the product's cache entry.
"""

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
from test_query_counts import StatementCounter


@pytest.fixture
def setup(client, db_session):
    admin = _make_admin(db_session)
    headers = _auth_headers(_login_as(client, admin.email, "AdminPass1"))
    category = _make_category(db_session)
    product = client.post(
        f"{ADMIN_PREFIX}/products",
        json={**_valid_product_payload(category.id), "price": "19.99", "stock_quantity": 98},
        headers=headers,
    ).json()
    return headers, product


def _add_variant(client, headers, product, sku, **body):
    response = client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": sku, "stock_quantity": 5, **body},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _detail(client, product):
    return client.get(f"{CATALOG_PREFIX}/products/{product['id']}")


def test_product_without_variants_has_an_empty_list(client, setup):
    _, product = setup

    body = _detail(client, product).json()

    assert body["has_variants"] is False
    assert body["variants"] == []
    assert body["price"] == "19.99"
    assert body["stock_quantity"] == 98


def test_product_with_variants_lists_the_active_ones(client, setup):
    headers, product = setup
    priced = _add_variant(
        client, headers, product, "SKU-M", price_override="24.50",
        attributes={"size": "M"}, stock_quantity=7,
    )
    inherits = _add_variant(client, headers, product, "SKU-L", attributes={"size": "L"})
    hidden = _add_variant(client, headers, product, "SKU-OLD", is_active=False)

    response = _detail(client, product)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_variants"] is True
    assert body["price"] == "19.99" and body["stock_quantity"] == 98  # the product's own, unchanged
    assert [v["id"] for v in body["variants"]] == [priced["id"], inherits["id"]]
    assert hidden["id"] not in [v["id"] for v in body["variants"]]
    assert body["variants"][0] == {
        "id": priced["id"],
        "sku": "SKU-M",
        "attributes": {"size": "M"},
        "price": "24.50",
        "currency": body["currency"],
        "stock_quantity": 7,
    }
    assert body["variants"][1]["price"] == "19.99"  # no override: the product's price


def test_only_inactive_variants_means_no_variants(client, setup):
    headers, product = setup
    _add_variant(client, headers, product, "SKU-OLD", is_active=False)

    body = _detail(client, product).json()

    assert body["has_variants"] is False
    assert body["variants"] == []


def test_etag_changes_after_a_variant_update(client, setup):
    headers, product = setup
    variant = _add_variant(client, headers, product, "SKU-M", price_override="24.50")
    first = _detail(client, product)  # primes the cache
    etag = first.headers["etag"]
    assert _detail(client, product).headers["etag"] == etag

    update = client.put(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants/{variant['id']}",
        json={"stock_quantity": 1, "price_override": "26.00"},
        headers=headers,
    )
    assert update.status_code == 200, update.text

    after = _detail(client, product)
    assert after.headers["etag"] != etag
    assert after.json()["variants"][0]["price"] == "26.00"
    assert client.get(
        f"{CATALOG_PREFIX}/products/{product['id']}", headers={"If-None-Match": etag}
    ).status_code == 200
    assert client.get(
        f"{CATALOG_PREFIX}/products/{product['id']}", headers={"If-None-Match": after.headers["etag"]}
    ).status_code == 304


def test_variant_stock_is_not_served_from_the_cache(client, db_session, setup):
    # A variant's stock changes without the product's cache entry being
    # dropped (an order for it); the detail must still show the new number.
    from app.models.product import ProductVariant

    headers, product = setup
    variant = _add_variant(client, headers, product, "SKU-M", stock_quantity=5)
    etag = _detail(client, product).headers["etag"]  # primes the cache

    row = db_session.get(ProductVariant, variant["id"])
    row.stock_quantity = 2
    db_session.commit()

    after = _detail(client, product)
    assert after.json()["variants"][0]["stock_quantity"] == 2
    assert after.headers["etag"] != etag


def test_query_count_does_not_grow_with_the_variants(client, setup):
    headers, product = setup

    def statements():
        with StatementCounter() as counter:
            response = _detail(client, product)
        assert response.status_code == 200, response.text
        return len(counter.statements), len(response.json()["variants"])

    _add_variant(client, headers, product, "SKU-0")
    small, small_variants = statements()
    for n in range(1, 10):
        _add_variant(client, headers, product, f"SKU-{n}")
    large, large_variants = statements()

    assert (small_variants, large_variants) == (1, 10)
    assert large == small, f"{small} statements for 1 variant, {large} for 10"
