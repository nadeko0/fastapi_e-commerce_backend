# Testing Strategy and Test Suite

Software that processes money cannot rely on good intentions or manual smoke testing. The testing strategy follows a rigorous pyramid: thousands of deterministic unit tests at the base, real database and service integration in the middle, property and contract fuzzing to explore edge cases, and mutation testing to prove that the tests actually catch bugs.

## Test Pyramid

```mermaid
flowchart TD
    E2E["E2E Tests (Stripe Test Mode, Stripe CLI, 54 live Stripe tests)"] --> Fuzz["Contract & Fuzz (Schemathesis, Hypothesis State Machines)"]
    Fuzz --> Integration["Integration Tests (PostgreSQL 18, SQLite WAL, Real Redis, 2,967 cases)"]
    Integration --> Unit["Unit Tests (Deterministic Mocks, Invariants, 2,788 cases)"]
```

1. **Unit Tests:** Fast, isolated tests covering models, services, schema serialization, and money calculations. All external I/O (Stripe, Redis, SMTP) is mocked deterministically.
2. **Integration Tests:** Executed across SQLite (local development) and PostgreSQL 18, the version Compose deploys (CI). Tests verify row locking in payment paths, database CHECK constraints, lower(email) unique indexes, and Alembic migrations.
3. **Property and State-Machine Tests:** Hypothesis generates pseudo-random inputs to find boundary bugs. Stateful tests (`PaymentChain`) exercise arbitrary sequences of orders, payments, cancellations, and refunds. One property test discovered that normalizing the German letter "ß" expands "0ß" into two characters ("0SS"), quietly invalidating ISO country code length invariants.
4. **Contract and Fuzz Testing:** Schemathesis was run against the 4.0.0 OpenAPI document (54 paths), outside this suite. It uncovered unhandled 500 errors on NUL bytes (`\x00`) and lone surrogates, prompting global input sanitization.
5. **E2E Tests (Stripe Test Mode):** Black-box tests driven against real Stripe test-mode endpoints (`api.stripe.com`) with webhook forwarding via the Stripe CLI.
6. **Mutation Testing:** Automated fault injection with `mutmut 3.8.0` across the payment, webhook, reconciliation, idempotency, order, rate-limit and error modules, run on 4.0.2 and 4.0.3 (see reports/T10-mutation.md) and again on 4.1.1. On 4.1.1: 3,434 mutants, 3,088 killed and 272 timed out on the first run, 74 survived; 31 of those were gaps in the tests and are now killed by named tests (tests/unit/test_mutants_4_1.py, tests/unit/test_payment_mutants.py, tests/integration/test_reconciliation.py), each proven by rerunning its mutant; the other 43 are equivalent (log wording, messages of assertions that cannot fail, join conditions inferred from the foreign key, defaults every caller overrides). On 6.0.0: 3,669 mutants, 3,338 killed and 279 timed out, 52 survived; 8 of those were gaps (the 429 message of a consent change, `metadata.payment_id` kept by redaction, the error count and the `continue` of a reconciliation run that lost a race) and are now killed by assertions in tests/integration/test_rate_limit_api.py, test_data_retention.py and test_reconciliation.py, each proven by rerunning its mutant; the other 44 are equivalent in the same ways, plus checks the code repeats after the SDK (the webhook tolerance) and SQL pre-filters whose rows are checked again in Python. The 279 timeouts are all in the error handlers and the rate limiter, which nearly every test passes through, so mutmut selects thousands of tests for each; run in order with `-x`, the first failing one comes minutes in, past mutmut's limit. One or two from each of the 8 functions concerned, 12 mutants, were run by hand without the limit: every one was killed by a failing test (after 3 to 13 minutes in sequence, or in a minute when its test file is run directly), and none hung. Mutation testing runs by hand, not in CI: one run takes about 2.5 hours on four cores.

## Continuous Integration Pipeline

GitHub Actions runs 4 jobs on ubuntu-latest, on pushes and pull requests to `main`. A new push to a pull request cancels its older run; runs on `main` always finish.

```mermaid
flowchart TD
    subgraph Parallel Checks
        lint["lint (Ruff, mypy, module length, pip-audit)"]
        test["test (SQLite and fakeredis, coverage gates)"]
        integration["integration (PostgreSQL 18, Redis 8.8, previous release on new migrations)"]
    end
    lint --> docker_build["docker-build (app, postgres and redis images: build, Trivy scan and Compose smoke test)"]
    test --> docker_build
    integration --> docker_build
```

