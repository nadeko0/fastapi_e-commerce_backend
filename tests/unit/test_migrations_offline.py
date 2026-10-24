"""`alembic upgrade head --sql` renders the whole chain without a database.

Offline mode is how a DBA gets the SQL to review or to run by hand. A
migration that queries the database (the duplicate-email check of
3f6c2a9d1b7e) cannot answer there, so it must emit its check as SQL instead
of running it.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _alembic(*args):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT, env=dict(os.environ), capture_output=True, text=True, timeout=120,
    )


def test_upgrade_head_renders_as_sql_without_a_database():
    result = _alembic("upgrade", "head", "--sql")

    assert result.returncode == 0, result.stderr[-2000:]
    sql = result.stdout
    assert "CREATE TABLE users" in sql
    assert "CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email))" in sql
    assert "UPDATE alembic_version SET version_num=" in sql


def test_the_offline_script_carries_the_duplicate_email_check_before_the_index():
    sql = _alembic("upgrade", "e1959653f4a2:3f6c2a9d1b7e", "--sql").stdout

    check = sql.index("RAISE EXCEPTION")
    assert check < sql.index("CREATE UNIQUE INDEX uq_users_email_lower")
    assert "GROUP BY lower(email) HAVING count(*) > 1" in sql
    # Like the online message, the script names no mailbox.
    assert "@" not in sql[sql.index("DO $$"):sql.index("CREATE UNIQUE INDEX")]
