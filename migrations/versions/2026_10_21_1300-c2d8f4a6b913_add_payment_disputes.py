"""add payment_disputes

Revision ID: c2d8f4a6b913
Revises: a7c3e5b19d42
Create Date: 2026-10-21 13:00:00.000000+00:00

One row per provider dispute (chargeback or inquiry) of a charge
(app/models/payment_ledger.py), unique by the provider's dispute id. An
open dispute puts its order on hold; a closed one keeps its outcome.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c2d8f4a6b913'
down_revision: Union[str, None] = 'a7c3e5b19d42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'payment_disputes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('payment_id', sa.Integer(), nullable=False),
        sa.Column('dispute_id', sa.String(), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('reason', sa.String(), nullable=True),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('amount >= 0', name='ck_payment_dispute_amount_non_negative'),
        sa.ForeignKeyConstraint(['payment_id'], ['payments.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dispute_id'),
    )
    op.create_index(
        'idx_payment_dispute_payment_id', 'payment_disputes', ['payment_id'], unique=False
    )


def downgrade() -> None:
    op.drop_index('idx_payment_dispute_payment_id', table_name='payment_disputes')
    op.drop_table('payment_disputes')
