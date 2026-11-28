#!/usr/bin/env bash
# Old code on the new schema. In a rolling deploy `alembic upgrade head` runs
# first and the previous release keeps serving until it is replaced, so a new
# migration must not break it (a NOT NULL column without a default, a dropped
# or renamed column the old code still uses). This copies the current
# migrations/ into a worktree of the previous release and runs that release's
# own database tests, which build their databases with the new chain.
#
# Usage: migration_compat.sh <base-tag>, with TEST_DATABASE_URL pointing at a
# PostgreSQL server the tests may create databases on. Exits 0 at once when
# migrations/ has not changed since the tag.
set -euo pipefail

BASE_TAG="${1:?usage: $0 <base-tag>}"
: "${TEST_DATABASE_URL:?set TEST_DATABASE_URL to a PostgreSQL server}"

if git diff --quiet "$BASE_TAG..HEAD" -- migrations/; then
    echo "No changes under migrations/ since $BASE_TAG"
    exit 0
fi

WORKTREE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/migration-compat-XXXXXX")
trap 'git worktree remove --force "$WORKTREE_DIR" 2>/dev/null || rm -rf "$WORKTREE_DIR"' EXIT

git worktree add --detach "$WORKTREE_DIR" "$BASE_TAG"
rm -rf "$WORKTREE_DIR/migrations"
cp -r migrations "$WORKTREE_DIR/migrations"

# Git Bash on Windows needs a Windows path for uv (pwd -W); Linux uses pwd.
cd "$(cd "$WORKTREE_DIR" && (pwd -W 2>/dev/null || pwd))"
unset VIRTUAL_ENV || true
uv sync -q --locked --group dev

# Settings the old release reads at import (CI sets them; defaults for local runs).
export JWT_SECRET_KEY="${JWT_SECRET_KEY:-ci-test-secret-key-long-enough-for-hs256-and-the-hs512-negative-test}"
export POSTGRES_SERVER="${POSTGRES_SERVER:-localhost}" POSTGRES_USER="${POSTGRES_USER:-ci}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-ci}" POSTGRES_DB="${POSTGRES_DB:-ci}"
export REDIS_HOST="${REDIS_HOST:-localhost}" SMTP_HOST="${SMTP_HOST:-localhost}"
export SMTP_USER="${SMTP_USER:-ci@example.com}" SMTP_PASSWORD="${SMTP_PASSWORD:-ci}"
export EMAILS_FROM_EMAIL="${EMAILS_FROM_EMAIL:-ci@example.com}" EMAILS_FROM_NAME="${EMAILS_FROM_NAME:-CI}"

# The old release's tests about the migrations themselves (its head, its
# model-vs-schema drift) fail by definition on a newer chain: left out.
# pytest ignores an --ignore/--deselect that matches nothing, so a stale
# entry here does not fail; prune it when the base release changes.
uv run pytest -q -p no:cacheprovider -n auto \
    -m "db_sensitive and not timing_sensitive" \
    -k "order or payment or checkout or cart or refund or user or address" \
    --ignore tests/integration/test_reconciliation_migration.py \
    --ignore tests/unit/test_migrations_offline.py \
    --deselect tests/integration/test_postgres_only.py::test_migrations_round_trip_and_agree_with_the_session_schema
