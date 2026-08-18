from app.models.category import Category
from app.models.product import Product

CATALOG_PREFIX = "/api/v1"


def _make_category(db_session, name="Electronics", parent_id=None, level=0, path=None):
    category = Category(name=name, parent_id=parent_id, level=level, path=path or [])
    db_session.add(category)
    db_session.commit()
    db_session.refresh(category)
    return category


def _make_product(db_session, category_id, name="Widget", price="9.99", stock=10):
    product = Product(
        name=name,
        description="A fine widget for all your widget needs",
        price=price,
        stock_quantity=stock,
        images=["https://example.com/widget.png"],
        characteristics={},
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
