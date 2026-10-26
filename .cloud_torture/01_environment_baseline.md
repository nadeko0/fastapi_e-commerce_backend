# Phase 1 — Cloud VM environment & test baseline

Date: 2026-10-26 · Project version: 4.0.3 (pyproject.toml) · Branch: `claude/elegant-johnson-b5n6yr`

## Toolchain
| Tool | Version / status |
|---|---|
| Python | 3.13 (uv-managed `.venv`) |
| pytest | 9.1.1 |
| Hypothesis | 6.170.5 |
| pytest-xdist | 3.8.0 |
| schemathesis | **not a project dependency** — not added (would change `pyproject.toml`/`uv.lock`, outside the allowed paths and a merge-conflict risk with the parallel review branch). Fuzzing done with Hypothesis + pytest directly. |
| mutmut | **not a project dependency** — not added for the same reason; mutation-style boundary checks are hand-written in `test_boundary_mutants.py`. |
| PostgreSQL | 16.15, local cluster started in the VM, role `torture`, `max_connections=400` |
| Redis | fakeredis (project fixture). `redis-server` binary exists but the suite's own fixture is used. |

## How the torture suite runs
* `tests/torture_suite/` lives under `tests/`, so `tests/conftest.py` (client, db_session, fake_redis,
  async_session_factory, per-test table cleanup, optional PostgreSQL DB) applies unchanged.
* No `conftest.py` in `tests/torture_suite/`: `tests/integration/stripe_event_support.py` does
  `from conftest import TestingSessionLocal`, and a second module named `conftest` would shadow the
  project one (first attempt failed exactly that way). `tests/torture_suite/torture_support.py` puts
  `tests/integration` on `sys.path` and exposes `requires_postgres`.
* SQLite run: `uv run pytest tests/torture_suite`
* PostgreSQL run: `TEST_DATABASE_URL=postgresql+psycopg://torture:torture@localhost:5432/postgres uv run pytest tests/torture_suite`

## Baseline of the existing suite (before any torture test)
| Run | Result |
|---|---|
| SQLite, `-n 4 -m "not timing_sensitive"` | **5214 passed, 109 skipped** in 126 s |
| PostgreSQL 16, `-n 4 -m "db_sensitive and not timing_sensitive"` | **2263 passed, 52 skipped** in 91 s |

The existing suite is green on both back ends; every failure in the torture suite is therefore new signal.
