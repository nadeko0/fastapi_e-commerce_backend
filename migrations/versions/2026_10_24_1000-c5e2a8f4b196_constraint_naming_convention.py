"""name foreign keys and unique constraints by the naming convention

Revision ID: c5e2a8f4b196
Revises: b6f1c8d2a047
Create Date: 2026-10-24 10:00:00.000000+00:00

app.models.base.NAMING_CONVENTION gives every constraint a deterministic name
(SQLAlchemy "Configuring Constraint Naming Conventions", Alembic "The
Importance of Naming Constraints"). The foreign keys and the two unique
constraints created before it carried the names PostgreSQL generated
(<table>_<column>_fkey, <table>_<column>_key). This migration renames them to
the convention names, so the database and the models agree and a later
migration can drop or alter a constraint by a name that is known.

Nothing else is touched: primary keys already are <table>_pkey, CHECK
constraints and the idx_*/uix_* objects have explicit names, ix_* indexes
already match "ix_%(column_0_label)s".

The current name of each object is read from the database catalog; a missing
object stops the migration with a clear error instead of renaming a guess.
ALTER TABLE ... RENAME CONSTRAINT is a catalog-only change (a short lock, no
rewrite); renaming a unique constraint renames its index too. SQLite: no-op.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c5e2a8f4b196'
down_revision: Union[str, None] = 'b6f1c8d2a047'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, referencing column, referenced table) of every foreign key
FOREIGN_KEYS = [
    ('addresses', 'user_id', 'users'),
    ('categories', 'parent_id', 'categories'),
    ('order_items', 'order_id', 'orders'),
    ('order_items', 'product_id', 'products'),
    ('order_items', 'variant_id', 'product_variants'),
    ('orders', 'shipping_address_id', 'addresses'),
    ('orders', 'user_id', 'users'),
    ('payment_discrepancies', 'payment_id', 'payments'),
    ('payment_disputes', 'payment_id', 'payments'),
    ('payment_refunds', 'payment_id', 'payments'),
    ('payments', 'order_id', 'orders'),
    ('product_variants', 'product_id', 'products'),
    ('products', 'category_id', 'categories'),
]
# (table, column) of every single-column unique constraint that had no name
UNIQUE_CONSTRAINTS = [
    ('payment_disputes', 'dispute_id'),
    ('payment_refunds', 'refund_id'),
]


def _fk_new(table: str, column: str, referred: str) -> str:
    return f'fk_{table}_{column}_{referred}'


def _fk_old(table: str, column: str, referred: str) -> str:
    return f'{table}_{column}_fkey'


def _uq_new(table: str, column: str) -> str:
    return f'uq_{table}_{column}'


def _uq_old(table: str, column: str) -> str:
    return f'{table}_{column}_key'


def _rename(table: str, old: str, new: str) -> None:
    if old != new:
        op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{old}" TO "{new}"')


def _current_names(inspector: sa.Inspector, table: str) -> dict:
    """{('fk', column, referred): name, ('uq', column): name} for one table."""
    found = {}
    for fk in inspector.get_foreign_keys(table):
        if len(fk['constrained_columns']) == 1:
            found[('fk', fk['constrained_columns'][0], fk['referred_table'])] = fk['name']
    for uq in inspector.get_unique_constraints(table):
        if len(uq['column_names']) == 1:
            found[('uq', uq['column_names'][0])] = uq['name']
    return found


def _rename_all(old_fk, new_fk, old_uq, new_uq) -> None:
    """Rename each object from the name the catalog reports; the name it is
    expected to have now (old_*) is checked, so a drifted database fails."""
    if op.get_context().as_sql:
        if op.get_context().dialect.name == 'postgresql':
            for table, column, referred in FOREIGN_KEYS:
                _rename(table, old_fk(table, column, referred), new_fk(table, column, referred))
            for table, column in UNIQUE_CONSTRAINTS:
                _rename(table, old_uq(table, column), new_uq(table, column))
        return
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    inspector = sa.inspect(bind)
    plan = []
    for table, column, referred in FOREIGN_KEYS:
        key = ('fk', column, referred)
        current = _current_names(inspector, table).get(key)
        expected = old_fk(table, column, referred)
        if current not in (expected, new_fk(table, column, referred)):
            raise RuntimeError(
                f'foreign key {table}.{column} -> {referred}: expected the name '
                f'{expected!r}, the database has {current!r}'
            )
        plan.append((table, current, new_fk(table, column, referred)))
    for table, column in UNIQUE_CONSTRAINTS:
        current = _current_names(inspector, table).get(('uq', column))
        expected = old_uq(table, column)
        if current not in (expected, new_uq(table, column)):
            raise RuntimeError(
                f'unique constraint {table}.{column}: expected the name '
                f'{expected!r}, the database has {current!r}'
            )
        plan.append((table, current, new_uq(table, column)))
    for table, current, target in plan:
        _rename(table, current, target)


def upgrade() -> None:
    _rename_all(_fk_old, _fk_new, _uq_old, _uq_new)


def downgrade() -> None:
    _rename_all(_fk_new, _fk_old, _uq_new, _uq_old)