- **lint:** Runs Ruff across `app` and `tests` (the pycodestyle, Pyflakes, isort and warning rules plus pyupgrade, bugbear, async, naive-datetime, FastAPI, simplify, logging, return, pytest, print and security (Bandit's rules, `S`, in `app` only) rule sets; the full list is in `pyproject.toml`), and static type checking with `mypy` over `app` (`warn_return_any`, `warn_unreachable`, `disallow_any_generics` and extra error codes everywhere; fully annotated definitions in the payment layer and `app/tasks.py`). It also fails when a module under `app/` has more than 800 lines (the aim is 500), and Ruff limits a function in `app/` to complexity 15, 15 branches and 60 statements (C901, PLR0912, PLR0915; the tests are exempt). Last, `pip-audit` checks the packages pinned in `uv.lock` (all groups, by hash) against the PyPI Advisory Database. Naive local time (`datetime.now()` without a zone, `date.today()`) is caught by Ruff's DTZ rules in the code itself, so no job runs the suite under other time zones (until 6.2.0 a 7-zone matrix did).
- **test:** Executes the primary suite in parallel on SQLite, runs timing-sensitive tests sequentially, and enforces the coverage gates.
- **integration:** Runs on the PostgreSQL 18 and Redis 8.8 images Compose deploys: the whole suite on PostgreSQL (including schema round-trip tests across every revision, `tests/integration/test_postgres_migration_roundtrip.py`), the timing-sensitive database tests alone, the `real_redis` parity tests, and last the newest tagged release's database tests of orders, payments, checkout, carts, refunds, users and addresses on the current migrations (`.github/scripts/migration_compat.sh`; that release's tests about the migrations themselves are left out, and it ends at once when `migrations/` is unchanged since the tag).
- **docker-build:** Builds the production app image and the `docker/postgres` and `docker/redis` images that Compose builds, scans all three with Trivy, and starts the `docker-compose.yml` stack (`db`, `redis`, `migrate`, `api`) to verify `/livez` and `/readyz`. It needs the three jobs above.

Secrets are caught before they are committed, not after the push: the `gitleaks` hook of `.pre-commit-config.yaml` (see Pre-commit Hooks below). `.gitleaks.toml` allowlists one placeholder: the JWT-shaped example of the `Token` response in `app/schemas/user.py` and the OpenAPI snapshot, only when the file and the exact placeholder value both match, so a real token in those files is still reported.

## Test Markers and Isolation

Configuration in `pyproject.toml` and `tests/conftest.py` ensures hermetic, reproducible execution:

- `timing_sensitive`: Tests asserting elapsed wall-clock time or event-loop latency. Deselected during parallel runs (`-m "not timing_sensitive"`) to prevent CPU contention flakes, then executed sequentially.
- `db_sensitive`: Tests dependent on PostgreSQL features (row locks, constraints, migrations). Selected automatically by filename pattern in `tests/conftest.py`.
- `real_redis`: Tests executed against a live Redis instance when `REAL_REDIS_URL` is set.

### Conftest Hermetic Behaviour

- **Redis:** `fakeredis` backs `RedisService` and async clients on a shared `FakeServer`. `REDIS_PORT=1` is set in the test environment so unmocked network calls fail immediately.
- **Payments and Email:** `PAYMENT_PROVIDER=mock` and `EMAIL_PROVIDER=logging` prevent external network traffic. The mock compares only the amount and currency when an idempotency key is reused, where Stripe compares every parameter; the API cannot reach that difference, because the application compares the whole request under its own `Idempotency-Key` first, so the mock is left simpler.
- **Password Hashing:** `BCRYPT_ROUNDS=4` minimizes CPU time during mass user creation.
- **Database Isolation:** For SQLite, each worker uses a dedicated file database in WAL mode (`PRAGMA journal_mode=WAL`, `synchronous=OFF`). For PostgreSQL, each test session creates a unique database (`ecom_pgtests_<epoch>_<hex>`), runs `alembic upgrade head`, and drops the database on exit. The throwaway databases of the migration tests are named the same way. Test startup removes stale temporary databases older than one day left by aborted runs on both SQLite (`ecommerce-tests-*`) and PostgreSQL (`ecom_pgtests_*`).
- **Random Test Order:** `pytest-randomly` is enabled by default to run tests in randomized order. Each run prints its seed in the header (`Using --randomly-seed=N`) and, because `-q` hides the header, again at the end (`random order seed: rerun with --randomly-seed=N`). Reproduce an exact execution order with `pytest --randomly-seed=N`, or disable randomization with `-p no:randomly`.
- **Network Blocking:** `pytest-socket` in addopts blocks outbound socket calls (`--allow-hosts=127.0.0.1,::1,localhost --allow-unix-socket`). It intercepts Python's `socket.connect` (blocking unmocked HTTP and SMTP clients). It does not intercept libpq (PostgreSQL's C client) or DNS lookups. Running against a `TEST_DATABASE_URL` on another host requires adding that host to `--allow-hosts`. Live Stripe integration tests are explicitly marked with `enable_socket`.
- **Specialized Test Suites:** The suite includes regression suites in `tests/torture_suite`, `tests/strict_domain_audit`, and `tests/audit_verification`. Setting `TORTURE_FULL=1` enables exhaustive property and concurrency exploration.
- **Table Cleanup:** An engine hook (`Engine.before_cursor_execute`) tracks dirty tables during each test. The autouse fixture `_reset_database` issues fast SQL deletes and sequence resets only for modified tables before each test.

