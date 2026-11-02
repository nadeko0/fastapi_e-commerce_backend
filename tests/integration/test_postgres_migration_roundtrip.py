"""Stepwise round trip verification for every migration on PostgreSQL.

Walks the revision chain from base to head dynamically using Alembic's
ScriptDirectory. For each revision R:
1. Snapshot schema S0;
2. Upgrade to R;
3. Snapshot schema S1;
4. Downgrade one step (to R.down_revision or base);
5. Assert schema equals S0;
6. Upgrade to R again;
7. Assert schema equals S1.

Snapshots read directly from PostgreSQL catalogs (tables, columns, indexes,
constraints, and enum types).
"""

import gc
import os
import time
import uuid

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from conftest import TEST_DATABASE_URL, requires_postgres
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import settings

pytestmark = requires_postgres

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _get_catalog_snapshot(engine) -> dict:
    """Read the current schema state from PostgreSQL system catalogs."""
    with engine.connect() as conn:
        tables = set(
            conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                )
            ).scalars().all()
        )

        columns = set(
            conn.execute(
                text(
                    "SELECT "
                    "  c.relname, "
                    "  a.attname, "
                    "  format_type(a.atttypid, a.atttypmod), "
                    "  a.attnotnull, "
                    "  pg_get_expr(d.adbin, d.adrelid) "
                    "FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid = a.attrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                    "WHERE n.nspname = 'public' "
                    "  AND c.relkind = 'r' "
                    "  AND c.relname <> 'alembic_version' "
                    "  AND a.attnum > 0 "
                    "  AND NOT a.attisdropped"
                )
            ).all()
        )

        indexes = set(
            conn.execute(
                text(
                    "SELECT tablename, indexname, indexdef "
                    "FROM pg_indexes "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                )
            ).all()
        )

        constraints = set(
            conn.execute(
                text(
                    "SELECT c.relname, con.conname, pg_get_constraintdef(con.oid) "
                    "FROM pg_constraint con "
                    "JOIN pg_class c ON c.oid = con.conrelid "
                    "JOIN pg_namespace n ON n.oid = con.connamespace "
                    "WHERE n.nspname = 'public' "
                    "  AND c.relname <> 'alembic_version'"
                )
            ).all()
        )

        enums = set(
            conn.execute(
                text(
                    "SELECT t.typname, e.enumlabel, e.enumsortorder "
                    "FROM pg_type t "
                    "JOIN pg_enum e ON t.oid = e.enumtypid "
                    "JOIN pg_namespace n ON n.oid = t.typnamespace "
                    "WHERE n.nspname = 'public'"
                )
            ).all()
        )

    return {
        "tables": tables,
        "columns": columns,
        "indexes": indexes,
        "constraints": constraints,
        "enums": enums,
    }


def _format_diff(expected: dict, actual: dict) -> str:
    """Format readable difference between two schema snapshots."""
    lines = []
    for section in ("tables", "columns", "indexes", "constraints", "enums"):
        missing = expected[section] - actual[section]
        unexpected = actual[section] - expected[section]
        if missing or unexpected:
            lines.append(f"  [{section}]")
            if missing:
                lines.append(f"    Missing after downgrade: {sorted(missing)}")
            if unexpected:
                lines.append(f"    Unexpected after downgrade: {sorted(unexpected)}")
    return "\n".join(lines)


