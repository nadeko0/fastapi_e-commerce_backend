"""add payment_refunds

Revision ID: a7c3e5b19d42
Revises: 3f6c2a9d1b7e
Create Date: 2026-10-21 12:00:00.000000+00:00

One row per provider refund of a charge (app/models/payment_ledger.py),
unique by the provider's refund id, so that a refund reported more than
once (the /refund call that made it, its webhooks, a retried request) is
counted once in payments.amount_refunded. Existing refunds have no rows:
their amounts are already in amount_refunded, and their ids were never
stored.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7c3e5b19d42'
down_revision: Union[str, None] = '3f6c2a9d1b7e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'payment_refunds',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('payment_id', sa.Integer(), nullable=False),
        sa.Column('refund_id', sa.String(), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('failure_reason', sa.String(), nullable=True),
        sa.Column('requires_manual_review', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('amount > 0', name='ck_payment_refund_amount_positive'),
        sa.ForeignKeyConstraint(['payment_id'], ['payments.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('refund_id'),
    )
    op.create_index('idx_payment_refund_payment_id', 'payment_refunds', ['payment_id'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_payment_refund_payment_id', table_name='payment_refunds')
    op.drop_table('payment_refunds')
