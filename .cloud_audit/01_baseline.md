# 01 — Baseline (Cloud VM, 2026-10-26)

| Check | Command | Result |
|---|---|---|
| Dependencies | `uv sync` | OK (Python 3.13, uv 0.11.32) |
| Fast test suite | `uv run pytest tests/ -q -n 4 -m "not timing_sensitive" -p no:cacheprovider` | **EXIT 0 — 5214 passed, 109 skipped in 136.6 s** (skips: real-Redis parity needs `REAL_REDIS_URL`, Postgres-only `db_sensitive` cases). Log: `_pytest_baseline.log` |
| Ruff (CI gate) | `uv run ruff check app tests` | **All checks passed** (exit 0) |
| Ruff format | `uv run ruff format --check app tests` | 227 files would be reformatted — formatter is **not** a CI gate (informational) |
| mypy (not configured in project) | `uvx --with pydantic mypy app --ignore-missing-imports` | 91 errors in 22 files (exit 1). ~50 are false positives from `Annotated`/`Depends` type aliases (`XListParams? has no attribute`); remainder triaged by Subagent 2D. Log: `_mypy.log` |
| Bandit | `uvx bandit -r app` | 0 High, 1 Medium (B104 `HOST="0.0.0.0"` default — intended for containers), 11 Low (B105 false positives on Stripe test tokens / Redis key prefixes). Log: `_bandit.log` |

Note: `-n 2` from the brief was raised to `-n 4` for VM throughput; selection identical.
Postgres-only `db_sensitive` tests and real-Redis tests were not runnable in this VM (no Postgres/Redis server).
