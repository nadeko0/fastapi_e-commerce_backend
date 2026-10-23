"""index foreign keys, drop useless indexes

Revision ID: 821e97899aca
Revises: ada27dd39537
Create Date: 2026-10-23 10:00:00.000000+00:00

PostgreSQL does not index the referencing column of a foreign key by itself
("it is often a good idea to index the referencing columns too",
https://www.postgresql.org/docs/current/ddl-constraints.html#DDL-CONSTRAINTS-FK).
order_items.order_id, order_items.product_id, order_items.variant_id and
orders.shipping_address_id had none: loading the items of an order and the
"has this product been ordered" check scanned the whole order_items table.
A test (tests/unit/test_foreign_key_indexes.py) fails when a foreign key of the
models has no index, so a future one cannot be forgotten.

Dropped as useless:
- idx_product_name_description, a btree on (name, description). A btree entry
  has to fit in about a third of a page, so a product with a long description
  could not be stored ("index row requires 10024 bytes, maximum size is
  8191"), and the search is ILIKE '%x%', which a btree cannot serve.
- ix_<table>_id on the nine tables whose primary key already has the same
  index; the primary key index serves every lookup by id.

On PostgreSQL the indexes are built and dropped CONCURRENTLY, so writes to the
tables are not blocked
(https://www.postgresql.org/docs/current/sql-createindex.html#SQL-CREATEINDEX-CONCURRENTLY).
That cannot run inside a transaction block, hence Alembic's autocommit_block
(https://alembic.sqlalchemy.org/en/latest/api/runtime.html), which commits the
migrations before it first. A failed CONCURRENTLY build leaves an invalid
index behind: drop it by hand and run the migration again (IF NOT EXISTS /
IF EXISTS keep a rerun from failing on the ones that did get built). SQLite
builds them plainly.

The downgrade rebuilds the dropped indexes; rebuilding the (name, description)
index fails if a product with a long description has been stored since.
"""
import contextlib
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '821e97899aca'
down_revision: Union[str, None] = 'ada27dd39537'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# index name -> (table, columns)
FOREIGN_KEY_INDEXES = {
    'idx_order_item_order_id': ('order_items', ['order_id']),
    'idx_order_item_product_id': ('order_items', ['product_id']),
    'idx_order_item_variant_id': ('order_items', ['variant_id']),
    'idx_order_shipping_address_id': ('orders', ['shipping_address_id']),
}

NAME_DESCRIPTION_INDEX = (
    'idx_product_name_description', 'products', ['name', 'description']
)

# ix_<table>_id, a second index on the primary key
PRIMARY_KEY_DUPLICATES = [
    'addresses', 'categories', 'order_items', 'orders', 'payments',
    'product_variants', 'products', 'users', 'webhook_events',
]


def _autocommit():
    if op.get_bind().dialect.name == 'postgresql':
        return op.get_context().autocommit_block()
    return contextlib.nullcontext()


def _create(name: str, table: str, columns: list) -> None:
    concurrently = op.get_bind().dialect.name == 'postgresql'
    op.create_index(
        name, table, columns, unique=False, if_not_exists=True,
        postgresql_concurrently=concurrently,
    )


def _drop(name: str, table: str) -> None:
    concurrently = op.get_bind().dialect.name == 'postgresql'
    op.drop_index(
        name, table_name=table, if_exists=True,
        postgresql_concurrently=concurrently,
    )


def upgrade() -> None:
    with _autocommit():
        for name, (table, columns) in FOREIGN_KEY_INDEXES.items():
            _create(name, table, columns)
        name, table, _columns = NAME_DESCRIPTION_INDEX
        _drop(name, table)
        for table in PRIMARY_KEY_DUPLICATES:
            _drop(op.f(f'ix_{table}_id'), table)


def downgrade() -> None:
    with _autocommit():
        for table in PRIMARY_KEY_DUPLICATES:
            _create(op.f(f'ix_{table}_id'), table, ['id'])
        _create(*NAME_DESCRIPTION_INDEX)
        for name, (table, _columns) in FOREIGN_KEY_INDEXES.items():
            _drop(name, table)
