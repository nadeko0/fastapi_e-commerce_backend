"""Product search: the query is text, not a LIKE pattern (F-19).

Both search paths (GET /products/search?query= and the listing's
search_query) match a substring of the name or description. % and _ in
what the visitor typed are characters to find, not wildcards, and \\ is
not an escape: otherwise "%" alone matches the whole catalogue and a
leading % is a full scan whatever the index.
"""

import pytest

from app.models.category import Category
from app.models.product import Product

API = "/api/v1"


@pytest.fixture
def catalog(db_session):
    category = Category(name="Clothing", level=0, path=[])
    db_session.add(category)
    db_session.commit()
    names = ["50% off shirt", "500 off shirt", "a_b widget", "axb widget", "back\\slash", "backslash"]
    for name in names:
        db_session.add(Product(
            name=name,
            description="Nothing to see in this description",
            price="9.99",
            stock_quantity=1,
            images=["https://example.com/p.png"],
            characteristics={},
            category_id=category.id,
        ))
    db_session.commit()
    return names


def _search(client, query):
    response = client.get(f"{API}/products/search", params={"query": query})
    assert response.status_code == 200, response.text
    return sorted(item["name"] for item in response.json()["data"]["items"])


def _listing(client, query):
    response = client.get(f"{API}/products", params={"search_query": query})
    assert response.status_code == 200, response.text
    return sorted(item["name"] for item in response.json()["data"]["items"])


@pytest.mark.parametrize("find", [_search, _listing], ids=["search", "listing"])
@pytest.mark.parametrize(
    "query,expected",
    [
        ("0% o", ["50% off shirt"]),
        ("a_b", ["a_b widget"]),
        ("k\\s", ["back\\slash"]),
        ("%%%", []),
        ("___", []),
    ],
)
def test_wildcards_in_the_query_are_matched_literally(client, catalog, find, query, expected):
    assert find(client, query) == expected


def test_listing_search_of_a_lone_percent_finds_percent_signs_not_everything(client, catalog):
    assert _listing(client, "%") == ["50% off shirt"]
    assert _listing(client, "_") == ["a_b widget"]


@pytest.mark.parametrize(
    "path,param",
    [("/products/search", "query"), ("/products", "search_query")],
)
def test_search_text_is_capped_at_100_characters(client, catalog, path, param):
    assert client.get(f"{API}{path}", params={param: "x" * 100}).status_code == 200
    assert client.get(f"{API}{path}", params={param: "x" * 101}).status_code == 422
