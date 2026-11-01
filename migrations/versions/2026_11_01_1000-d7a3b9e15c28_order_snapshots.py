"""snapshot line and shipping data at checkout

Revision ID: d7a3b9e15c28
Revises: c5e2a8f4b196
Create Date: 2026-10-25 10:00:00.000000+00:00

An order line used to keep only price_at_time; product_name and variant_sku
were read from the live product and variant, so renaming a product or
changing a variant's SKU rewrote the history of orders already placed.
Orders also referenced addresses directly, so editing an address rewrote
delivered and historical orders.
This migration adds snapshot columns to order_items and orders, and backfills
existing orders' shipping snapshot from addresses.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd7a3b9e15c28'
down_revision: Union[str, None] = 'c5e2a8f4b196'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('order_items', sa.Column('product_name_snapshot', sa.String(), nullable=True))
    op.add_column('order_items', sa.Column('variant_sku_snapshot', sa.String(), nullable=True))
    op.add_column(
        'order_items',
        sa.Column(
            'variant_attributes_snapshot',
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'),
            nullable=True,
        ),
    )
    op.add_column('orders', sa.Column('ship_to_street', sa.String(), nullable=True))
    op.add_column('orders', sa.Column('ship_to_city', sa.String(), nullable=True))
    op.add_column('orders', sa.Column('ship_to_state', sa.String(), nullable=True))
    op.add_column('orders', sa.Column('ship_to_postal_code', sa.String(), nullable=True))
    op.add_column('orders', sa.Column('ship_to_country', sa.String(), nullable=True))
    op.add_column('orders', sa.Column('ship_to_address_type', sa.String(), nullable=True))
    op.add_column('orders', sa.Column('ship_to_delivery_phone', sa.String(), nullable=True))
    op.add_column('orders', sa.Column('ship_to_delivery_instructions', sa.String(), nullable=True))

    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE orders
            SET
                ship_to_street = addresses.street,
                ship_to_city = addresses.city,
                ship_to_state = addresses.state,
                ship_to_postal_code = addresses.postal_code,
                ship_to_country = addresses.country,
                ship_to_address_type = addresses.address_type,
                ship_to_delivery_phone = addresses.delivery_phone,
                ship_to_delivery_instructions = addresses.delivery_instructions
            FROM addresses
            WHERE orders.shipping_address_id = addresses.id
            """
        )
    )


def downgrade() -> None:
    op.drop_column('orders', 'ship_to_delivery_instructions')
    op.drop_column('orders', 'ship_to_delivery_phone')
    op.drop_column('orders', 'ship_to_address_type')
    op.drop_column('orders', 'ship_to_country')
    op.drop_column('orders', 'ship_to_postal_code')
    op.drop_column('orders', 'ship_to_state')
    op.drop_column('orders', 'ship_to_city')
    op.drop_column('orders', 'ship_to_street')
    op.drop_column('order_items', 'variant_attributes_snapshot')
    op.drop_column('order_items', 'variant_sku_snapshot')
    op.drop_column('order_items', 'product_name_snapshot')
