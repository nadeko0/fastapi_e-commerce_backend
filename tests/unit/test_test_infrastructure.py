"""The test database wiring that the migrated routes rely on."""

import asyncio
import os

from conftest import _remove_stale_test_dirs, _stale_test_database_names
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


def test_remove_stale_test_dirs(tmp_path):
    now = 1_000_000.0
    max_age_seconds = 3600.0
    old_time = now - 7200.0
    fresh_time = now - 60.0

    old_dir = tmp_path / "ecommerce-tests-x"
    old_dir.mkdir()
    os.utime(old_dir, (old_time, old_time))

    fresh_dir = tmp_path / "ecommerce-tests-fresh"
    fresh_dir.mkdir()
    os.utime(fresh_dir, (fresh_time, fresh_time))

    unrelated_dir = tmp_path / "unrelated-dir"
    unrelated_dir.mkdir()
    os.utime(unrelated_dir, (old_time, old_time))

    plain_file = tmp_path / "ecommerce-tests-file"
    plain_file.write_text("data")
    os.utime(plain_file, (old_time, old_time))

    _remove_stale_test_dirs(tmp_path, now, max_age_seconds)

    assert not old_dir.exists()
    assert fresh_dir.exists()
    assert unrelated_dir.exists()
    assert plain_file.exists()


def test_remove_stale_test_dirs_missing_root(tmp_path):
    missing_root = tmp_path / "missing_dir"
    _remove_stale_test_dirs(missing_root, now=1_000_000.0, max_age_seconds=3600.0)


def test_stale_test_database_names():
    now = 1_000_000.0
    max_age_seconds = 3600.0

    old_valid = f"ecom_pgtests_{int(now - 7200)}_0123456789ab"
    fresh_valid = f"ecom_pgtests_{int(now - 60)}_0123456789ab"
    old_format = "ecom_pgtests_abcdef123456"
    invalid_case = "ecom_pgtests_123_XYZ"
    unrelated = "ecommerce"

    names = [old_valid, fresh_valid, old_format, invalid_case, unrelated]
    assert _stale_test_database_names(names, now, max_age_seconds) == [old_valid]
    assert _stale_test_database_names([old_valid], now, max_age_seconds) == [old_valid]
    assert _stale_test_database_names([fresh_valid], now, max_age_seconds) == []
    assert _stale_test_database_names([old_format], now, max_age_seconds) == []
    assert _stale_test_database_names([invalid_case], now, max_age_seconds) == []
    assert _stale_test_database_names([unrelated], now, max_age_seconds) == []