## Execution Commands

### Fast Local Execution
Run the full test suite in parallel using pytest-xdist, followed by timing tests:
```bash
uv run pytest tests/ -q -n 2 --dist worksteal -m "not timing_sensitive"
uv run pytest tests/ -q -m timing_sensitive
```

### PostgreSQL Integration Suite
Execute tests against a live PostgreSQL server (requires `CREATEDB` privilege):
```bash
TEST_DATABASE_URL=postgresql+psycopg://ci:ci@localhost:5432/ci uv run pytest tests/ -m "not timing_sensitive" -q -n 2 --dist worksteal
TEST_DATABASE_URL=postgresql+psycopg://ci:ci@localhost:5432/ci uv run pytest tests/ -m "db_sensitive and timing_sensitive" -q
```

### Real Redis Parity Suite
Validate Redis Lua scripts, token TTLs, and concurrency against a real server:
```bash
REAL_REDIS_URL=redis://localhost:6379/0 uv run pytest tests/ -m real_redis -q
```

### Live Stripe Integration Tests
Live tests make real network calls to `api.stripe.com` in Stripe test mode. They are skipped unless the `STRIPE_SECRET_KEY` environment variable contains a valid test key (not a placeholder) or `.env.test` is provided:
```bash
STRIPE_SECRET_KEY=<your Stripe test secret key> uv run pytest tests/integration/test_payment_live_stripe.py tests/integration/test_checkout_session_live_stripe.py tests/integration/test_payment_live_journey.py tests/integration/test_reconciliation_live_stripe.py
```

### Hypothesis Profiles
Select property testing budgets using `HYPOTHESIS_PROFILE`:
- `dev`: 100 examples, 200 ms deadline (default).
- `ci`: 200 examples, derandomized, deadlines disabled, `print_blob=True`.
- `explore`: 200 examples with randomized seeds to uncover novel failure modes. A failing example is saved in `.hypothesis/examples` (as under `dev`) and replayed first by the next run until it passes:
```bash
HYPOTHESIS_PROFILE=explore uv run pytest tests/integration/test_payment_state_machine.py
```

### OpenAPI Contract Snapshot
`tests/unit/test_openapi_contract.py` (30 tests) checks what the OpenAPI document promises beyond its shape: the operation ids, the values of free-text response fields, the response fields that are always present, the 429 response and the admin marker. The snapshot test, which pins the shape (including the cookie and response headers), prevents accidental API contract drift against `tests/snapshots/openapi.json`:
```bash
# Verify schema matches snapshot:
uv run pytest tests/unit/test_openapi_snapshot.py

# Update snapshot after intended schema changes:
UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest tests/unit/test_openapi_snapshot.py
```

### Coverage and Payment Layer Gate
CI enforces an overall 85% coverage gate and a strict 100% line and branch gate on the financial core:
```bash
# Measure combined coverage:
uv run pytest tests/ -q -n auto --dist worksteal -m "not timing_sensitive" --cov=app --cov-report= --cov-fail-under=0
uv run pytest tests/ -q -m timing_sensitive --cov=app --cov-append --cov-report=term-missing --cov-report=xml

# Enforce 100% payment layer gate:
uv run coverage report --fail-under=100 \
  --include="app/api/v1/orders/*.py,app/services/orders.py,app/services/payment/*.py,app/schemas/order.py,app/models/order.py,app/models/order_items.py,app/models/payment.py,app/models/payment_ledger.py,app/tasks.py,app/services/email/messages.py,app/services/stripe_events.py,app/services/stripe_event_redaction.py,app/services/reconciliation.py,app/models/payment_discrepancy.py,app/schemas/webhook_event.py" \
  --omit="app/services/payment/stripe_provider.py"
```

### Type Checking
Run static type verification with mypy across `app/` (the tests are not type-checked: Ruff covers them, and their fixtures and Hypothesis strategies would need annotations that prove little):
```bash
uv run mypy
```

