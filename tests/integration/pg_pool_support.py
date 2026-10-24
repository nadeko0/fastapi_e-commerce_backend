"""The application's own connection pool, pointed at the test database.

tests/conftest.py gives the suite NullPool engines (a TestClient request may
run on its own event loop, and an async psycopg connection cannot outlive the
loop that opened it), so the pool settings of app/core/database.py are never
exercised. app_pool builds an engine with exactly those settings (pool size,
overflow, pre-ping, recycle, the server-side timeouts) against the throwaway
PostgreSQL database and makes the application's session factory use it. A
`with TestClient(...)` block keeps one loop for all its requests, so pooled
connections are reused across them, as under uvicorn.
"""

import contextlib

from conftest import TEST_DB_URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.core.database as database_module

APPLICATION_NAME = "app-pool-test"


@contextlib.contextmanager
def app_pool(monkeypatch, client, pre_ping=True):
    connect_args = {**database_module._build_connect_args(TEST_DB_URL), "application_name": APPLICATION_NAME}
    kwargs = {**database_module._engine_kwargs, "connect_args": connect_args, "pool_pre_ping": pre_ping, "echo": False}
    engine = create_async_engine(TEST_DB_URL, **kwargs)
    monkeypatch.setattr(
        database_module,
        "AsyncSessionLocal",
        async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    try:
        yield engine
    finally:
        # On the loop the connections were opened on (the client's portal).
        client.portal.call(engine.dispose)
