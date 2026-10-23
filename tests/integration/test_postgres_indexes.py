"""Index behaviour that only a real PostgreSQL server shows (skipped on SQLite).

A btree entry has to fit in about a third of a page, and a long description
is part of the (name, description) key of the old idx_product_name_description.
"""
import random
import string

from conftest import requires_postgres
from sqlalchemy import text

from app.models.category import Category
from app.models.product import Product

pytestmark = requires_postgres


def _incompressible(length):
    # Random letters: TOAST compression cannot shrink them below the limit.
    rng = random.Random(7)
    return "".join(rng.choices(string.ascii_letters + string.digits, k=length))


def test_a_product_with_a_very_long_description_can_be_stored(db_session):
    category = Category(name="Books", level=0, path=[])
    db_session.add(category)
    db_session.flush()
    product = Product(
        name="Long read",
        description=_incompressible(10_000),
        price=10,
        stock_quantity=1,
        category_id=category.id,
    )
    db_session.add(product)
    db_session.commit()

    assert db_session.get(Product, product.id) is not None


def test_the_search_query_does_not_use_a_btree_index(db_session):
    plan = "\n".join(
        row[0]
        for row in db_session.execute(
            text(
                "EXPLAIN SELECT id FROM products WHERE name ILIKE '%x%' "
                "OR description ILIKE '%x%'"
            )
        )
    )
    assert "idx_product_name_description" not in plan
