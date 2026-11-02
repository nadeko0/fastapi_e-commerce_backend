#!/usr/bin/env bash
# The base commit and HEAD timed on the same machine, one after the other.
# A baseline kept from an earlier run came from another runner: shared runners
# differ in CPU model, and the same commit measured up to 2.3 times slower on
# one model than on another, which is a regression to any threshold that
# still catches one. Here both sides share the runner, so what is left
# between them is the run-to-run noise of one machine.
#
# Usage: benchmark_ab.sh <base-ref> [max-slowdown-percent, default 35].
# Fails when a benchmark's median on HEAD is more than that percentage slower
# than on the base. When the base has no tests/benchmarks, HEAD only runs.
set -euo pipefail

if [ -z "${1:-}" ]; then
    echo "Usage: $0 <base-ref> [max-slowdown-percent]" >&2
    exit 1
fi
BASE_REF="$1"
MAX_SLOWDOWN="${2:-35}"

# Settings the app reads at import; CI test values.
export JWT_SECRET_KEY="${JWT_SECRET_KEY:-ci-test-secret-key-long-enough-for-hs256-and-the-hs512-negative-test}"
export POSTGRES_SERVER="${POSTGRES_SERVER:-localhost}" POSTGRES_USER="${POSTGRES_USER:-ci}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-ci}" POSTGRES_DB="${POSTGRES_DB:-ci}"
export REDIS_HOST="${REDIS_HOST:-localhost}" SMTP_HOST="${SMTP_HOST:-localhost}"
export SMTP_USER="${SMTP_USER:-ci@example.com}" SMTP_PASSWORD="${SMTP_PASSWORD:-ci}"
export EMAILS_FROM_EMAIL="${EMAILS_FROM_EMAIL:-ci@example.com}" EMAILS_FROM_NAME="${EMAILS_FROM_NAME:-CI}"
unset VIRTUAL_ENV || true

# Inside the checkout (.benchmarks/ is git-ignored): when the machine info of
# the two runs differs (it includes the CPU clock py-cpuinfo measures, which
# can change between two runs on one runner), pytest-benchmark 5.3.0 prints
# the storage path relative to the working directory and fails with
# ValueError for a path outside it, as it did on GitHub with /tmp.
mkdir -p .benchmarks
STORAGE_DIR=$(mktemp -d "$PWD/.benchmarks/ab-XXXXXX")
WORKTREE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/benchmark-base-XXXXXX")
cleanup() {
    git worktree remove --force "$WORKTREE_DIR" 2>/dev/null || rm -rf "$WORKTREE_DIR" 2>/dev/null || true
    rm -rf "$STORAGE_DIR"
}
trap cleanup EXIT

run_benchmarks() {
    # Warm-up rounds first: the first benchmark of a cold process measured
    # twice its usual median.
    uv run pytest tests/benchmarks -p no:cacheprovider -p no:randomly \
        --benchmark-enable --benchmark-only --benchmark-warmup=on \
        --benchmark-storage="file://$STORAGE_DIR" "$@"
}

if ! git cat-file -e "$BASE_REF:tests/benchmarks" 2>/dev/null; then
    echo "$BASE_REF has no tests/benchmarks: timing HEAD only"
    uv sync --locked --group dev
    run_benchmarks
    exit 0
fi

echo "Timing the base, $BASE_REF"
git worktree add --detach "$WORKTREE_DIR" "$BASE_REF"
(
    cd "$WORKTREE_DIR"
    uv sync --locked --group dev
    run_benchmarks --benchmark-save=base
)

echo "Timing HEAD against it"
uv sync --locked --group dev
run_benchmarks --benchmark-compare=0001 --benchmark-compare-fail="median:${MAX_SLOWDOWN}%"
