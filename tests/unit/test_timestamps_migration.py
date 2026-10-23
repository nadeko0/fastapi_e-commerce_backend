"""Migration ada27dd39537: every DateTime column becomes TIMESTAMP WITH TIME
ZONE on PostgreSQL, and nothing happens on SQLite.

The PostgreSQL statements are rendered offline (Alembic's as_sql mode); the
real server is exercised by the whole suite on PostgreSQL and by the
up/down/up check of the migrations.
"""
import importlib.util
import io
import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

import app.models  # noqa: F401 registers all tables on Base
from app.core.clock import UTCDateTime
from app.models.base import Base

ROOT = Path(__file__).resolve().parents[2]
REVISION = "ada27dd39537"


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


def _model_timestamp_columns():
    found = {}
    for table in Base.metadata.sorted_tables:
        names = [c.name for c in table.columns if isinstance(c.type, UTCDateTime)]
        if names:
            found[table.name] = set(names)
    return found


def test_it_follows_the_previous_one_in_a_single_chain():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert script.get_revision(REVISION) is not None
    assert _migration().down_revision == "d4e9a1c7b265"


def test_it_covers_exactly_the_datetime_columns_of_the_models():
    migrated = {t: set(c) for t, c in _migration().TIMESTAMP_COLUMNS.items()}
    assert migrated == _model_timestamp_columns()


def test_every_model_datetime_column_is_time_zone_aware():
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            impl = getattr(column.type, "impl", column.type)
            if impl.__class__.__name__ == "DateTime":
                assert isinstance(column.type, UTCDateTime), f"{table.name}.{column.name}"
                assert column.type.impl.timezone is True


def test_upgrade_converts_every_column_reading_the_stored_value_as_utc():
    sql = _postgres_sql("upgrade")

    assert "SET LOCAL TIME ZONE 'UTC'" in sql
    assert "SET LOCAL lock_timeout = '10s'" in sql
    expected = sum(len(c) for c in _migration().TIMESTAMP_COLUMNS.values())
    alters = re.findall(
        r"ALTER COLUMN (\w+) TYPE TIMESTAMP WITH TIME ZONE USING \1 AT TIME ZONE 'UTC'", sql
    )
    assert len(alters) == expected == 33
    assert sql.index("SET LOCAL") < sql.index("ALTER TABLE")


def test_downgrade_goes_back_to_timestamp_without_time_zone():
    sql = _postgres_sql("downgrade")

    assert "SET LOCAL TIME ZONE 'UTC'" in sql
    alters = re.findall(
        r"ALTER COLUMN (\w+) TYPE TIMESTAMP WITHOUT TIME ZONE USING \1 AT TIME ZONE 'UTC'", sql
    )
    assert len(alters) == 33


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
