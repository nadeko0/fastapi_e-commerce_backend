"""store the request fingerprint next to the Idempotency-Key

Revision ID: b6f1c8d2a047
Revises: 821e97899aca
Create Date: 2026-10-23 11:00:00.000000+00:00

draft-ietf-httpapi-idempotency-key-header-08, section 2.4 (fingerprint) and
2.7 (a key reused with a different payload: 422,
https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/).
orders.idempotency_fingerprint and payments.idempotency_fingerprint hold the
SHA-256 (hex) of the request that first carried the key. Nullable: rows stored
before this migration have none and are replayed as before, without a check.
Adding a nullable column without a default is a catalog-only change on
PostgreSQL (no table rewrite).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b6f1c8d2a047'
down_revision: Union[str, None] = '821e97899aca'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('idempotency_fingerprint', sa.String(length=64), nullable=True))
    op.add_column('payments', sa.Column('idempotency_fingerprint', sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column('payments', 'idempotency_fingerprint')
    op.drop_column('orders', 'idempotency_fingerprint')
