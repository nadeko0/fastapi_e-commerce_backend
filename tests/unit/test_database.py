import asyncio

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.core.database as database_module


@pytest.fixture
def sqlite_session_factory(monkeypatch):
    """Point app.core.database's session_scope at an isolated
    in-memory SQLite engine so session_scope's commit/rollback/close paths
    can be exercised directly, without touching the real Postgres engine
    configured from settings.DATABASE_URI."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_local = sessionmaker(autoflush=False, bind=engine)
    monkeypatch.setattr(database_module, "SessionLocal", session_local)
    yield session_local
    engine.dispose()


def test_session_scope_commits_on_clean_exit(sqlite_session_factory):
    with database_module.session_scope() as session:
        session.execute(text("CREATE TABLE t (id INTEGER)"))
        session.execute(text("INSERT INTO t VALUES (1)"))

    with database_module.session_scope() as session:
        assert session.execute(text("SELECT COUNT(*) FROM t")).scalar() == 1


def test_session_scope_rolls_back_and_raises_http_exception_on_db_error(
    sqlite_session_factory,
):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info, database_module.session_scope() as session:
        session.execute(text("SELECT * FROM this_table_does_not_exist"))

    assert exc_info.value.status_code == 503
    assert exc_info.value.headers.get("Retry-After") == "5"
    assert exc_info.value.detail == "The database is temporarily unavailable, please retry"


@pytest.fixture
def async_sqlite_session_factory(monkeypatch):
    """Point get_async_db at an
    isolated in-memory aiosqlite engine. StaticPool keeps the one in-memory
    connection alive across the separate sessions a test opens."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async_engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    session_local = async_sessionmaker(bind=async_engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_local)
    yield session_local
    # Closes the pooled aiosqlite connection (and its worker thread).
    asyncio.run(async_engine.dispose())


def test_get_async_db_commits_on_clean_exit(async_sqlite_session_factory):
    import asyncio

    async def scenario():
        gen = database_module.get_async_db()
        session = await anext(gen)
        await session.execute(text("CREATE TABLE t (id INTEGER)"))
        await session.execute(text("INSERT INTO t VALUES (1)"))
        # Exhausting the generator is what FastAPI does after the response:
        # it runs the code after `yield`, i.e. the commit.
        with pytest.raises(StopAsyncIteration):
            await anext(gen)

        async with async_sqlite_session_factory() as other:
            return (await other.execute(text("SELECT COUNT(*) FROM t"))).scalar()

    assert asyncio.run(scenario()) == 1


def test_get_async_db_rolls_back_and_raises_http_exception_on_db_error(
    async_sqlite_session_factory,
):
    import asyncio

    from fastapi import HTTPException
    from sqlalchemy.exc import OperationalError

    async def scenario():
        async with async_sqlite_session_factory() as setup:
            await setup.execute(text("CREATE TABLE t (id INTEGER)"))
            await setup.commit()

        gen = database_module.get_async_db()
        session = await anext(gen)
        await session.execute(text("INSERT INTO t VALUES (1)"))
        try:
            await session.execute(text("SELECT * FROM this_table_does_not_exist"))
        except OperationalError as e:
            # FastAPI throws the route's exception into the dependency at
            # its `yield`; athrow() is that same hand-off.
            with pytest.raises(HTTPException) as exc_info:
                await gen.athrow(e)

        async with async_sqlite_session_factory() as other:
            # The insert happened before the failing statement, in the same
            # transaction: the rollback must have taken it with it.
            rows = (await other.execute(text("SELECT COUNT(*) FROM t"))).scalar()
        return exc_info.value, rows

    http_exc, rows = asyncio.run(scenario())
    assert http_exc.status_code == 503
    assert http_exc.headers.get("Retry-After") == "5"
    assert http_exc.detail == "The database is temporarily unavailable, please retry"
    assert rows == 0


def test_get_async_db_rolls_back_and_raises_500_on_unhandled_integrity_error(
    async_sqlite_session_factory,
):
    import asyncio

    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError

    async def scenario():
        async with async_sqlite_session_factory() as setup:
            await setup.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
            await setup.commit()

        gen = database_module.get_async_db()
        session = await anext(gen)
        await session.execute(text("INSERT INTO t VALUES (1)"))
        err = IntegrityError("INSERT INTO t VALUES (1)", {}, Exception("duplicate"))
        with pytest.raises(HTTPException) as exc_info:
            await gen.athrow(err)

        async with async_sqlite_session_factory() as other:
            rows = (await other.execute(text("SELECT COUNT(*) FROM t"))).scalar()
        return exc_info.value, rows

    http_exc, rows = asyncio.run(scenario())
    assert http_exc.status_code == 500
    assert "Retry-After" not in (http_exc.headers or {})
    assert http_exc.detail == "A database error occurred"
    assert rows == 0


def test_is_database_unavailable_error():
    from sqlalchemy.exc import (
        DataError,
        DBAPIError,
        DisconnectionError,
        IntegrityError,
        InterfaceError,
        OperationalError,
        ProgrammingError,
    )
    from sqlalchemy.exc import (
        TimeoutError as SATimeoutError,
    )

    # Database unavailable signals -> True
    assert database_module.is_database_unavailable_error(
        OperationalError("SELECT 1", {}, Exception("connection refused"))
    )
    assert database_module.is_database_unavailable_error(
        InterfaceError("SELECT 1", {}, Exception("closed"))
    )
    assert database_module.is_database_unavailable_error(
        SATimeoutError("pool timeout")
    )
    assert database_module.is_database_unavailable_error(
        DisconnectionError("disconnected")
    )
    dbapi_inval = DBAPIError("SELECT 1", {}, Exception("lost"))
    dbapi_inval.connection_invalidated = True
    assert database_module.is_database_unavailable_error(dbapi_inval)

    # Programmer / data / constraint errors -> False
    dbapi_normal = DBAPIError("SELECT 1", {}, Exception("bad"))
    dbapi_normal.connection_invalidated = False
    assert not database_module.is_database_unavailable_error(dbapi_normal)
    assert not database_module.is_database_unavailable_error(
        IntegrityError("INSERT", {}, Exception("duplicate"))
    )
    assert not database_module.is_database_unavailable_error(
        ProgrammingError("SELECT", {}, Exception("syntax"))
    )
    assert not database_module.is_database_unavailable_error(
        DataError("INSERT", {}, Exception("value too large"))
    )
