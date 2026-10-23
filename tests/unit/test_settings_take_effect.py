"""Settings that name a behaviour must produce it: DB_POOL_RECYCLE,
SQL_DEBUG (engines are built at import, hence the subprocess, as in
test_config.py) and REDIS_POOL_SIZE."""

import os
import subprocess
import sys

from app.core.config import settings
from app.services.redis import RedisService

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _engines(**env):
    code = (
        "from app.core.database import engine, async_engine; import app.tasks as t;"
        "print(engine.pool._recycle, bool(engine.echo), async_engine.sync_engine.pool._recycle,"
        " bool(async_engine.echo), t.engine.pool._recycle, bool(t.engine.echo))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=_ROOT, env={**os.environ, **env},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


def test_defaults_are_unchanged():
    assert _engines() == ["3600", "False", "3600", "False", "3600", "False"]


def test_db_pool_recycle_reaches_every_engine():
    recycle = _engines(DB_POOL_RECYCLE="123")
    assert recycle[0] == recycle[2] == recycle[4] == "123"


def test_sql_debug_turns_on_statement_echo_on_every_engine():
    echo = _engines(SQL_DEBUG="true")
    assert echo[1] == echo[3] == echo[5] == "True"


def test_sync_redis_pool_uses_redis_pool_size(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_POOL_SIZE", 7)
    monkeypatch.setattr(RedisService, "_instance", None)
    monkeypatch.setattr(RedisService, "_pool", None)
    RedisService()
    assert RedisService._pool.max_connections == 7
