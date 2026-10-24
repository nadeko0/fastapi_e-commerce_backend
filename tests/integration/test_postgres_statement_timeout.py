"""statement_timeout on PostgreSQL: a statement that runs too long is cancelled,
the request gets the generic database error, and the connection goes back to
the pool usable.

The application connects with `-c statement_timeout=60000` (app/core/database.py).
Waiting a minute is not a test, so the route here lowers it for its own
transaction with SET LOCAL, the same mechanism with a different number: the
server cancels the statement with QueryCanceled and get_async_db answers what
it answers to any SQLAlchemyError.

Skipped unless TEST_DATABASE_URL names a server; CI's test-postgres job runs it.
"""

import logging
import time
from typing import Any

import pytest
from conftest import requires_postgres
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pg_pool_support import app_pool
from sqlalchemy import text

from app.core.database import DbSession
from app.core.errors import register_error_handlers

pytestmark = requires_postgres


@pytest.fixture
def probe_app():
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/slow")
    async def slow(db: DbSession) -> Any:
        await db.execute(text("SET LOCAL statement_timeout = '300ms'"))
        await db.execute(text("SELECT pg_sleep(30) /* secret-marker */"))

    @app.get("/state")
    async def state(db: DbSession) -> Any:
        row = (await db.execute(text("SELECT pg_backend_pid(), current_setting('statement_timeout')"))).one()
        return {"pid": row[0], "statement_timeout": row[1]}

    return app


def test_a_statement_over_the_timeout_is_cancelled_and_the_connection_stays_usable(
    probe_app, monkeypatch, caplog
):
    caplog.set_level(logging.ERROR, logger="app.core.database")
    with TestClient(probe_app, raise_server_exceptions=False) as client, app_pool(monkeypatch, client) as engine:
        first = client.get("/state").json()

        started = time.monotonic()
        response = client.get("/slow")
        elapsed = time.monotonic() - started

        assert response.status_code == 500
        assert response.json()["detail"] == "A database error occurred"
        assert "pg_sleep" not in response.text and "secret-marker" not in response.text
        assert elapsed < 10  # cancelled after ~0.3 s, not run to its 30 s
        logged = [record for record in caplog.records if record.name == "app.core.database" and record.exc_info]
        assert logged, "the failure is logged server-side"
        assert "canceling statement due to statement timeout" in str(logged[0].exc_info[1])
        assert engine.pool.checkedout() == 0

        after = client.get("/state").json()
        # The same backend came back from the pool, and SET LOCAL ended with
        # the cancelled transaction: the configured 60 s is in force again.
        assert after["pid"] == first["pid"]
        assert after["statement_timeout"] == first["statement_timeout"] == "1min"
