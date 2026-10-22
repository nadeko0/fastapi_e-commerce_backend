"""store webhook event payloads and processing state

Revision ID: d4e9a1c7b265
Revises: d4b7e1f93a62
Create Date: 2026-10-22 11:00:00.000000+00:00

Stripe events are stored first and applied afterwards by a Celery task, so
webhook_events gains the event payload and its processing state (status,
attempts, last_error, processed_at, updated_at). Every row that exists
already was applied in the request that recorded it: it becomes
"processed", with no payload.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4e9a1c7b265'
down_revision: Union[str, None] = 'd4b7e1f93a62'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('webhook_events', sa.Column('payload', sa.Text(), nullable=True))
    # The server defaults fill the rows that exist; they are dropped again
    # below because the application always supplies these values itself.
    op.add_column('webhook_events', sa.Column(
        'status', sa.String(), nullable=False, server_default='processed'))
    op.add_column('webhook_events', sa.Column(
        'attempts', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('webhook_events', sa.Column('last_error', sa.Text(), nullable=True))
    op.add_column('webhook_events', sa.Column('processed_at', sa.DateTime(), nullable=True))
    op.add_column('webhook_events', sa.Column('updated_at', sa.DateTime(), nullable=True))
    op.execute("UPDATE webhook_events SET processed_at = created_at, updated_at = created_at")
    with op.batch_alter_table('webhook_events') as batch:
        batch.alter_column('updated_at', existing_type=sa.DateTime(), nullable=False)
        batch.alter_column('status', existing_type=sa.String(), server_default=None)
        batch.alter_column('attempts', existing_type=sa.Integer(), server_default=None)
    op.create_index(
        'idx_webhook_event_status_updated', 'webhook_events', ['status', 'updated_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('idx_webhook_event_status_updated', table_name='webhook_events')
    with op.batch_alter_table('webhook_events') as batch:
        batch.drop_column('updated_at')
        batch.drop_column('processed_at')
        batch.drop_column('last_error')
        batch.drop_column('attempts')
        batch.drop_column('status')
        batch.drop_column('payload')
