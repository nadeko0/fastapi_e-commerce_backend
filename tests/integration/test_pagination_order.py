"""Every paginated list has a total order (F-15).

LIMIT/OFFSET pages only fit together when the ORDER BY puts the rows in one
unique order: "it is important to use an ORDER BY clause that constrains the
result rows into a unique order. Otherwise you will get an unpredictable
subset of the query's rows" (PostgreSQL docs, LIMIT and OFFSET). So each
list ends its ORDER BY in the primary key, in the direction of the main
sort, and a list with no sort option is ordered by the key, ascending.

Two kinds of test: the SQL each endpoint sends (its ORDER BY ends in the id
column), and paging through rows that share the sort value one row per page.
"""
import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from conftest import engine
from sqlalchemy import event

import app.core.database as database_module
from app.core.security import create_access_token, get_password_hash
from app.models.address import Address
from app.models.category import Category
from app.models.enums import UserRole
from app.models.order import Order
from app.models.product import Product
from app.models.user import User

API = "/api/v1"
SAME_MOMENT = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)


# -- Setup ---------------------------------------------------------------------------

def _user(db_session, email, role=UserRole.CLIENT):
    user = User(
        email=email, hashed_password=get_password_hash("Str0ngPass1"), full_name="Someone",
        role=role, gdpr_consent=True, privacy_policy_accepted=True, consent_history=[],
        is_active=True, is_email_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    return user, {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest.fixture
def category(db_session):
    category = Category(name="Shared", level=0, path=[])
    db_session.add(category)
    db_session.commit()
    return category


@pytest.fixture
def products(db_session, category):
    """Five products that share a price (and a name prefix)."""
    rows = [
        Product(name="Same", description="Same description", price=Decimal("10.00"),
                stock_quantity=1, images=["https://example.com/p.png"], characteristics={},
                category_id=category.id)
        for _ in range(5)
    ]
    db_session.add_all(rows)
    db_session.commit()
    return sorted(row.id for row in rows)


@pytest.fixture
def buyer(db_session):
    return _user(db_session, "buyer@example.com")


@pytest.fixture
def admin_headers(db_session):
    return _user(db_session, "admin@example.com", role=UserRole.ADMIN)[1]


@pytest.fixture
def addresses(db_session, buyer):
    user, _ = buyer
    rows = [
        Address(user_id=user.id, street=f"{i} Main St", city="Town", state="ST",
                postal_code="10001", country="US")
        for i in range(4)
    ]
    db_session.add_all(rows)
    db_session.commit()
    return sorted(row.id for row in rows)


@pytest.fixture
def orders(db_session, buyer, addresses):
    """Four orders of one customer, all created at the same instant."""
    user, _ = buyer
    rows = [
        Order(user_id=user.id, total_amount=Decimal("10.00"), shipping_address_id=addresses[0],
              created_at=SAME_MOMENT, updated_at=SAME_MOMENT)
        for _ in range(4)
    ]
    db_session.add_all(rows)
    db_session.commit()
    return sorted(row.id for row in rows)


# -- Helpers -------------------------------------------------------------------------

def _paged_ids(client, url, headers=None, params=None):
    """The ids of every row, read one row per page."""
    seen = []
    for page in range(1, 30):
        response = client.get(url, params={**(params or {}), "page": page, "size": 1}, headers=headers)
        assert response.status_code == 200, response.text
        rows = response.json()["items"]
        if not rows:
            return seen
        seen.extend(row["id"] for row in rows)
    raise AssertionError("the pages never ended")


def _order_by_terms(client, url, headers=None, params=None):
    """The ORDER BY terms of the one statement of the request that has a LIMIT."""
    statements = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    # The sync engine serves the sync routes, the async one the async routes.
    async_engine = database_module.AsyncSessionLocal.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", _record)
    event.listen(async_engine, "before_cursor_execute", _record)
    try:
        response = client.get(url, params=params, headers=headers)
    finally:
        event.remove(engine, "before_cursor_execute", _record)
        event.remove(async_engine, "before_cursor_execute", _record)
    assert response.status_code == 200, response.text

    paged = [s for s in statements if re.search(r"\bLIMIT\b", s, re.I)]
    assert len(paged) == 1, paged
    match = re.search(r"\bORDER BY\b(.*?)(?:\bLIMIT\b|\bOFFSET\b|$)", paged[0], re.I | re.S)
    assert match, f"no ORDER BY in: {paged[0]}"
    return [term.strip() for term in match.group(1).split(",")]


def _assert_ends_in_id(terms, direction):
    assert re.fullmatch(rf'(?:\w+\.)?"?id"?(?: {direction})?', terms[-1], re.I), terms
    # ASC is the default: an explicit DESC is what has to be there.
    assert (direction == "DESC") == terms[-1].upper().endswith(" DESC"), terms


# -- The SQL -------------------------------------------------------------------------

@pytest.mark.parametrize(
    "params,direction",
    [
        ({}, "ASC"),
        ({"sort_by": "price_asc"}, "ASC"),
        ({"sort_by": "price_desc"}, "DESC"),
        ({"sort_by": "name_asc"}, "ASC"),
        ({"sort_by": "name_desc"}, "DESC"),
        ({"sort_by": "created_at_asc"}, "ASC"),
        ({"sort_by": "created_at_desc"}, "DESC"),
    ],
)
def test_product_list_ends_its_order_by_in_the_id(client, products, params, direction):
    _assert_ends_in_id(_order_by_terms(client, f"{API}/products", params=params), direction)


def test_product_search_ends_its_order_by_in_the_id(client, products):
    _assert_ends_in_id(
        _order_by_terms(client, f"{API}/products/search", params={"query": "Same"}), "ASC")


def test_category_list_ends_its_order_by_in_the_id(client, category):
    _assert_ends_in_id(_order_by_terms(client, f"{API}/categories"), "ASC")


def test_address_list_ends_its_order_by_in_the_id(client, buyer, addresses):
    _assert_ends_in_id(_order_by_terms(client, f"{API}/users/addresses", headers=buyer[1]), "ASC")


def test_order_list_of_a_customer_ends_its_order_by_in_the_id(client, buyer, orders):
    terms = _order_by_terms(client, f"{API}/users/orders", headers=buyer[1])

    assert "created_at" in terms[0] and terms[0].upper().endswith(" DESC")
    _assert_ends_in_id(terms, "DESC")


@pytest.mark.parametrize(
    "params,direction",
    [
        ({}, "DESC"),
        ({"sort_by": "created_at_desc"}, "DESC"),
        ({"sort_by": "total_amount_asc"}, "ASC"),
        ({"sort_by": "status_desc"}, "DESC"),
    ],
)
def test_admin_order_list_ends_its_order_by_in_the_id(client, admin_headers, orders, params, direction):
    _assert_ends_in_id(
        _order_by_terms(client, f"{API}/admin/orders", headers=admin_headers, params=params), direction)


# -- Paging through rows that tie ----------------------------------------------------

@pytest.mark.parametrize("params", [{}, {"sort_by": "price_asc"}, {"sort_by": "name_asc"}])
def test_products_with_one_price_come_once_each_in_id_order(client, products, params):
    assert _paged_ids(client, f"{API}/products", params=params) == products


def test_products_sorted_descending_break_ties_descending(client, products):
    seen = _paged_ids(client, f"{API}/products", params={"sort_by": "price_desc"})

    assert seen == sorted(products, reverse=True)


def test_search_pages_come_once_each(client, products):
    assert _paged_ids(client, f"{API}/products/search", params={"query": "Same"}) == products


def test_categories_page_once_each(client, db_session):
    rows = [Category(name=f"Cat {i}", level=0, path=[]) for i in range(4)]
    db_session.add_all(rows)
    db_session.commit()

    assert _paged_ids(client, f"{API}/categories") == sorted(row.id for row in rows)


def test_addresses_page_once_each(client, buyer, addresses):
    assert _paged_ids(client, f"{API}/users/addresses", headers=buyer[1]) == addresses


def test_orders_created_at_one_instant_page_once_each_newest_id_first(client, buyer, orders):
    seen = _paged_ids(client, f"{API}/users/orders", headers=buyer[1])

    assert seen == sorted(orders, reverse=True)


@pytest.mark.parametrize(
    "params,descending",
    [
        ({}, True),
        ({"sort_by": "total_amount_asc"}, False),
        ({"sort_by": "total_amount_desc"}, True),
    ],
)
def test_admin_orders_created_at_one_instant_page_once_each(client, admin_headers, orders, params, descending):
    seen = _paged_ids(client, f"{API}/admin/orders", headers=admin_headers, params=params)

    assert seen == sorted(orders, reverse=descending)
