"""The public catalog's rules, end to end: what the listing filters, sorts
and pages; what search scopes; the category list and tree; and that a
product withdrawn from sale is nowhere to be seen."""

from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.models.category import Category
from app.models.product import Product

API = "/api/v1"

_PRICES = ["5.00", "1.50", "9.99", "3.25", "7.10", "2.00", "8.80"]


def _category(db, name, parent=None):
    category = Category(
        name=name,
        parent_id=parent.id if parent else None,
        level=parent.level + 1 if parent else 0,
        path=[*parent.path, parent.id] if parent else [],
    )
    db.add(category)
    db.commit()
    return category


def _product(db, category, name, price="9.99", stock=10, active=True, description=None):
    product = Product(
        name=name,
        description=description or "A plain description",
        price=price,
        stock_quantity=stock,
        images=["https://example.com/p.png"],
        characteristics={},
        category_id=category.id,
        is_active=active,
    )
    db.add(product)
    db.commit()
    return product


def _listing(client, **params):
    response = client.get(f"{API}/products", params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _names(page):
    return [item["name"] for item in page["items"]]


@pytest.fixture
def priced_catalog(db_session):
    category = _category(db_session, "Things")
    for index, price in enumerate(_PRICES):
        _product(db_session, category, f"Item {chr(ord('a') + index)}", price=price)
    return category


# --- pagination -------------------------------------------------------------


@settings(
    max_examples=30,
    deadline=None,
    # The catalogue is created once per test and only read by the examples.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(page=st.integers(min_value=1, max_value=6), size=st.integers(min_value=1, max_value=8))
def test_pages_are_consecutive_slices_of_the_sorted_catalogue(client, priced_catalog, page, size):
    ordered = [name for _, name in sorted(
        (Decimal(price), f"Item {chr(ord('a') + index)}") for index, price in enumerate(_PRICES)
    )]

    body = _listing(client, sort_by="price_asc", page=page, size=size)

    assert _names(body) == ordered[(page - 1) * size: page * size]
    assert body["total"] == len(_PRICES)
    assert (body["page"], body["size"]) == (page, size)
    assert body["has_more"] is (page * size < len(_PRICES))


@pytest.mark.parametrize(
    "params",
    [{"page": 0}, {"page": -1}, {"size": 0}, {"size": 101}, {"page": "x"}],
)
def test_out_of_range_paging_is_a_422(client, params):
    assert client.get(f"{API}/products", params=params).status_code == 422
    assert client.get(f"{API}/categories", params=params).status_code == 422


def test_largest_page_size_is_accepted(client, priced_catalog):
    assert len(_listing(client, size=100)["items"]) == len(_PRICES)


# --- sorting ----------------------------------------------------------------


@pytest.mark.parametrize(
    "sort_by,key,reverse",
    [
        ("price_asc", lambda item: Decimal(item["price"]), False),
        ("price_desc", lambda item: Decimal(item["price"]), True),
        ("name_asc", lambda item: item["name"], False),
        ("name_desc", lambda item: item["name"], True),
        ("created_at_asc", lambda item: item["created_at"], False),
        ("created_at_desc", lambda item: item["created_at"], True),
    ],
)
def test_every_documented_sort_orders_the_listing(client, priced_catalog, sort_by, key, reverse):
    items = _listing(client, sort_by=sort_by, size=100)["items"]
    assert items == sorted(items, key=key, reverse=reverse)


@pytest.mark.parametrize(
    "sort_by",
    ["price", "price_up", "stock_quantity_asc", "id_desc", "name_asc;drop", "PRICE_ASC"],
)
def test_anything_else_as_sort_by_is_a_422(client, sort_by):
    # sort_by picks a model attribute by name: only the three documented
    # columns may get that far.
    assert client.get(f"{API}/products", params={"sort_by": sort_by}).status_code == 422


# --- filters ----------------------------------------------------------------


def test_price_bounds_are_inclusive(client, priced_catalog):
    body = _listing(client, min_price="2.00", max_price="7.10", sort_by="price_asc")
    assert [item["price"] for item in body["items"]] == ["2.00", "3.25", "5.00", "7.10"]
    assert body["total"] == 4


def test_equal_bounds_select_one_price(client, priced_catalog):
    assert [item["price"] for item in _listing(client, min_price="9.99", max_price="9.99")["items"]] == ["9.99"]


@pytest.mark.parametrize(
    "params",
    [{"min_price": "-1"}, {"max_price": "-0.01"}, {"min_price": "cheap"}],
)
def test_impossible_price_bounds_are_a_422(client, params):
    assert client.get(f"{API}/products", params=params).status_code == 422


def test_in_stock_filter_and_its_absence(client, db_session):
    category = _category(db_session, "Things")
    _product(db_session, category, "Sold out", stock=0)
    _product(db_session, category, "Plenty", stock=3)

    assert _names(_listing(client, in_stock=True)) == ["Plenty"]
    # false means "do not filter", not "only what is sold out".
    assert sorted(_names(_listing(client, in_stock=False))) == ["Plenty", "Sold out"]
    assert sorted(_names(_listing(client))) == ["Plenty", "Sold out"]


def test_category_filter(client, db_session):
    tools = _category(db_session, "Tools")
    toys = _category(db_session, "Toys")
    _product(db_session, tools, "Hammer")
    _product(db_session, toys, "Kite")

    assert _names(_listing(client, category_id=tools.id)) == ["Hammer"]
    assert _listing(client, category_id=999999) == {
        "items": [], "total": 0, "page": 1, "size": 20, "has_more": False,
    }


def test_filters_combine(client, db_session):
    tools = _category(db_session, "Tools")
    toys = _category(db_session, "Toys")
    _product(db_session, tools, "Cheap hammer", price="4.00")
    _product(db_session, tools, "Dear hammer", price="40.00")
    _product(db_session, tools, "Cheap saw", price="3.00", stock=0)
    _product(db_session, toys, "Cheap hammer toy", price="2.00")

    body = _listing(client, category_id=tools.id, max_price="10", in_stock=True, search_query="hammer")

    assert _names(body) == ["Cheap hammer"]


# --- search -----------------------------------------------------------------


def test_search_matches_name_or_description_ignoring_case(client, db_session):
    category = _category(db_session, "Things")
    _product(db_session, category, "Blue Lamp")
    _product(db_session, category, "Chair", description="Goes well with a BLUE lamp")
    _product(db_session, category, "Table")

    response = client.get(f"{API}/products/search", params={"query": "blue"})

    assert sorted(item["name"] for item in response.json()["data"]["items"]) == ["Blue Lamp", "Chair"]


def test_search_can_be_scoped_to_a_category_and_paged(client, db_session):
    lamps = _category(db_session, "Lamps")
    other = _category(db_session, "Other")
    for index in range(3):
        _product(db_session, lamps, f"Lamp {index}")
    _product(db_session, other, "Lamp elsewhere")

    first = client.get(
        f"{API}/products/search", params={"query": "lamp", "category_id": lamps.id, "size": 2}
    ).json()["data"]
    second = client.get(
        f"{API}/products/search", params={"query": "lamp", "category_id": lamps.id, "size": 2, "page": 2}
    ).json()["data"]

    assert (first["total"], first["has_more"], len(first["items"])) == (3, True, 2)
    assert (second["has_more"], len(second["items"])) == (False, 1)
    assert {item["name"] for item in first["items"] + second["items"]} == {"Lamp 0", "Lamp 1", "Lamp 2"}


@pytest.mark.parametrize("params", [{}, {"query": "ab"}, {"query": ""}])
def test_search_needs_at_least_three_characters(client, params):
    assert client.get(f"{API}/products/search", params=params).status_code == 422


# --- visibility -------------------------------------------------------------


def test_a_product_withdrawn_from_sale_is_nowhere_to_be_seen(client, db_session):
    category = _category(db_session, "Things")
    _product(db_session, category, "Visible gadget")
    hidden = _product(db_session, category, "Hidden gadget", active=False)

    assert _names(_listing(client)) == ["Visible gadget"]
    assert _listing(client)["total"] == 1
    assert _names(_listing(client, search_query="gadget")) == ["Visible gadget"]
    search = client.get(f"{API}/products/search", params={"query": "gadget"}).json()["data"]
    assert [item["name"] for item in search["items"]] == ["Visible gadget"]
    assert client.get(f"{API}/products/{hidden.id}").status_code == 404


def test_a_product_is_not_cached_while_it_is_withdrawn(client, db_session, fake_redis):
    category = _category(db_session, "Things")
    hidden = _product(db_session, category, "Hidden", active=False)

    assert client.get(f"{API}/products/{hidden.id}").status_code == 404
    assert list(fake_redis.scan_iter("*product*")) == []


# --- categories -------------------------------------------------------------


def test_categories_can_be_listed_by_parent_and_paged(client, db_session):
    root = _category(db_session, "Root")
    children = [_category(db_session, f"Child {index}", parent=root) for index in range(3)]
    _category(db_session, "Other root")

    first = client.get(f"{API}/categories", params={"parent_id": root.id, "size": 2}).json()["data"]
    second = client.get(
        f"{API}/categories", params={"parent_id": root.id, "size": 2, "page": 2}
    ).json()["data"]
    everything = client.get(f"{API}/categories").json()["data"]

    assert (first["total"], first["has_more"]) == (3, True)
    assert {c["id"] for c in first["items"] + second["items"]} == {c.id for c in children}
    assert second["has_more"] is False
    assert everything["total"] == 5


def test_an_empty_category_tree(client):
    assert client.get(f"{API}/categories/tree").json()["data"] == {
        "tree": [], "total_categories": 0, "max_depth": 0,
    }


def test_the_tree_holds_every_root_and_reports_its_depth(client, db_session):
    first = _category(db_session, "First")
    second = _category(db_session, "Second")
    child = _category(db_session, "Child", parent=first)
    _category(db_session, "Grandchild", parent=child)

    body = client.get(f"{API}/categories/tree").json()["data"]

    assert sorted(node["name"] for node in body["tree"]) == ["First", "Second"]
    assert (body["total_categories"], body["max_depth"]) == (4, 2)
    by_name = {node["name"]: node for node in body["tree"]}
    assert by_name["Second"]["children"] == []
    assert by_name["First"]["children"][0]["children"][0]["name"] == "Grandchild"
    assert second.id in {node["id"] for node in body["tree"]}


def test_the_tree_is_served_from_the_cache_once_built(client, db_session):
    _category(db_session, "Root")
    first = client.get(f"{API}/categories/tree").json()["data"]

    _category(db_session, "Added after the tree was cached")
    second = client.get(f"{API}/categories/tree").json()["data"]

    assert second == first
