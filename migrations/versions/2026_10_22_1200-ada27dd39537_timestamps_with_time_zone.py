"""timestamps with time zone

Revision ID: ada27dd39537
Revises: d4e9a1c7b265
Create Date: 2026-10-22 12:00:00.000000+00:00

Every DateTime column becomes TIMESTAMP WITH TIME ZONE. The values that exist
were written by datetime.utcnow(), so they are UTC: the conversion reads them
as such (USING col AT TIME ZONE 'UTC', which turns a timestamp without time
zone into one with, taking the given value to be in the named zone;
https://www.postgresql.org/docs/current/functions-datetime.html).

SET LOCAL TIME ZONE 'UTC' makes the session zone UTC for this transaction. Since
PostgreSQL 12 a timestamp <-> timestamptz change then needs no table rewrite
(https://www.postgresql.org/docs/12/release-12.html); indexes on the columns
are still rebuilt. SET LOCAL lock_timeout makes the ALTERs fail fast on a busy
table instead of queueing behind a long transaction (and, behind them, every
other query on the table); ALTER TABLE takes an ACCESS EXCLUSIVE lock
(https://www.postgresql.org/docs/current/sql-altertable.html).

SQLite has no real column types, so there it does nothing. The downgrade
goes back to TIMESTAMP WITHOUT TIME ZONE, keeping the UTC wall-clock value.

Columns that appear in no model but only in the migration history: none. A
test (tests/unit/test_timestamps_migration.py) checks that this list equals the
DateTime columns of the models, so a future column cannot be forgotten.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'ada27dd39537'
down_revision: Union[str, None] = 'd4e9a1c7b265'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# table -> its timestamp columns, as of this revision
TIMESTAMP_COLUMNS = {
    'addresses': ['created_at', 'updated_at'],
    'categories': ['created_at', 'updated_at'],
    'order_items': ['created_at'],
    'orders': ['created_at', 'updated_at'],
    'payment_discrepancies': ['detected_at', 'last_seen_at', 'resolved_at'],
    'payment_disputes': ['closed_at', 'created_at', 'updated_at'],
    'payment_refunds': ['created_at', 'updated_at'],
    'payments': ['created_at', 'updated_at'],
    'product_variants': ['created_at', 'updated_at'],
    'products': ['created_at', 'updated_at'],
    'users': [
        'email_verification_date', 'gdpr_consent_date', 'privacy_policy_accepted_date',
        'marketing_consent_date', 'data_deletion_date', 'last_login',
        'last_activity_date', 'created_at', 'updated_at',
    ],
    'webhook_events': ['processed_at', 'created_at', 'updated_at'],
}


def _convert(timezone: bool) -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute("SET LOCAL TIME ZONE 'UTC'")
    op.execute("SET LOCAL lock_timeout = '10s'")
    for table, columns in TIMESTAMP_COLUMNS.items():
        for column in columns:
            op.alter_column(
                table, column,
                type_=sa.DateTime(timezone=timezone),
                postgresql_using=f"{column} AT TIME ZONE 'UTC'",
            )


def upgrade() -> None:
    _convert(timezone=True)


def downgrade() -> None:
    _convert(timezone=False)
