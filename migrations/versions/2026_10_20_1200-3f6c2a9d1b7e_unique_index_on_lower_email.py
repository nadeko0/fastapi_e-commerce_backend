"""unique index on lower(email)

Revision ID: 3f6c2a9d1b7e
Revises: e1959653f4a2
Create Date: 2026-10-20 12:00:00.000000+00:00

One account per mailbox. ix_users_email (initial schema) is unique but
case-sensitive, so "Case@Example.com" and "case@example.com" could be two
accounts. The application now stores emails in lower case and looks them
up by lower(email); this index makes the database refuse the duplicate as
well, and serves those lookups.

Existing rows are not rewritten: an account stored as "Jane@Example.com"
keeps that spelling and is found by lower(email). If the table already
holds addresses that differ only in case, the index cannot be created, and
this migration stops with a message instead of picking one of the accounts
to keep. Which one is the real one is a decision for a person: merge or
rename them, then run the migration again.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3f6c2a9d1b7e'
down_revision: Union[str, None] = 'e1959653f4a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = 'uq_users_email_lower'


def upgrade() -> None:
    duplicates = op.get_bind().execute(sa.text(
        "SELECT lower(email) FROM users GROUP BY lower(email) HAVING count(*) > 1"
    )).fetchall()
    if duplicates:
        # The addresses themselves are not printed: they are personal data,
        # and this message ends up in deploy logs.
        raise RuntimeError(
            f"Cannot create {INDEX_NAME}: {len(duplicates)} email address(es) belong to "
            "more than one account when compared without regard to case. Merge or rename "
            "those accounts, then run the migration again. To list them: "
            "SELECT lower(email), count(*), min(id), max(id) FROM users "
            "GROUP BY lower(email) HAVING count(*) > 1;"
        )
    op.create_index(INDEX_NAME, 'users', [sa.text('lower(email)')], unique=True)


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name='users')