def _assert_c5e2a8f4b196_downgrade_exception(s0: dict, s0_after: dict, revision: str) -> None:
    """Verify accepted difference for revision c5e2a8f4b196.

    Revision c5e2a8f4b196 ('name foreign keys and unique constraints by the naming convention'):
    Upgrading renames legacy PostgreSQL-generated constraint names (table_col_fkey, table_col_key)
    to SQLAlchemy naming convention names (fk_*, uq_*). Downgrading intentionally renames them
    back to the legacy generated names, as tested in test_postgres_constraint_names.py.
    When walking from an empty database, earlier table creations through SQLAlchemy metadata
    assigned convention names immediately, so S0 carries convention names while S0_after carries
    legacy PostgreSQL-generated names.
    We accept this difference only because downgrade cannot know earlier tables were created
    fresh under newer metadata conventions, and verify that:
    1. Tables, columns, and enums match identically;
    2. Constraint differences are strictly the 12 foreign keys and 2 unique constraints
       renamed between convention names (fk_*/uq_*) and legacy names (*_fkey/*_key);
    3. Index differences are strictly the 2 unique indexes corresponding to those unique constraints.
    """
    assert s0["tables"] == s0_after["tables"], (
        f"Revision {revision} downgrade altered tables: {s0['tables'] ^ s0_after['tables']}"
    )
    assert s0["columns"] == s0_after["columns"], (
        f"Revision {revision} downgrade altered columns: {s0['columns'] ^ s0_after['columns']}"
    )
    assert s0["enums"] == s0_after["enums"], (
        f"Revision {revision} downgrade altered enums: {s0['enums'] ^ s0_after['enums']}"
    )

    # All constraints in S0 that differ in S0_after must be fk_* or uq_* convention names
    s0_constraints_diff = s0["constraints"] - s0_after["constraints"]
    s0_after_constraints_diff = s0_after["constraints"] - s0["constraints"]

    assert len(s0_constraints_diff) == 14, (
        f"Expected exactly 14 renamed constraints in S0, got {len(s0_constraints_diff)}"
    )
    assert len(s0_after_constraints_diff) == 14, (
        f"Expected exactly 14 renamed constraints in S0_after, got {len(s0_after_constraints_diff)}"
    )

    for _table, name, _defn in s0_constraints_diff:
        assert name.startswith(("fk_", "uq_")), f"Unexpected constraint in S0 diff: {name}"

    for _table, name, _defn in s0_after_constraints_diff:
        assert name.endswith(("_fkey", "_key")), f"Unexpected constraint in S0_after diff: {name}"

    # Unique index diff must correspond to the 2 unique constraints
    s0_indexes_diff = s0["indexes"] - s0_after["indexes"]
    s0_after_indexes_diff = s0_after["indexes"] - s0["indexes"]

    assert len(s0_indexes_diff) == 2, (
        f"Expected exactly 2 renamed unique indexes in S0, got {len(s0_indexes_diff)}"
    )
    assert len(s0_after_indexes_diff) == 2, (
        f"Expected exactly 2 renamed unique indexes in S0_after, got {len(s0_after_indexes_diff)}"
    )


def test_every_migration_round_trips_stepwise_on_postgres():
    """Verify that every migration upgrade and downgrade restores the schema exactly."""
    admin_url = make_url(TEST_DATABASE_URL)
    # The name the session database uses, so a run killed halfway leaves a
    # database the next run's sweep in tests/conftest.py removes.
    name = f"ecom_pgtests_{int(time.time())}_{uuid.uuid4().hex[:12]}"
    url = admin_url.set(database=name).render_as_string(hide_password=False)

    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))

    engine = create_engine(url)
    original_uri = settings.DATABASE_URI
    try:
        settings.DATABASE_URI = SecretStr(url)
        alembic_cfg = Config(os.path.join(_ROOT, "alembic.ini"))
        alembic_cfg.set_main_option("script_location", os.path.join(_ROOT, "migrations"))
        alembic_cfg.set_main_option("sqlalchemy.url", url)
        # Leave the suite's logging alone (see migrations/env.py): alembic.ini
        # would replace the root handlers and level for every later test.
        alembic_cfg.attributes["configure_logger"] = False

        script = ScriptDirectory.from_config(alembic_cfg)
        heads = script.get_heads()
        assert len(heads) == 1, f"The migration chain must have exactly one head, found: {heads}"

        # Walk revisions from base to head in chronological order
        revisions = list(reversed(list(script.walk_revisions(base="base", head="head"))))
        assert len(revisions) > 0, "No revisions found in migrations directory"

        for r in revisions:
            s0 = _get_catalog_snapshot(engine)

            # Step 1: upgrade to revision R
            command.upgrade(alembic_cfg, r.revision)
            s1 = _get_catalog_snapshot(engine)

            # Step 2: downgrade one step
            down_rev = r.down_revision or "base"
            command.downgrade(alembic_cfg, down_rev)
            s0_after = _get_catalog_snapshot(engine)

            # Step 3: verify downgrade restores S0 (or matches accepted naming exception)
            if r.revision == "c5e2a8f4b196":
                _assert_c5e2a8f4b196_downgrade_exception(s0, s0_after, r.revision)
            else:
                assert s0_after == s0, (
                    f"Migration {r.revision} ({r.doc}) downgrade failed to restore schema S0:\n"
                    f"{_format_diff(s0, s0_after)}"
                )

            # Step 4: re-upgrade to revision R
            command.upgrade(alembic_cfg, r.revision)
            s1_after = _get_catalog_snapshot(engine)

            # Step 5: verify re-upgrade restores S1
            assert s1_after == s1, (
                f"Migration {r.revision} ({r.doc}) re-upgrade failed to restore schema S1:\n"
                f"{_format_diff(s1, s1_after)}"
            )

    finally:
        settings.DATABASE_URI = original_uri
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
        gc.collect()
