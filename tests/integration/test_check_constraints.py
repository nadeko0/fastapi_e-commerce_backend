"""CHECK constraints on money and stock (F-21).

The application keeps these invariants itself (schemas, the conditional
stock UPDATE), but a bug, a script or hand-written SQL could still store a
negative stock, a zero quantity or a refund larger than the payment. The
database refuses that now. The test schema is built from the models, so the
constraints live in the models' __table_args__; the migration
(76d14f9f12e5) adds the same ones to an existing database and must agree.
"""

import importlib.util
import os
import subprocess
import sys
from decimal import Decimal

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, text
from sqlalchemy.exc import IntegrityError

from app.models.address import Address
from app.models.base import Base
from app.models.category import Category
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.payment import Payment
from app.models.product import Product, ProductVariant
from app.models.user import User

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REVISION = "76d14f9f12e5"
_PREVIOUS = "ed98c07e2fbc"


def _migration():
    versions = os.path.join(_ROOT, "migrations", "versions")
    (name,) = [f for f in os.listdir(versions) if f"-{_REVISION}_" in f]
    spec = importlib.util.spec_from_file_location("check_constraints_migration", os.path.join(versions, name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def order_graph(db_session):
    """One of everything, valid; tests then break one value."""
    user = User(
        email="buyer@example.com", hashed_password="x", full_name="Buyer",
        gdpr_consent=True, privacy_policy_accepted=True,
    )
    category = Category(name="Electronics", level=0, path=[])
    db_session.add_all([user, category])
    db_session.flush()
    product = Product(
        name="Widget", description="A fine widget", price=Decimal("9.99"), stock_quantity=5,
        images=[], characteristics={}, category_id=category.id,
    )
    db_session.add(product)
    db_session.flush()
    variant = ProductVariant(product_id=product.id, sku="W-1", stock_quantity=1)
    address = Address(
        user_id=user.id, street="1 Main St", city="Town", state="State", postal_code="12345",
        country="DE", address_type="home",
    )
    db_session.add_all([variant, address])
    db_session.flush()
    order = Order(user_id=user.id, total_amount=Decimal("9.99"), shipping_address_id=address.id)
    db_session.add(order)
    db_session.flush()
    item = OrderItem(order_id=order.id, product_id=product.id, quantity=1, price_at_time=Decimal("9.99"))
    payment = Payment(
        order_id=order.id, payment_intent_id="pi_1", amount=999, amount_refunded=0, currency="usd",
    )
    db_session.add_all([item, payment])
    db_session.commit()
    return {"product": product, "variant": variant, "order": order, "item": item, "payment": payment}


@pytest.mark.parametrize(
    "row,column,value",
    [
        ("product", "price", Decimal("-0.01")),
        ("product", "stock_quantity", -1),
        ("variant", "stock_quantity", -1),
        ("variant", "price_override", Decimal("-1.00")),
        ("item", "quantity", 0),
        ("item", "quantity", -1),
        ("item", "price_at_time", Decimal("-0.01")),
        ("order", "total_amount", Decimal("-0.01")),
        ("payment", "amount", -1),
        ("payment", "amount_refunded", -1),
        ("payment", "amount_refunded", 1000),  # more than the 999 paid
    ],
)
def test_the_database_refuses_a_value_the_application_would_never_write(db_session, order_graph, row, column, value):
    setattr(order_graph[row], column, value)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.parametrize(
    "row,column,value",
    [
        ("product", "price", Decimal("0.00")),
        ("product", "stock_quantity", 0),
        ("variant", "price_override", None),
        ("variant", "price_override", Decimal("0.00")),
        ("order", "total_amount", Decimal("0.00")),
        ("payment", "amount_refunded", 999),  # a full refund
    ],
)
def test_the_boundaries_are_allowed(db_session, order_graph, row, column, value):
    setattr(order_graph[row], column, value)
    db_session.commit()


def _model_check_constraints():
    found = {}
    for table in Base.metadata.sorted_tables:
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint) and constraint.name in _migration().CONSTRAINTS_BY_NAME:
                found[constraint.name] = (table.name, str(constraint.sqltext))
    return found


def test_the_migration_adds_exactly_the_models_constraints():
    migration = _migration()
    assert {name: (table, condition) for table, name, condition in migration.CONSTRAINTS} == _model_check_constraints()
    assert migration.down_revision == _PREVIOUS


def test_the_migration_refuses_to_run_over_rows_that_break_a_constraint(db_session, order_graph):
    # Postgres would refuse the ALTER TABLE too, but one constraint at a
    # time and without saying how many rows; the migration checks all of
    # them first and names each, before changing anything. The test schema
    # already has the constraints, so they are switched off while the rows a
    # database from before this migration could hold are written: SQLite is
    # told to look away; on PostgreSQL, where the schema outlives the test,
    # the two constraints are dropped and put back afterwards.
    postgres = db_session.get_bind().dialect.name == "postgresql"
    broken = {"ck_product_stock_non_negative", "ck_payment_refunded_within_amount"}
    constraints = [c for c in _migration().CONSTRAINTS if c[1] in broken]
    if postgres:
        for table, name, _condition in constraints:
            db_session.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT {name}"))
    else:
        db_session.execute(text("PRAGMA ignore_check_constraints = ON"))
    try:
        db_session.execute(text("UPDATE products SET stock_quantity = -3"))
        db_session.execute(text("UPDATE payments SET amount_refunded = amount + 1"))
        db_session.commit()
        if not postgres:
            db_session.execute(text("PRAGMA ignore_check_constraints = OFF"))

        connection = db_session.connection()
        with (
            Operations.context(MigrationContext.configure(connection)),
            pytest.raises(RuntimeError) as error,
        ):
            _migration().upgrade()
        message = str(error.value)
        assert "ck_product_stock_non_negative" in message and "products" in message
        assert "ck_payment_refunded_within_amount" in message
        assert "1 row" in message
    finally:
        if postgres:
            db_session.rollback()
            db_session.execute(text("UPDATE products SET stock_quantity = 0"))
            db_session.execute(text("UPDATE payments SET amount_refunded = 0"))
            for table, name, condition in constraints:
                db_session.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({condition})"))
            db_session.commit()


def test_the_migration_finds_nothing_wrong_with_valid_rows(db_session, order_graph):
    assert _migration().find_violations(db_session.connection()) == []


def _offline_sql(*args):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args, "--sql"],
        cwd=_ROOT, env=dict(os.environ), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_upgrade_adds_and_downgrade_drops_every_constraint_on_postgres():
    # Offline mode renders the Postgres DDL without a database.
    upgrade = _offline_sql("upgrade", f"{_PREVIOUS}:{_REVISION}")
    downgrade = _offline_sql("downgrade", f"{_REVISION}:{_PREVIOUS}")
    for table, name, condition in _migration().CONSTRAINTS:
        assert f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({condition})" in upgrade
        assert f"ALTER TABLE {table} DROP CONSTRAINT {name}" in downgrade
