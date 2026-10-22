"""add payment_discrepancies

Revision ID: d4b7e1f93a62
Revises: c2d8f4a6b913
Create Date: 2026-10-22 10:00:00.000000+00:00

One row per difference the daily reconciliation found between a payment and
the provider's record of it (app/models/payment_discrepancy.py), unique by
payment, kind and subject so that running twice records it once.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4b7e1f93a62'
down_revision: Union[str, None] = 'c2d8f4a6b913'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'payment_discrepancies',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('payment_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('subject', sa.String(), nullable=False),
        sa.Column('ours', sa.String(), nullable=True),
        sa.Column('theirs', sa.String(), nullable=True),
        sa.Column('detected_at', sa.DateTime(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(), nullable=False),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['payment_id'], ['payments.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('payment_id', 'kind', 'subject', name='uix_payment_discrepancy'),
    )
    op.create_index(
        'idx_payment_discrepancy_payment_id', 'payment_discrepancies', ['payment_id'],
        unique=False,
    )
    op.create_index(
        'idx_payment_discrepancy_resolved_at', 'payment_discrepancies', ['resolved_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('idx_payment_discrepancy_resolved_at', table_name='payment_discrepancies')
    op.drop_index('idx_payment_discrepancy_payment_id', table_name='payment_discrepancies')
    op.drop_table('payment_discrepancies')
