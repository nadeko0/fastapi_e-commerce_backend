"""On PostgreSQL every foreign key, unique constraint, primary key and index
carries the name the models' naming convention gives it, and migration
c5e2a8f4b196 renames in both directions and refuses a database that lacks an
object.

(Migrated database == models is also asserted by test_postgres_only.py, which
compares the schema built by `alembic upgrade head` with this one and runs
Alembic's compare_metadata.)
"""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from conftest import engine, requires_postgres
from sqlalchemy import text

import app.models  # noqa: F401 registers all tables on Base
from app.models.base import Base

pytestmark = requires_postgres

ROOT = Path(__file__).resolve().parents[2]
REVISION = "c5e2a8f4b196"


def _migration():
    (path,) = (ROOT / "migrations" / "versions").glob(f"*-{REVISION}_*.py")
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _db_names(connection, kind):
    return {
        row[0]
        for row in connection.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE connamespace = 'public'::regnamespace "
                "AND contype = :kind"
            ),
            {"kind": kind},
        )
    }


def _model_names(kind):
    return {
        c.name
        for table in Base.metadata.sorted_tables
        for c in table.constraints
        if type(c).__name__ == kind
    }


def test_constraint_names_equal_the_convention():
    with engine.connect() as connection:
        assert _db_names(connection, "f") == _model_names("ForeignKeyConstraint")
        assert _db_names(connection, "u") == _model_names("UniqueConstraint")
        assert _db_names(connection, "p") - {"alembic_version_pkc"} == {f"{t.name}_pkey" for t in Base.metadata.sorted_tables}


def test_every_index_name_equals_the_model_index_name():
    expected = {index.name for table in Base.metadata.sorted_tables for index in table.indexes}
    with engine.connect() as connection:
        actual = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT i.indexname FROM pg_indexes i WHERE i.schemaname = 'public' AND NOT EXISTS ("
                    "SELECT 1 FROM pg_constraint c WHERE c.conname = i.indexname "
                    "AND c.connamespace = 'public'::regnamespace)"
                )
            )
        }
    assert actual == expected


def _run(connection, step):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_migration(), step)()


def test_downgrade_restores_the_generated_names_and_upgrade_the_convention_names():
    module = _migration()
    connection = engine.connect()
    transaction = connection.begin()
    try:
        _run(connection, "downgrade")
        names = _db_names(connection, "f") | _db_names(connection, "u")
        assert {module._fk_old(*fk) for fk in module.FOREIGN_KEYS} <= names
        assert {module._uq_old(*uq) for uq in module.UNIQUE_CONSTRAINTS} <= names
        assert not any(n.startswith(("fk_", "uq_")) for n in names)

        _run(connection, "upgrade")
        names = _db_names(connection, "f") | _db_names(connection, "u")
        assert {module._fk_new(*fk) for fk in module.FOREIGN_KEYS} <= names
        assert {module._uq_new(*uq) for uq in module.UNIQUE_CONSTRAINTS} <= names
        assert not ({module._fk_old(*fk) for fk in module.FOREIGN_KEYS} | {module._uq_old(*uq) for uq in module.UNIQUE_CONSTRAINTS}) & names
    finally:
        transaction.rollback()
        connection.close()


def test_a_database_missing_an_expected_object_fails_clearly():
    connection = engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(text('ALTER TABLE "orders" DROP CONSTRAINT "fk_orders_user_id_users"'))
        with pytest.raises(RuntimeError, match=r"orders\.user_id -> users"):
            _run(connection, "upgrade")
    finally:
        transaction.rollback()
        connection.close()
