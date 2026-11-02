#!/usr/bin/env bash
# Old code on the new schema. During a rolling deploy the previous release
# keeps serving requests after `alembic upgrade head` has run, so a new
# migration must not break it (a NOT NULL column without a default, a dropped
# or renamed column the old code still writes). This runs the previous
# release's own database tests on the current migrations: the release is
# checked out into a temporary worktree, the current migrations/ is copied over
# its own, and its tests then build their databases with the new chain.
#
# Usage: migration_compat.sh <base-tag>, with TEST_DATABASE_URL pointing at a
# PostgreSQL server the tests may create databases on. Exits 0 at once when
# migrations/ has not changed since the tag.
set -euo pipefail

if [ -z "${1:-}" ]; then
    echo "Usage: $0 <base-tag>" >&2
    exit 1
fi
BASE_TAG="$1"
: "${TEST_DATABASE_URL:?set TEST_DATABASE_URL to a PostgreSQL server}"

if git diff --quiet "$BASE_TAG..HEAD" -- migrations/; then
    echo "No changes under migrations/ between $BASE_TAG and HEAD"
    exit 0
fi

WORKTREE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/migration-compat-XXXXXX")
cleanup() {
    git worktree remove --force "$WORKTREE_DIR" 2>/dev/null || rm -rf "$WORKTREE_DIR" 2>/dev/null || true
}
trap cleanup EXIT

echo "Checking out $BASE_TAG into $WORKTREE_DIR"
git worktree add --detach "$WORKTREE_DIR" "$BASE_TAG"
rm -rf "$WORKTREE_DIR/migrations"
cp -r migrations "$WORKTREE_DIR/migrations"

# Git Bash on Windows: uv and Python want a Windows path (pwd -W). On Linux
# pwd -W fails and the plain path is used.
cd "$(cd "$WORKTREE_DIR" && (pwd -W 2>/dev/null || pwd))"
unset VIRTUAL_ENV || true
uv sync --locked --group dev

# Settings the old release reads at import; CI test values.
export JWT_SECRET_KEY="${JWT_SECRET_KEY:-ci-test-secret-key-long-enough-for-hs256-and-the-hs512-negative-test}"
export POSTGRES_SERVER="${POSTGRES_SERVER:-localhost}" POSTGRES_USER="${POSTGRES_USER:-ci}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-ci}" POSTGRES_DB="${POSTGRES_DB:-ci}"
export REDIS_HOST="${REDIS_HOST:-localhost}" SMTP_HOST="${SMTP_HOST:-localhost}"
export SMTP_USER="${SMTP_USER:-ci@example.com}" SMTP_PASSWORD="${SMTP_PASSWORD:-ci}"
export EMAILS_FROM_EMAIL="${EMAILS_FROM_EMAIL:-ci@example.com}" EMAILS_FROM_NAME="${EMAILS_FROM_NAME:-CI}"

# The behaviour tests of the old release that touch the database. Its tests
# about the migrations themselves (the head, drift between its models and the
# schema) fail by definition on a newer chain and are left out by name.
uv run pytest -p no:cacheprovider -n auto \
    -m "db_sensitive and not timing_sensitive" \
    -k "order or payment or checkout or cart or refund or user or address" \
    --deselect tests/integration/test_postgres_only.py::test_migrations_round_trip_and_agree_with_the_session_schema \
    --deselect tests/integration/test_reconciliation_migration.py::test_the_migration_builds_the_table_of_the_model \
    --deselect tests/integration/test_reconciliation_migration.py::test_the_migration_can_be_undone \
    --deselect tests/integration/test_reconciliation_migration.py::test_the_migration_history_has_one_head \
    --deselect tests/unit/test_migrations_offline.py::test_upgrade_head_renders_as_sql_without_a_database \
    --deselect tests/unit/test_migrations_offline.py::test_the_offline_script_carries_the_duplicate_email_check_before_the_index
