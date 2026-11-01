"""Migration d7a3b9e15c28: order line and shipping snapshots."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]
REVISION = "d7a3b9e15c28"


def _migration():
    (path,) = (ROOT / "migrations" / "versions").glob(f"*{REVISION}*.py")
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def connection():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE addresses (
                id INTEGER PRIMARY KEY,
                street VARCHAR NOT NULL,
                city VARCHAR NOT NULL,
                state VARCHAR NOT NULL,
                postal_code VARCHAR NOT NULL,
                country VARCHAR NOT NULL,
                address_type VARCHAR NOT NULL,
                delivery_phone VARCHAR,
                delivery_instructions VARCHAR
            )
        """))
        conn.execute(text("""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                shipping_address_id INTEGER NOT NULL REFERENCES addresses(id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE order_items (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES orders(id)
            )
        """))
        yield conn
    engine.dispose()


def _run(connection, step):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_migration(), step)()


def test_migration_upgrade_and_backfill(connection):
    connection.execute(text("""
        INSERT INTO addresses (id, street, city, state, postal_code, country, address_type, delivery_phone, delivery_instructions)
        VALUES (1, '10 Downing St', 'London', 'London', 'SW1A 2AA', 'GB', 'home', '+442079250918', 'Ring bell')
    """))
    connection.execute(text("""
        INSERT INTO orders (id, shipping_address_id) VALUES (1, 1)
    """))

    _run(connection, "upgrade")

    row = connection.execute(text("""
        SELECT ship_to_street, ship_to_city, ship_to_country, ship_to_delivery_phone
        FROM orders WHERE id = 1
    """)).one()
    assert row.ship_to_street == "10 Downing St"
    assert row.ship_to_city == "London"
    assert row.ship_to_country == "GB"
    assert row.ship_to_delivery_phone == "+442079250918"

    order_item_cols = [r[1] for r in connection.execute(text("PRAGMA table_info(order_items)")).all()]
    assert "product_name_snapshot" in order_item_cols
    assert "variant_sku_snapshot" in order_item_cols
    assert "variant_attributes_snapshot" in order_item_cols
