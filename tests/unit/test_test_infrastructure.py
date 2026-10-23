"""The test database wiring that the migrated routes rely on."""

import asyncio

from sqlalchemy import select

import app.core.database as database_module
from app.models.user import User


def test_session_scope_opens_sessions_on_the_test_database(db_session):
    db_session.add(User(email="scope@example.com", hashed_password="x", full_name="Scope"))
    db_session.commit()

    assert database_module.SessionLocal.kw["bind"] is db_session.get_bind()
    with database_module.session_scope() as session:
        assert session.query(User).filter(User.email == "scope@example.com").count() == 1


def test_async_session_factory_sees_rows_committed_by_the_sync_session(
    db_session, async_session_factory
):
    db_session.add(User(email="async@example.com", hashed_password="x", full_name="Async"))
    db_session.commit()

    async def scenario():
        async with async_session_factory() as session:
            return (await session.execute(select(User.email))).scalars().all()

    assert asyncio.run(scenario()) == ["async@example.com"]
