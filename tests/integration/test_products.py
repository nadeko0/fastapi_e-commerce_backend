from app.models.category import Category
from app.models.product import Product

CATALOG_PREFIX = "/api/v1"


def _make_category(db_session, name="Electronics", parent_id=None, level=0, path=None):
    category = Category(name=name, parent_id=parent_id, level=level, path=path or [])
    db_session.add(category)
    db_session.commit()
    db_session.refresh(category)
    return category


def _make_product(db_session, category_id, name="Widget", price="9.99", stock=10, characteristics=None):
    product = Product(
        name=name,
        description="A fine widget for all your widget needs",
        price=price,
        stock_quantity=stock,
        images=["https://example.com/widget.png"],
        characteristics=characteristics or {},
        category_id=category_id,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def test_list_categories_returns_created_category(client, db_session):
    _make_category(db_session)

    response = client.get(f"{CATALOG_PREFIX}/categories")

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Electronics"


def test_get_category_returns_404_for_missing_id(client):
    response = client.get(f"{CATALOG_PREFIX}/categories/999999")

    assert response.status_code == 404


def test_get_category_returns_existing_category(client, db_session):
    category = _make_category(db_session)

    response = client.get(f"{CATALOG_PREFIX}/categories/{category.id}")

    assert response.status_code == 200
    assert response.json()["data"]["id"] == category.id


def test_category_tree_reflects_parent_child_relationship(client, db_session):
    parent = _make_category(db_session, name="Root")
    _make_category(db_session, name="Child", parent_id=parent.id, level=1, path=[parent.id])

    response = client.get(f"{CATALOG_PREFIX}/categories/tree")

    assert response.status_code == 200
    tree = response.json()["data"]["tree"]
    assert len(tree) == 1
    assert tree[0]["name"] == "Root"
    assert len(tree[0]["children"]) == 1
    assert tree[0]["children"][0]["name"] == "Child"


def test_category_tree_nests_correctly_at_three_levels(client, db_session):
    # Regression coverage: get_category_tree (app/api/v1/products.py)
    # recursively attaches children by parent_id, so a shallow (2-level)
    # tree alone wouldn't catch a build_tree bug that only breaks past the
    # first level. Assert the full root -> child -> grandchild nesting.
    root = _make_category(db_session, name="Root")
    child = _make_category(db_session, name="Child", parent_id=root.id, level=1, path=[root.id])
    _make_category(
        db_session, name="Grandchild", parent_id=child.id, level=2, path=[root.id, child.id]
    )

    response = client.get(f"{CATALOG_PREFIX}/categories/tree")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total_categories"] == 3
    assert data["max_depth"] == 2

    tree = data["tree"]
    assert len(tree) == 1
    root_node = tree[0]
    assert root_node["name"] == "Root"
    assert len(root_node["children"]) == 1

    child_node = root_node["children"][0]
    assert child_node["name"] == "Child"
    assert len(child_node["children"]) == 1

    grandchild_node = child_node["children"][0]
    assert grandchild_node["name"] == "Grandchild"
    assert grandchild_node["children"] == []


def test_list_products_returns_created_product(client, db_session):
    category = _make_category(db_session)
    _make_product(db_session, category.id)

    response = client.get(f"{CATALOG_PREFIX}/products")

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Widget"


def test_list_products_paginates(client, db_session):
    category = _make_category(db_session)
    for i in range(3):
        _make_product(db_session, category.id, name=f"Widget {i}")

    response = client.get(f"{CATALOG_PREFIX}/products", params={"page": 1, "size": 2})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["has_more"] is True


def test_list_products_filters_by_min_price(client, db_session):
    category = _make_category(db_session)
    _make_product(db_session, category.id, name="Cheap", price="1.00")
    _make_product(db_session, category.id, name="Pricey", price="99.99")

    response = client.get(f"{CATALOG_PREFIX}/products", params={"min_price": "50.00"})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Pricey"


def test_list_products_filters_in_stock_only(client, db_session):
    category = _make_category(db_session)
    _make_product(db_session, category.id, name="OutOfStock", stock=0)
    _make_product(db_session, category.id, name="InStock", stock=5)

    response = client.get(f"{CATALOG_PREFIX}/products", params={"in_stock": True})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["name"] == "InStock"


def test_get_product_returns_404_for_missing_id(client):
    response = client.get(f"{CATALOG_PREFIX}/products/999999")

    assert response.status_code == 404


def test_get_product_returns_existing_product(client, db_session):
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)

    response = client.get(f"{CATALOG_PREFIX}/products/{product.id}")

    assert response.status_code == 200
    assert response.json()["data"]["id"] == product.id
    assert response.json()["data"]["category_name"] == category.name


def test_get_product_is_served_from_cache_on_second_call(client, db_session):
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)

    first = client.get(f"{CATALOG_PREFIX}/products/{product.id}")
    assert first.status_code == 200

    # Mutate the DB row directly without going through the cache-invalidating
    # admin endpoint - a cache hit should still return the stale cached name.
    db_session.query(Product).filter(Product.id == product.id).update({"name": "Renamed"})
    db_session.commit()

    second = client.get(f"{CATALOG_PREFIX}/products/{product.id}")
    assert second.status_code == 200
    assert second.json()["data"]["name"] == "Widget"


def test_search_products_matches_name(client, db_session):
    category = _make_category(db_session)
    _make_product(db_session, category.id, name="Special Gadget")
    _make_product(db_session, category.id, name="Unrelated Item")

    response = client.get(f"{CATALOG_PREFIX}/products/search", params={"query": "Gadget"})

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Special Gadget"


def test_list_products_with_characteristics_filter_does_not_crash(client, db_session):
    # Regression test: the characteristics filter used
    # Product.characteristics[key].astext, which only exists on Postgres's
    # JSON/JSONB comparator - Product.characteristics is a generic
    # sqlalchemy.JSON column, so this raised AttributeError on every
    # request that passed a characteristics filter. Fixed to use the
    # cross-dialect .as_string() accessor, and to index into ["value"]
    # since stored characteristics are structured ProductCharacteristic
    # objects ({"color": {"name": "Color", "value": "red", ...}}), not
    # flat key->value pairs.
    #
    # This only asserts the endpoint doesn't crash (200, not 500) - SQLite
    # (this test's DB) doesn't reliably compile the double-nested JSON path
    # this filter needs (characteristics[key]["value"]), so it can't be
    # asserted to actually *filter* correctly here. That's verified against
    # real PostgreSQL instead, see scripts/pg_smoke_test.py (run manually
    # against a local Postgres - not part of this suite, which has no
    # Postgres available).
    category = _make_category(db_session)
    _make_product(
        db_session, category.id, name="Red Widget",
        characteristics={"color": {"name": "Color", "value": "red"}},
    )

    response = client.get(
        f"{CATALOG_PREFIX}/products",
        params={"characteristics": '{"color": "red"}'},
    )

    assert response.status_code == 200
