"""The MetaData naming convention and migration c5e2a8f4b196 (which renames
the foreign keys and unique constraints PostgreSQL had named itself).

Sources: SQLAlchemy "Configuring Constraint Naming Conventions"
(https://docs.sqlalchemy.org/en/20/core/constraints.html#configuring-constraint-naming-conventions)
and Alembic "The Importance of Naming Constraints"
(https://alembic.sqlalchemy.org/en/latest/naming.html). The real server is
exercised by tests/integration/test_postgres_constraint_names.py and by the
up/down/up check of the migrations.
"""
import importlib.util
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, create_engine

import app.models  # noqa: F401 registers all tables on Base
from app.models.base import Base

ROOT = Path(__file__).resolve().parents[2]
REVISION = "c5e2a8f4b196"


def _migration():
    (path,) = (ROOT / "migrations" / "versions").glob(f"*-{REVISION}_*.py")
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _constraints(kind):
    return [
        (table, c)
        for table in Base.metadata.sorted_tables
        for c in table.constraints
        if isinstance(c, kind)
    ]


def test_it_is_in_the_chain_after_the_previous_revision():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert script.get_revision(REVISION) is not None
    assert _migration().down_revision == "b6f1c8d2a047"


def test_every_foreign_key_is_named_by_the_convention():
    fks = _constraints(ForeignKeyConstraint)
    assert fks
    for table, fk in fks:
        (column,) = fk.column_keys
        assert fk.name == f"fk_{table.name}_{column}_{fk.referred_table.name}"


def test_every_unique_constraint_is_named():
    for table, uq in _constraints(UniqueConstraint):
        assert uq.name, table.name
        assert len(uq.name) <= 63


def test_primary_keys_keep_the_postgresql_default_name():
    for table in Base.metadata.sorted_tables:
        assert table.primary_key.name == f"{table.name}_pkey"


def test_check_constraint_names_stay_the_explicit_ones():
    checks = _constraints(CheckConstraint)
    assert len(checks) >= 16
    for table, ck in checks:
        assert ck.name.startswith("ck_"), (table.name, ck.name)
        assert not ck.name.startswith(f"ck_{table.name}_ck_"), ck.name


def test_the_migration_lists_exactly_the_foreign_keys_and_uniques_of_the_models():
    module = _migration()
    model_fks = {
        (table.name, fk.column_keys[0], fk.referred_table.name)
        for table, fk in _constraints(ForeignKeyConstraint)
    }
    assert set(module.FOREIGN_KEYS) == model_fks
    assert len(module.FOREIGN_KEYS) == len(model_fks)
    model_uq_names = {uq.name for _, uq in _constraints(UniqueConstraint)}
    for table, column in module.UNIQUE_CONSTRAINTS:
        assert module._uq_new(table, column) in model_uq_names
    # every unique constraint named by the convention is migrated
    assert {n for n in model_uq_names if n.startswith("uq_")} == {
        module._uq_new(*uq) for uq in module.UNIQUE_CONSTRAINTS
    }


@pytest.mark.parametrize("step", ["upgrade", "downgrade"])
def test_on_sqlite_it_does_nothing(step):
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        Base.metadata.create_all(connection)
        before = connection.exec_driver_sql("SELECT sql FROM sqlite_master ORDER BY name").all()
        with Operations.context(MigrationContext.configure(connection)):
            getattr(_migration(), step)()
        after = connection.exec_driver_sql("SELECT sql FROM sqlite_master ORDER BY name").all()
    engine.dispose()
    assert before == after
