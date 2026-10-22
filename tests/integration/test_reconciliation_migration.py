"""
The payment_discrepancies migration (d4b7e1f93a62) builds the table the model
describes, undoes itself, and leaves the migration history with one head.
"""
import importlib.util
import os

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from app.models.base import Base
from app.models.payment_discrepancy import PaymentDiscrepancy

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REVISION = "d4b7e1f93a62"
TABLE = "payment_discrepancies"


def _migration():
    versions = os.path.join(_ROOT, "migrations", "versions")
    (name,) = [f for f in os.listdir(versions) if f"-{_REVISION}_" in f]
    spec = importlib.util.spec_from_file_location("discrepancies_migration", os.path.join(versions, name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def database():
    """SQLite with everything but the new table, as an upgraded database
    would have it."""
    engine = create_engine("sqlite://")
    tables = [t for t in Base.metadata.sorted_tables if t.name != TABLE]
    Base.metadata.create_all(engine, tables=tables)
    yield engine
    engine.dispose()


def _upgrade(engine):
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            _migration().upgrade()


def test_the_migration_builds_the_table_of_the_model(database):
    _upgrade(database)

    inspector = inspect(database)
    model = PaymentDiscrepancy.__table__
    assert {c["name"]: (str(c["type"]), c["nullable"]) for c in inspector.get_columns(TABLE)} == {
        c.name: (str(c.type), c.nullable) for c in model.columns}
    assert {u["name"]: u["column_names"] for u in inspector.get_unique_constraints(TABLE)} == {
        "uix_payment_discrepancy": ["payment_id", "kind", "subject"]}
    assert {i["name"] for i in inspector.get_indexes(TABLE)} == {i.name for i in model.indexes}
    assert [(f["constrained_columns"], f["referred_table"])
            for f in inspector.get_foreign_keys(TABLE)] == [(["payment_id"], "payments")]


def test_the_migration_can_be_undone(database):
    _upgrade(database)

    with database.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            _migration().downgrade()

    assert TABLE not in inspect(database).get_table_names()


def test_the_migration_history_has_one_head():
    config = Config(os.path.join(_ROOT, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(_ROOT, "migrations"))

    heads = ScriptDirectory.from_config(config).get_heads()

    assert len(heads) == 1
