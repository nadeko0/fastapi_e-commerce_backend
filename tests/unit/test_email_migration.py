"""Migration 3f6c2a9d1b7e: the unique index on lower(email).

Run against a throwaway SQLite database through Alembic's own Operations,
the way `alembic upgrade` runs it, without the rest of the chain (the
initial schema is written for Postgres). Only a users table with the one
column that matters is needed.
"""
import importlib.util
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[2]
REVISION = "3f6c2a9d1b7e"


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
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR NOT NULL UNIQUE)"))
        yield conn
    engine.dispose()


def _run(connection, step):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_migration(), step)()


def _indexes(connection):
    # From sqlite_master: SQLAlchemy's reflection skips expression indexes.
    return set(connection.execute(text(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'users'"
    )).scalars())


def _insert(connection, *emails):
    for email in emails:
        connection.execute(text("INSERT INTO users (email) VALUES (:email)"), {"email": email})


def test_upgrade_creates_the_index_and_the_database_then_refuses_case_duplicates(connection):
    _insert(connection, "jane@example.com", "Other@Example.com")

    _run(connection, "upgrade")

    assert "uq_users_email_lower" in _indexes(connection)
    with pytest.raises(IntegrityError):
        _insert(connection, "JANE@example.com")


def test_upgrade_stops_with_a_clear_message_when_case_duplicates_exist(connection):
    _insert(connection, "jane@example.com", "Jane@Example.com", "bob@example.com", "BOB@example.com")

    with pytest.raises(RuntimeError) as refused:
        _run(connection, "upgrade")

    message = str(refused.value)
    assert "2 email address(es) belong to more than one account" in message
    assert "Merge or rename those accounts" in message
    # No mailbox in a message that ends up in deploy logs.
    assert "example.com" not in message
    assert "uq_users_email_lower" not in _indexes(connection)
    # Nothing was changed to make the index fit.
    stored = connection.execute(text("SELECT email FROM users ORDER BY id")).scalars().all()
    assert stored == ["jane@example.com", "Jane@Example.com", "bob@example.com", "BOB@example.com"]


def test_downgrade_drops_the_index(connection):
    _run(connection, "upgrade")
    _run(connection, "downgrade")

    assert "uq_users_email_lower" not in _indexes(connection)
    _insert(connection, "jane@example.com", "JANE@example.com")


def test_the_migration_chain_still_has_a_single_head():
    # Two migrations written in parallel against the same parent would give
    # `alembic upgrade head` two heads to choose from, and it refuses.
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert len(ScriptDirectory.from_config(config).get_heads()) == 1
