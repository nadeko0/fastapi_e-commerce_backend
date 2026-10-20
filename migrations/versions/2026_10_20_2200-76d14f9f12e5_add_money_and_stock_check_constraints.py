"""add CHECK constraints on money and stock

Revision ID: 76d14f9f12e5
Revises: ed98c07e2fbc
Create Date: 2026-10-20 22:00:00.000000+00:00

The same constraints are declared in the models' __table_args__ (the test
suite builds its schema from those and checks that this list matches).

Existing rows are checked first, for every constraint, and the upgrade
stops before changing anything if one is broken, naming the constraint and
how many rows break it. Postgres would refuse the ALTER TABLE as well, but
one constraint at a time and without saying which rows. Those rows have to
be corrected by hand: what a negative stock or an over-refund should have
been is not something a migration can guess. In offline mode (--sql) there
is no database to check, and Postgres's own refusal is what remains.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = '76d14f9f12e5'
down_revision: Union[str, None] = 'ed98c07e2fbc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, constraint name, condition). Payments are in cents (integers),
# the rest Numeric(10, 2). amount >= 0, not > 0: a payment row is written
# before the provider is asked, and the provider is what rejects zero.
CONSTRAINTS = [
    ("products", "ck_product_price_non_negative", "price >= 0"),
    ("products", "ck_product_stock_non_negative", "stock_quantity >= 0"),
    ("product_variants", "ck_product_variant_stock_non_negative", "stock_quantity >= 0"),
    (
        "product_variants",
        "ck_product_variant_price_override_non_negative",
        "price_override IS NULL OR price_override >= 0",
    ),
    ("order_items", "ck_order_item_quantity_positive", "quantity > 0"),
    ("order_items", "ck_order_item_price_non_negative", "price_at_time >= 0"),
    ("orders", "ck_order_total_non_negative", "total_amount >= 0"),
    ("payments", "ck_payment_amount_non_negative", "amount >= 0"),
    ("payments", "ck_payment_refunded_non_negative", "amount_refunded >= 0"),
    ("payments", "ck_payment_refunded_within_amount", "amount_refunded <= amount"),
]
CONSTRAINTS_BY_NAME = {name: (table, condition) for table, name, condition in CONSTRAINTS}


def find_violations(connection) -> list:
    """One line per constraint that existing rows break. NOT (condition)
    is NULL, so not counted, for a NULL column, exactly as a CHECK lets
    NULL through."""
    violations = []
    for table, name, condition in CONSTRAINTS:
        count = connection.execute(
            text(f"SELECT count(*) FROM {table} WHERE NOT ({condition})")
        ).scalar()
        if count:
            violations.append(
                f"{table}: {count} row{'s' if count != 1 else ''} break {name} ({condition})"
            )
    return violations


def upgrade() -> None:
    # as_sql: offline mode (--sql), where there is no database to ask.
    if not op.get_context().as_sql:
        violations = find_violations(op.get_bind())
        if violations:
            raise RuntimeError(
                "Cannot add the CHECK constraints, existing rows break them; correct "
                "these rows and run the migration again (nothing was changed):\n  "
                + "\n  ".join(violations)
            )
    for table, name, condition in CONSTRAINTS:
        op.create_check_constraint(name, table, condition)


def downgrade() -> None:
    for table, name, _condition in reversed(CONSTRAINTS):
        op.drop_constraint(name, table, type_="check")
