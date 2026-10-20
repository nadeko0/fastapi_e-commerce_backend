"""add currency to orders

Revision ID: e1959653f4a2
Revises: 76d14f9f12e5
Create Date: 2026-10-20 10:00:00.000000+00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e1959653f4a2'
down_revision: Union[str, None] = '76d14f9f12e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing orders get USD: it was the only currency the store used (the
    # Checkout Session default, and what every client sent to /pay). The
    # server default only backfills them and is then dropped, so a new row
    # that does not set the currency fails instead of silently getting one;
    # the application sets it from STORE_CURRENCY (app/models/order.py).
    op.add_column(
        'orders',
        sa.Column('currency', sa.String(length=3), nullable=False, server_default='USD'),
    )
    op.alter_column('orders', 'currency', server_default=None)


def downgrade() -> None:
    op.drop_column('orders', 'currency')