### Local Benchmarks
Run performance benchmarks for hot execution paths:
```bash
uv run pytest tests/benchmarks --benchmark-enable --benchmark-only
```

They run by hand, not in CI: compare two commits on the same machine, as runners of one kind differ in CPU model.

### Pre-commit Hooks
`.pre-commit-config.yaml` runs with `prek` (a dev dependency) before each commit: gitleaks on the staged changes, zizmor on the workflow, Ruff, a check that `uv.lock` matches `pyproject.toml`, and generic file checks (YAML and TOML syntax, large files, trailing whitespace, final newline). Install it once per clone:
```bash
uv run prek install
uv run prek run --all-files   # on demand
```

## Verified Metrics for Release v7.0.0

Verification results on release v7.0.0 (CI run on the release candidate); a row measured on an earlier release says which:

| Metric | Measured Value | Standard / Target |
|---|---|---|
| SQLite Test Suite | 6,665 passed, 130 skipped without their services (+ 30 timing), random order, CI | 0 failures |
| PostgreSQL Test Suite | 6,735 passed on PostgreSQL 18.6 (+ 103 timing-sensitive database tests), 28 Redis-semantics tests on Redis 8.8, CI | Full migration and constraint parity |
| Hypothesis `explore` profile (before the 7.0.0 export and stock changes) | 10 runs of the 1,542 property and state-machine cases on PostgreSQL 18.6 with Redis 8.8 (WSL, Docker), new random inputs each run: 9 clean; in one, the payment state machine failed once and Hypothesis reported the replay as flaky (`FlakyStrategyDefinition`); 15 reruns of that test did not reproduce it, cause not found. An earlier run found the out-of-order dispute status bug, fixed in 7.0.0 | No falsifying example in the code |
| Migration Round Trip | every revision on PostgreSQL 18; the 6.2.0 tests on the 7.0.0 migrations: 2,474 passed (CI) | Previous release keeps working on the new schema |
| Live Stripe Test Suite (v4.1.0) | 52 passed, 2 skipped | api.stripe.com sandbox |
| Payment Layer Coverage | 100.0% line and branch on the listed payment-layer files (the mock provider file is excluded) | Zero uncovered branches in financial path |
| Overall Coverage (lines and branches combined) | 99.0% (source: `app`) | Fail-under gate set to 85% |
| Mutation Score (mutmut 3.8.0, v6.0.0) | 98.8% of 3,669 mutants detected (3,346 killed, 279 over the time limit; a sample of 12 of these run without it: all killed by a failing test) | The 44 survivors reviewed as equivalent |
| CI Execution Time | ~16 minutes across 4 jobs | Fully automated GitHub Actions workflow |

## Campaign Reports

Detailed test methodologies, logs, and remediation records live in the reports directory:
- [Final Test Campaign Overview](reports/final-test-campaign.md): Exit criteria, process, and results.
- [Stage T1: Supply Chain & Static Security](reports/T1-supply-chain.md): Trivy, gitleaks, pip-audit.
- [Stage T2: Test Polygon Environment](reports/T2-test-environment.md): Staging setup and core isolation.
- [Stage T3: Contract and Fuzz Testing](reports/T3-schemathesis.md): Schemathesis contract validation.
- [Stage T4: End-to-End Black-Box Verification](reports/T4-e2e.md): Stripe CLI and webhook flows.
- [Stage T5: Migrations and Disaster Recovery](reports/T5-migrations-backup.md): Schema diffs and backup restore.
- [Stage T6: Dynamic Security Testing](reports/T6-dast-owasp.md): OWASP ZAP and vulnerability remediation.
- [Stage T7: Load and Soak Testing](reports/T7-load.md): Locust load benchmarks and resource trends.
- [Stage T8: Chaos and Resilience](reports/T8-chaos.md): Fault injection and autonomous recovery.
- [Stage T9: Live Server Verification](reports/T9-live-server.md): Shared VPS deployment over SSH tunnel.
- [Stage T10: Mutation Testing](reports/T10-mutation.md): Fault injection with mutmut 3.8.0.
- [Daylight Saving Time Verification](reports/daylight-saving-time.md): Clock shift simulations across timezones.
- [Remediated Defects (fixes-4.0.x)](reports/fixes-4.0.x.md): Root cause analysis and commits for fixes X1 to X10.

See also: [PERFORMANCE.md](PERFORMANCE.md), [RESILIENCE.md](RESILIENCE.md), [ARCHITECTURE.md](ARCHITECTURE.md), [API.md](API.md).
