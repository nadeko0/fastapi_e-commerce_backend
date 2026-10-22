"""Migration d4e9a1c7b265: webhook_events keeps the event payload and its
processing state.

Run on a throwaway SQLite database through Alembic's own Operations, on a
webhook_events table as the initial schema made it, with a row in it.
"""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[2]
REVISION = "d4e9a1c7b265"


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
        conn.execute(text(
            "CREATE TABLE webhook_events (id INTEGER PRIMARY KEY, "
            "event_id VARCHAR NOT NULL UNIQUE, event_type VARCHAR NOT NULL, "
            "created_at DATETIME NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO webhook_events (event_id, event_type, created_at) "
            "VALUES ('evt_old', 'payment_intent.succeeded', '2026-10-01 12:00:00')"
        ))
        yield conn
    engine.dispose()


def _run(connection, step):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_migration(), step)()


def _columns(connection):
    return {c["name"] for c in inspect(connection).get_columns("webhook_events")}


def test_down_revision_is_the_previous_head():
    assert _migration().down_revision == "d4b7e1f93a62"


def test_upgrade_turns_existing_events_into_processed_ones(connection):
    _run(connection, "upgrade")

    row = connection.execute(text(
        "SELECT status, attempts, payload, last_error, processed_at, updated_at "
        "FROM webhook_events"
    )).one()
    assert row.status == "processed"
    assert row.attempts == 0
    assert row.payload is None and row.last_error is None
    assert row.processed_at == "2026-10-01 12:00:00"
    assert row.updated_at == "2026-10-01 12:00:00"
    assert "idx_webhook_event_status_updated" in {
        i["name"] for i in inspect(connection).get_indexes("webhook_events")
    }


def test_upgrade_leaves_no_default_for_the_application_to_rely_on(connection):
    _run(connection, "upgrade")

    defaults = {c["name"]: c["default"] for c in inspect(connection).get_columns("webhook_events")}
    assert defaults["status"] is None
    assert defaults["attempts"] is None
    assert defaults["updated_at"] is None


def test_downgrade_restores_the_old_columns(connection):
    _run(connection, "upgrade")
    _run(connection, "downgrade")

    assert _columns(connection) == {"id", "event_id", "event_type", "created_at"}
