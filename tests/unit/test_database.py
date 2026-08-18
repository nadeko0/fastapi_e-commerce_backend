import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.core.database as database_module


@pytest.fixture
def sqlite_session_factory(monkeypatch):
    """Point app.core.database's session_scope/get_db at an isolated
    in-memory SQLite engine so session_scope's commit/rollback/close paths
    can be exercised directly, without touching the real Postgres engine
    configured from settings.DATABASE_URI."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database_module, "SessionLocal", session_local)
    return session_local


def test_get_db_yields_a_working_session(sqlite_session_factory):
    gen = database_module.get_db()
    session = next(gen)

    assert session.execute(text("SELECT 1")).scalar() == 1

    gen.close()


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

    with pytest.raises(HTTPException) as exc_info:
        with database_module.session_scope() as session:
            session.execute(text("SELECT * FROM this_table_does_not_exist"))

    assert exc_info.value.status_code == 500
