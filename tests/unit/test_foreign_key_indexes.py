"""Every foreign key column of the models has an index, and migration
821e97899aca (which adds the missing ones) is wired up correctly.

PostgreSQL does not index referencing columns by itself
(https://www.postgresql.org/docs/current/ddl-constraints.html), so a foreign
key without an index means a sequential scan on every join or delete of the
referenced row. The check reads Base.metadata, so a new foreign key is covered
without touching this file.

The PostgreSQL statements are rendered offline (as_sql); the real server runs
the migration in the whole suite on PostgreSQL and in the up/down/up check.
"""
import importlib.util
import io
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import UniqueConstraint, create_engine, inspect

import app.models  # noqa: F401 registers all tables on Base
from app.models.base import Base

ROOT = Path(__file__).resolve().parents[2]
REVISION = "821e97899aca"


def _migration():
    (path,) = (ROOT / "migrations" / "versions").glob(f"*-{REVISION}_*.py")
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _postgres_sql(step):
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(context):
        getattr(_migration(), step)()
    return buffer.getvalue()


def _index_column_lists(table):
    """Column lists of every full (non-partial) index, including the primary
    key and unique constraints; a partial index does not cover every row."""
    lists = [[c.name for c in table.primary_key.columns]]
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            lists.append([c.name for c in constraint.columns])
    for index in table.indexes:
        if index.dialect_options["postgresql"]["where"] is None:
            lists.append([getattr(e, "name", None) for e in index.expressions])
    return lists


def test_every_foreign_key_column_has_an_index():
    unindexed = []
    for table in Base.metadata.sorted_tables:
        lists = _index_column_lists(table)
        for fk in table.foreign_key_constraints:
            columns = [c.name for c in fk.columns]
            # a composite index whose leading columns are the key also counts
            if not any(columns == indexed[: len(columns)] for indexed in lists):
                unindexed.append(f"{table.name}.{','.join(columns)}")
    assert unindexed == []


def test_there_is_one_head_and_it_follows_the_previous_one():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [REVISION]
    assert _migration().down_revision == "ada27dd39537"


def test_the_migration_creates_the_indexes_the_models_declare():
    declared = {
        index.name: (table.name, [c.name for c in index.columns])
        for table in Base.metadata.sorted_tables
        for index in table.indexes
    }
    for name, (table, columns) in _migration().FOREIGN_KEY_INDEXES.items():
        assert declared[name] == (table, columns)
    assert len(_migration().FOREIGN_KEY_INDEXES) == 4


def test_the_models_no_longer_declare_the_dropped_indexes():
    names = {i.name for t in Base.metadata.sorted_tables for i in t.indexes}
    assert "idx_product_name_description" not in names
    for table in _migration().PRIMARY_KEY_DUPLICATES:
        assert f"ix_{table}_id" not in names


def test_upgrade_builds_and_drops_concurrently_outside_a_transaction():
    sql = _postgres_sql("upgrade")

    for name in _migration().FOREIGN_KEY_INDEXES:
        assert f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON" in sql
    assert "DROP INDEX CONCURRENTLY IF EXISTS idx_product_name_description" in sql
    assert sql.count("DROP INDEX CONCURRENTLY IF EXISTS ix_") == 9
    assert "COMMIT" in sql  # autocommit_block ends the open transaction first
    assert "CREATE INDEX IF NOT EXISTS" not in sql


def test_downgrade_restores_the_old_indexes_and_drops_the_new_ones():
    sql = _postgres_sql("downgrade")

    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_product_name_description ON products (name, description)" in sql
    assert sql.count("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_") == 9
    for name in _migration().FOREIGN_KEY_INDEXES:
        assert f"DROP INDEX CONCURRENTLY IF EXISTS {name}" in sql


@pytest.mark.parametrize("step", ["upgrade", "downgrade"])
# SQLAlchemy cannot reflect the expression index on lower(email) and says so;
# the index names this test reads are all plain ones.
@pytest.mark.filterwarnings("ignore:Skipped unsupported reflection of expression-based index")
def test_on_sqlite_it_runs_plainly(step):
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        Base.metadata.create_all(connection)
        if step == "upgrade":
            # a database as the previous revision left it
            connection.exec_driver_sql("DROP INDEX idx_order_item_order_id")
            connection.exec_driver_sql("CREATE INDEX idx_product_name_description ON products (name, description)")
            for table in _migration().PRIMARY_KEY_DUPLICATES:
                connection.exec_driver_sql(f"CREATE INDEX ix_{table}_id ON {table} (id)")
        with Operations.context(MigrationContext.configure(connection)):
            getattr(_migration(), step)()
        names = {
            index["name"]
            for table in inspect(connection).get_table_names()
            for index in inspect(connection).get_indexes(table)
        }
    engine.dispose()
    if step == "upgrade":
        assert "idx_order_item_order_id" in names
        assert "idx_product_name_description" not in names
        assert not names & {f"ix_{t}_id" for t in _migration().PRIMARY_KEY_DUPLICATES}
    else:
        assert "idx_product_name_description" in names
        assert "idx_order_item_order_id" not in names
