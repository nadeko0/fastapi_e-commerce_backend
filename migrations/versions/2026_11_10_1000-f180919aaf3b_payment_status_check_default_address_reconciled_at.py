"""payment status CHECK, one default address per user, payments.reconciled_at, new-order index

Revision ID: f180919aaf3b
Revises: d7a3b9e15c28
Create Date: 2026-11-10 10:00:00.000000+00:00

Four schema changes, each declared in the models as well:

- ck_payment_attempt_status: payments.status is one of
  app.models.payment.PAYMENT_STATUSES, as orders.status and
  orders.payment_status already are (ck_order_status, ck_payment_status; the
  latter name is the order's column, hence "attempt" here). Existing rows are
  checked first and the upgrade stops before changing anything if one breaks
  it, naming how many, as 76d14f9f12e5 does: what such a row should have said
  is not something a migration can guess. In offline mode (--sql) there is no
  database to ask, and PostgreSQL's own refusal is what remains.

- uix_user_default_address: at most one active default address per user.
  Two requests that each made an address the default could both commit.
  Existing duplicates are resolved first, without asking: the newest
  default (created_at, then id) of each user stays the default, the others
  stop being one. Nothing is lost, the addresses stay as they are. The
  downgrade drops the index and does not bring the duplicates back.

- payments.reconciled_at: when the reconciliation last compared the payment
  with the provider. The run takes the window's least recently compared
  payments first, so above RECONCILIATION_MAX_PAYMENTS a payment waits for
  a later run instead of never being compared again. NULL (never compared)
  for every existing row, which puts them first. A nullable column without a
  default is a catalog-only change on PostgreSQL (no table rewrite).

- idx_order_new_created_at: orders (created_at) WHERE status = 'new', for
  the unpaid-order expiry sweep (new orders placed before a cutoff), which
  otherwise reads the whole table every five minutes.

The indexes are built plainly, inside the migration's one transaction, not
CONCURRENTLY as 821e97899aca does: that would commit the CHECK and the
default-address fix-up before the rest. A plain build blocks writes to the
table while it runs; on orders and addresses that is short.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f180919aaf3b'
down_revision: Union[str, None] = 'd7a3b9e15c28'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, constraint name, condition), in the shape of 76d14f9f12e5's list.
# The values are written out, not imported: a migration states the schema of
# its own revision, whatever the application says later.
CONSTRAINTS = [
    (
        "payments",
        "ck_payment_attempt_status",
        "status IN ('processing', 'requires_action', 'unknown', 'succeeded', 'failed')",
    ),
]

DEFAULT_INDEX = "uix_user_default_address"
DEFAULT_WHERE = "is_default AND is_active"

NEW_ORDERS_INDEX = "idx_order_new_created_at"
NEW_ORDERS_WHERE = "status = 'new'"

# Every active default but the newest of its user stops being the default.
UNSET_OLDER_DEFAULTS = """
UPDATE addresses SET is_default = false
WHERE is_default AND is_active
AND id <> (
    SELECT newest.id FROM addresses AS newest
    WHERE newest.user_id = addresses.user_id AND newest.is_default AND newest.is_active
    ORDER BY newest.created_at DESC, newest.id DESC
    LIMIT 1
)
"""


def find_violations(connection) -> list:
    """One line per constraint that existing rows break. A NULL status is
    not counted, exactly as a CHECK lets NULL through (the column is NOT
    NULL anyway)."""
    violations = []
    for table, name, condition in CONSTRAINTS:
        count = connection.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE NOT ({condition})")
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
                "Cannot add the CHECK constraint, existing rows break it; correct "
                "these rows and run the migration again (nothing was changed):\n  "
                + "\n  ".join(violations)
            )
    for table, name, condition in CONSTRAINTS:
        op.create_check_constraint(name, table, condition)

    op.execute(UNSET_OLDER_DEFAULTS)
    op.create_index(
        DEFAULT_INDEX, 'addresses', ['user_id'], unique=True,
        postgresql_where=sa.text(DEFAULT_WHERE), sqlite_where=sa.text(DEFAULT_WHERE),
    )

    op.add_column('payments', sa.Column('reconciled_at', sa.DateTime(timezone=True), nullable=True))

    op.create_index(
        NEW_ORDERS_INDEX, 'orders', ['created_at'],
        postgresql_where=sa.text(NEW_ORDERS_WHERE), sqlite_where=sa.text(NEW_ORDERS_WHERE),
    )


def downgrade() -> None:
    op.drop_index(NEW_ORDERS_INDEX, table_name='orders')
    op.drop_column('payments', 'reconciled_at')
    op.drop_index(DEFAULT_INDEX, table_name='addresses')
    for table, name, _condition in reversed(CONSTRAINTS):
        op.drop_constraint(name, table, type_="check")
