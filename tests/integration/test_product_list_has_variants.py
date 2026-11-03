"""GET /products and GET /products/search say per product whether it is sold by
variants (has_variants), from one query for the whole page, so a storefront
list can mark such products without opening each detail page.
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
    return headers, category


def _product(client, headers, category, name):
    response = client.post(
        f"{ADMIN_PREFIX}/products",
        json={**_valid_product_payload(category.id), "name": name},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _variant(client, headers, product, sku, **body):
    response = client.post(
        f"{ADMIN_PREFIX}/products/{product['id']}/variants",
        json={"sku": sku, "stock_quantity": 5, **body},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _flags(response):
    assert response.status_code == 200, response.text
    return {item["name"]: item["has_variants"] for item in response.json()["items"]}


def test_list_marks_only_products_with_an_active_variant(client, setup):
    headers, category = setup
    plain = _product(client, headers, category, "Plain mug")
    sold_by_variants = _product(client, headers, category, "Shirt mug")
    only_inactive = _product(client, headers, category, "Retired mug")
    _variant(client, headers, sold_by_variants, "SKU-A")
    _variant(client, headers, sold_by_variants, "SKU-B")
    _variant(client, headers, only_inactive, "SKU-OLD", is_active=False)

    flags = _flags(client.get(f"{CATALOG_PREFIX}/products"))

    assert flags == {plain["name"]: False, sold_by_variants["name"]: True, only_inactive["name"]: False}


def test_search_marks_has_variants_too(client, setup):
    headers, category = setup
    _product(client, headers, category, "Plain mug")
    sold_by_variants = _product(client, headers, category, "Shirt mug")
    _variant(client, headers, sold_by_variants, "SKU-A")

    flags = _flags(client.get(f"{CATALOG_PREFIX}/products/search", params={"query": "mug"}))

    assert flags == {"Plain mug": False, "Shirt mug": True}


def test_flag_follows_the_page_not_the_whole_catalog(client, setup):
    headers, category = setup
    first = _product(client, headers, category, "Mug 1")
    second = _product(client, headers, category, "Mug 2")
    _variant(client, headers, second, "SKU-2")

    page_one = _flags(client.get(f"{CATALOG_PREFIX}/products", params={"size": 1, "page": 1}))
    page_two = _flags(client.get(f"{CATALOG_PREFIX}/products", params={"size": 1, "page": 2}))

    assert page_one == {first["name"]: False}
    assert page_two == {second["name"]: True}


def test_empty_page_runs_no_variant_query(client, setup):
    with StatementCounter() as counter:
        response = client.get(f"{CATALOG_PREFIX}/products")
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    assert not any("product_variants" in statement for statement in counter.statements)


@pytest.mark.parametrize("path", ["/products", "/products/search"])
def test_statement_count_does_not_grow_with_the_page(client, setup, path):
    headers, category = setup
    params = {"query": "mug"} if path.endswith("search") else {}

    def statements():
        with StatementCounter() as counter:
            response = client.get(f"{CATALOG_PREFIX}{path}", params=params)
        assert response.status_code == 200, response.text
        return len(counter.statements), len(response.json()["items"])

    first = _product(client, headers, category, "Mug 0")
    _variant(client, headers, first, "SKU-0")
    small, small_items = statements()
    for n in range(1, 8):
        product = _product(client, headers, category, f"Mug {n}")
        if n % 2:
            _variant(client, headers, product, f"SKU-{n}")
    large, large_items = statements()

    assert (small_items, large_items) == (1, 8)
    assert large == small


def test_list_item_has_variants_agrees_with_the_detail(client, setup):
    headers, category = setup
    product = _product(client, headers, category, "Shirt mug")
    _variant(client, headers, product, "SKU-A")

    listed = client.get(f"{CATALOG_PREFIX}/products").json()["items"][0]
    detail = client.get(f"{CATALOG_PREFIX}/products/{product['id']}").json()

    assert listed["has_variants"] is detail["has_variants"] is True
