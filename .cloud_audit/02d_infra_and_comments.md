# 2D: Infrastructure, model-vs-migration drift, stale comments, mypy triage

Repo: `fastapi_e-commerce_backend` v4.0.3, HEAD `6bf493d`. Auditor: Subagent 2D. Date: 2026-10-26.
Nothing under `app/`, `migrations/` or `tests/` was modified. Every runtime check used throw-away servers (a private PostgreSQL 16.15 cluster on 127.0.0.1:55432 and redis-server on 127.0.0.1:56379). Both were stopped and deleted afterwards.

---

## 1. Scope checklist

### Part 1: infrastructure (read in full, line by line)
| File | Read | Notes |
|---|---|---|
| app/core/database.py | ✔ | engines, sessions, error mapping, DbSession scope |
| app/core/config.py | ✔ | pool sizing, DATABASE_URI assembly, deploy checks |
| app/core/clock.py | ✔ | |
| app/core/logging_config.py | ✔ | |
| app/core/telemetry.py | ✔ | |
| app/core/probes.py | ✔ | |
| app/core/celery_context.py | ✔ | |
| app/core/request_context.py | ✔ | |
| app/core/overload.py | ✔ | |
| app/services/redis.py | ✔ | |
| app/services/redis_async.py | ✔ | |
| app/services/redis_codec.py | ✔ (comments plus the I/O-relevant parts) | |
| app/main.py, main.py, app/__init__.py | ✔ | lifespan, disposal, middleware order |
| app/tasks.py | ✔ | sessions, engine, beat, purge loops |
| app/models/*.py (13 files) | ✔ | |
| migrations/env.py + all 15 versions | ✔ | |
| Dockerfile, docker-compose.yml, alembic.ini, .env.example, docker/postgres/initdb/10-app-role.sh | ✔ | |
| Supporting reads | ✔ | app/services/reconciliation.py, stripe_events.py (session handling), gdpr.py, email/smtp_provider.py, FastAPI 0.153 `routing.py` (function-scope exit order) |

### Part 2: comments and docstrings, all 80 `app/*.py` files plus migrations
Method: every `#` comment and every module, class and function docstring was pulled out with a tokenize/ast extractor and read. A second script cross-checked every identifier and path named in a comment against all code names in `app/`, `migrations/`, `tests/` and the config files. Its 31 hits were checked by hand.

| Package | Files (✔ = all comments and docstrings read) |
|---|---|
| app/ | ✔ `__init__.py` ✔ `main.py` ✔ `tasks.py` |
| app/api | ✔ `__init__.py` ✔ `merge_patch.py` ✔ `v1/__init__.py` ✔ `v1/admin.py` ✔ `v1/cart.py` ✔ `v1/legal.py` ✔ `v1/orders.py` ✔ `v1/products.py` ✔ `v1/users.py` |
| app/core | ✔ `__init__` ✔ `body_limit` ✔ `celery_context` ✔ `clock` ✔ `config` ✔ `currencies` ✔ `database` ✔ `dependencies` ✔ `errors` ✔ `etag` ✔ `forwarded` ✔ `head` ✔ `idempotency` ✔ `location` ✔ `logging_config` ✔ `openapi` ✔ `overload` ✔ `passwords` ✔ `probes` ✔ `rate_limit` ✔ `request_context` ✔ `security` ✔ `security_headers` ✔ `sorting` ✔ `telemetry` ✔ `text_input` |
| app/models | ✔ `__init__` ✔ `address` ✔ `base` ✔ `category` ✔ `enums` ✔ `order` ✔ `order_items` ✔ `payment` ✔ `payment_discrepancy` ✔ `payment_ledger` ✔ `product` ✔ `user` |
| app/schemas | ✔ `__init__` ✔ `address` ✔ `admin` ✔ `cart` ✔ `category` ✔ `common` ✔ `legal` ✔ `order` ✔ `product` ✔ `user` ✔ `webhook_event` |
| app/services | ✔ `__init__` ✔ `gdpr` ✔ `orders` ✔ `reconciliation` ✔ `redis` ✔ `redis_async` ✔ `redis_codec` ✔ `stripe_events` |
| app/services/email | ✔ `__init__` ✔ `base` ✔ `logging_provider` ✔ `messages` ✔ `smtp_provider` |
| app/services/payment | ✔ `__init__` ✔ `base` ✔ `exceptions` ✔ `live_stripe_provider` ✔ `stripe_provider` ✔ `types` |
| migrations | ✔ `env.py` ✔ `README` ✔ `script.py.mako` ✔ 482750d08d54 ✔ 5fe510d5d45e ✔ b6d7b96c6d17 ✔ ed98c07e2fbc ✔ e1959653f4a2 ✔ 3f6c2a9d1b7e ✔ 76d14f9f12e5 ✔ a7c3e5b19d42 ✔ c2d8f4a6b913 ✔ d4b7e1f93a62 ✔ d4e9a1c7b265 ✔ ada27dd39537 ✔ 821e97899aca ✔ b6f1c8d2a047 ✔ c5e2a8f4b196 |

The table lists 80 app files: 3 top-level + 9 api + 26 core + 12 models + 11 schemas + 8 services + 5 email + 6 payment, which matches `git ls-files 'app/*.py' | wc -l` = 80.

---

## 2. Summary

| ID | Severity | Confidence | Title |
|---|---|---|---|
| 2D-01 | **High** | Confirmed (reproduced on PG 16) | The GDPR erasure and retention purges run 100 bcrypt hashes inside one open transaction, which exceeds the 30 s `idle_in_transaction_session_timeout`. The batch rolls back on every run, so erasures never complete. |
| 2D-02 | Medium | Confirmed | The Celery worker and beat never install the JSON logging. Worker logs carry no request_id/trace_id, `LOG_LEVEL` and `LOG_FILE` are ignored, and the `celery_context` propagation is never seen in any log. |
| 2D-03 | Low-Medium | Plausible | The GDPR deletion route sends SMTP mail while its DB transaction is open. A slow SMTP server leads to the idle-in-transaction kill and a 500 after the "request received" email has gone out. It also holds a pooled connection for the whole SMTP exchange. |
| 2D-04 | Low-Medium | Confirmed | `DATABASE_URI` is assembled without URL-encoding the user and password. A password containing `@` (or `%XX`) yields a wrong host or password and every request answers 503. |
| 2D-05 | Low | Confirmed (code) / Plausible (impact) | No `connect_timeout` for psycopg, and `/health` has no time limit. Against a black-holed DB each connect waits for the OS TCP timeout. |
| 2D-06 | Low | Plausible | Engines are built without `hide_parameters=True`. Bound SQL parameters (PII) reach the logs through `exc_info` and are stored in `webhook_events.last_error`, which is admin-visible and not redacted on erasure. |
| 2D-07 | Low / Info | Confirmed | `send_order_confirmation` and `send_order_status_update` Celery tasks (with retry, dedup, routing and rate-limit config) are never enqueued. All order email goes through in-process `BackgroundTasks` and is lost on a worker crash. |
| 2D-08 | Info | Confirmed | Redis socket timeouts rely on redis-py 8.x defaults (5 s) with an unpinned `redis` dependency. Today this is safe through uv.lock. |
| 2D-09 | Info | Confirmed | Only one migration sets `lock_timeout`. 76d14f9f12e5 adds validated CHECK constraints under ACCESS EXCLUSIVE with no lock timeout. |
| Drift | none | Confirmed | `alembic upgrade head` on PostgreSQL 16 vs `Base.metadata`: `compare_metadata` = `[]`, `alembic check` clean. CHECK names, FK `ondelete` and every constraint and index name match. The existing drift test passes on PG. |
| 2D-C01..C19 | Low (docs) | Confirmed | 19 stale or lying comments and docstrings (section 5) |
| mypy | n/a | n/a | 91 errors, 0 real runtime bugs, 1 latent bug in dead code (section 6) |

---

## 3. Infrastructure findings

### 2D-01 (High, Confirmed): erasure and retention purge cannot finish on PostgreSQL

**Where**
- `app/core/database.py:56-63`: every Postgres connection (API and Celery, `app/tasks.py:160`) gets
  `"-c idle_in_transaction_session_timeout=30000 -c statement_timeout=60000"`.
- `app/tasks.py:286-290`:
  ```python
  # Each account costs a bcrypt hash (BCRYPT_ROUNDS, ~0.25 s at 12), so a
  # batch of 100 is ~25 s: well inside task_soft_time_limit, ...
  PURGE_BATCH_SIZE = 100
  ```
- `app/tasks.py:352-365` (`_anonymise_pending`): `SELECT … LIMIT 100` opens the transaction. Next, `_anonymise_account` runs `get_password_hash(str(uuid4()))` once per account (`tasks.py:372`, synchronous bcrypt, pure CPU, nothing sent to the DB because `autoflush=False`). Only after that does `stripe_events.redact_users_events` issue the next statement, followed by `db.commit()`.

For the whole hashing loop the connection is **idle in transaction**. At `BCRYPT_ROUNDS=12` that takes 100 × ~0.3 s ≈ 30 s on a 4-core box (measured below). At 13 rounds, which is allowed ("a deployment must not go below 12"), it takes about 60 s. Postgres then terminates the session. The next statement fails, the batch rolls back, and the task fails. Tomorrow's run selects the same accounts first (keyset from id 0) and fails again. **No erasure request older than the grace period ever completes**: this violates GDPR Art. 17 and the privacy policy's promise, and nothing alerts. `purge_expired_retention` (`tasks.py:407-451`, the same `_anonymise_pending`) has the same failure.

Aggravating factors:
- The exception is `psycopg.errors.IdleInTransactionSessionTimeout`, which SQLAlchemy raises as **`InternalError`**, not `OperationalError`. So `is_database_unavailable_error()` (`database.py:131-147`) does not classify it as an outage, and no `autoretry_for=(OperationalError,)` covers it.
- The suite runs on SQLite with `BCRYPT_ROUNDS=4`, so it never sees this. The PG-only tests do not exercise the purge with real cost.
- With prefork concurrency equal to the number of CPUs, other tasks (and the second purge 15 min later) compete for CPU, so even 12 rounds is a coin toss.

**Reproduction** (script `purge_repro.py` in the scratchpad: 100 users with an expired `data_deletion_date`, then `purge_expired_deletion_requests` through `app.tasks.task_session`, against a migrated PG 16 database):
```
$ DATABASE_URI=postgresql+psycopg://…/drift2d BCRYPT_ROUNDS=12 python purge_repro.py
purged 100 in 28.6 s          # 1.4 s from the kill threshold on an idle 4-core box
$ DATABASE_URI=… BCRYPT_ROUNDS=13 python purge_repro.py
FAILED after 56.6 s: InternalError (psycopg.errors.IdleInTransactionSessionTimeout)
       terminating connection due to idle-in-transaction timeout
still pending: 100
# server log: FATAL:  terminating connection due to idle-in-transaction timeout
```
For reference, bcrypt on this machine measures 0.30 s per hash at 12 rounds and 0.59 s at 13.

**Minimal fix.** Hash once per batch, before the batch's transaction opens. The password is a random UUID nobody knows, so one hash per batch is as unusable as 100:
```diff
--- a/app/tasks.py
+++ b/app/tasks.py
@@ def _anonymise_pending(db, pending, batch_size: int, due=None) -> int:
     while True:
+        # bcrypt outside the transaction: 100 hashes inside it kept the
+        # connection idle in transaction past idle_in_transaction_session_timeout.
+        unusable_hash = get_password_hash(str(uuid4()))
         accounts = db.scalars(pending.where(User.id > last_id).limit(batch_size)).all()
@@
         for account in erased:
-            _anonymise_account(account)
+            _anonymise_account(account, unusable_hash)
@@
-def _anonymise_account(account) -> None:
+def _anonymise_account(account, unusable_hash: str) -> None:
     account.email = f"deleted-user-{account.id}@deleted.invalid"
-    account.hashed_password = get_password_hash(str(uuid4()))
+    account.hashed_password = unusable_hash
```
Optionally also add `psycopg.errors.IdleInTransactionSessionTimeout` (or `InternalError` whose `.orig` is that class) to `is_database_unavailable_error`, and add a PG-only regression test with `BCRYPT_ROUNDS=12` and 100 users.

**Disproof attempts.** Does `autoflush=False` change this? No: it is the reason no statement is sent during the loop. Does `selectinload` issue the second query before the loop? Yes, but that still happens before the hashes, so it does not help. Could the 30 s timeout be absent in production? No: it is applied unconditionally for any `postgresql…` URL, the Celery engine included.

---

### 2D-02 (Medium, Confirmed): Celery processes never configure the application's logging
- `app/main.py:26` is the only caller of `setup_logging()`. `app/tasks.py` (the Celery app module) never imports `app.main` and does not connect `celery.signals.setup_logging` or `after_setup_logger`.
- Check: `python -c "import logging, app.tasks, sys; print('app.main' in sys.modules, logging.getLogger().handlers, logging.getLogger('app').level)"` prints `False [] 0`.
- Effects in the worker and in beat:
  - Celery's own plain-text formatter is used, not `JSONFormatter`.
  - The `request_id`/`trace_id` that `app/core/celery_context.py` carefully restores ("restored into the same contextvar the JSON log formatter reads", `celery_context.py:7-8`) are never written to any log line.
  - `LOG_LEVEL` and `LOG_FILE` are ignored.
  - The "app.* at LOG_LEVEL, root at WARNING" policy (`logging_config.py:83-88`) does not apply: `--loglevel=info` makes every third-party INFO logger visible. Some of those contain `sqlalchemy.engine` output when `SQL_DEBUG` is set.
- Failure scenario: an operator tries to trace a webhook from the API's `request_id` through `process_stripe_event`. The ids never appear in the worker logs, and the log pipeline (built for JSON lines) cannot parse worker output.
- Fix:
  ```diff
  --- a/app/tasks.py
  +++ b/app/tasks.py
  -from celery.signals import worker_process_init, worker_process_shutdown
  +from celery.signals import setup_logging as celery_setup_logging, worker_process_init, worker_process_shutdown
  +from app.core.logging_config import setup_logging
  @@
  +# Connecting this signal stops Celery configuring logging itself; the worker
  +# and beat then write the same JSON lines (with request_id/trace_id) as the API.
  +celery_setup_logging.connect(lambda **_: setup_logging(), weak=False)
  ```

---

### 2D-03 (Low-Medium, Plausible): GDPR deletion holds an open transaction across synchronous SMTP
- `app/api/v1/legal.py:346-348`: `await gdpr_service.process_data_deletion(await _load_user(db, current_user.id))`. The `SELECT` in `_load_user` opens the transaction.
- `app/services/gdpr.py:126-141`: inside that transaction, `await run_in_threadpool(send_gdpr_request_received, …)` runs. That is connect, STARTTLS, login and send, each blocking operation with `SMTP_TIMEOUT_SECONDS=10` (`smtp_provider.py:41-45`). Only then does it set the flags and `await self.db.commit()`.
- Scenario: a slow or tar-pitting SMTP relay, or a saturated anyio threadpool (40 threads shared with bcrypt), takes more than 30 s. Postgres kills the connection, the commit raises, and the user gets a 500 "Failed to process deletion request" although they already received the "deletion request received" email. Even at the normal ~0.8 s per SMTP send, every deletion pins one of the 15 async pool connections, idle in transaction, for the SMTP duration.
- Fix: commit first, then mail (or `await self.db.commit()` right after loading):
  ```diff
  --- a/app/services/gdpr.py
  +++ b/app/services/gdpr.py
  @@ async def process_data_deletion(self, user: User) -> str:
  -            from app.services.email import send_gdpr_request_received
  -            await run_in_threadpool(send_gdpr_request_received, user.email, "deletion", request_id)
               user.is_active = False
               user.data_deletion_requested = True
               user.data_deletion_date = utc_now()
               await self.db.commit()
  +            from app.services.email import send_gdpr_request_received
  +            await run_in_threadpool(send_gdpr_request_received, user.email, "deletion", request_id)
  ```
  `expire_on_commit=False`, so `user.email` stays readable after the commit.

---

### 2D-04 (Low-Medium, Confirmed): `DATABASE_URI` built by string interpolation
- `app/core/config.py:208`:
  `return f"postgresql+psycopg://{info.data.get('POSTGRES_USER')}:{password}@{info.data.get('POSTGRES_SERVER')}/{info.data.get('POSTGRES_DB')}"`
- The same codebase already knows this problem for the broker URL (`app/tasks.py:59-69` quotes the Redis password). The deploy check only requires at least 16 bytes, and password generators commonly emit `@`.
- Reproduction (Settings built with `POSTGRES_SERVER=db`, then `make_url` on the result):
  ```
  'p@ssw0rd-1234567890ab'  -> host 'ssw0rd-1234567890ab@db', password mismatch
  'Ab3/x+Yz9=Qw8kLm2Np4Rs6T', 'abc#def…', 'pa%ss…' -> parsed OK (SQLAlchemy is lenient), but any '%XX' sequence would be percent-decoded
  ```
  Startup does not fail (engines connect lazily). Every request then answers 503 "database temporarily unavailable" and `/readyz` fails, with nothing pointing at the password.
- Fix (verified to round-trip `p@ss/w:rd#1%41`):
  ```diff
  -        return f"postgresql+psycopg://{info.data.get('POSTGRES_USER')}:{password}@{info.data.get('POSTGRES_SERVER')}/{info.data.get('POSTGRES_DB')}"
  +        from sqlalchemy.engine import URL
  +        return URL.create(
  +            "postgresql+psycopg", username=info.data.get("POSTGRES_USER"), password=password,
  +            host=info.data.get("POSTGRES_SERVER"), database=info.data.get("POSTGRES_DB"),
  +        ).render_as_string(hide_password=False)
  ```

---

### 2D-05 (Low): no connect timeout for the database; `/health` unbounded
- `app/core/database.py:56-64` sets `options` only. libpq/psycopg's `connect_timeout` defaults to none, so against a black-holed DB host (SYN dropped) a new connection waits for the kernel TCP timeout (about 2 min on Linux). `pool_timeout=30` only bounds waiting for a pooled slot. `pool_pre_ping` then has to open new connections and hangs the same way.
- `app/main.py:189` calls `probes.database_is_up()` with `limit_seconds=None`, so there is no time limit at all (`/readyz` correctly uses 2 s). Docker's probe gives up after 5-10 s, but each 30 s probe leaves a coroutine and a pending connect behind.
- Impact is bounded by the in-flight limiter (503 quickly once 15 requests are stuck), so the severity stays Low.
- Fix: `"connect_timeout": 5` in `_build_connect_args`, and `probes.database_is_up(READINESS_TIMEOUT_SECONDS)` / `redis_is_up(...)` in `health_check`.

### 2D-06 (Low): bound SQL parameters reach the logs and `webhook_events.last_error`
- Engines (`database.py:69-81,105`, `tasks.py:152-161`) do not set `hide_parameters=True`. SQLAlchemy's `str(exc)` includes `[parameters: …]`.
- These sinks log `exc_info=e` for DB errors: `database.py:160,168,193,195` and `admin.py:627`.
- `stripe_events.py:594` stores `f"{type(error).__name__}: {error}"[:2000]` in `webhook_events.last_error`. That column is admin-visible (`schemas/webhook_event.py:16`) and is not touched by `redact_users_events` (`stripe_events.py:710-759`, which redacts only `payload`).
- The code's own policy says logs are "kept apart from the account and not scrubbed when it is erased" (`email/base.py:12-15`, `users.py:518-520`).
- Scenario: a `DataError` or `IntegrityError` on a user or address write logs the email, phone and address values. On the users table it also logs the bcrypt hash.
- Fix: `hide_parameters=True` in `_engine_kwargs` and in the Celery engine (set it to `settings.SQL_DEBUG`'s inverse if parameters are wanted in development).

### 2D-07 (Low / Info): order email Celery tasks are dead; email delivery is not durable
- `send_order_confirmation` and `send_order_status_update` (`tasks.py:194-284`), with their `autoretry_for`, Redis dedup markers, `task_routes` entry and `task_annotations` rate limits (`tasks.py:114-126`), are **never enqueued**. `grep -rn "\.delay(\|apply_async" app` finds only `process_stripe_event` and `check_payment_reconciliation`.
- All order and status emails are sent from FastAPI `BackgroundTasks` (`orders.py:396,510,571,781`) or `_AfterCommit` (`stripe_events.py:508`). A process crash or restart after the commit loses the email, and nothing retries it. Long comments in `tasks.py` and `email/messages.py:66-73,108-115` describe behaviour of the dead path.
- Recommendation: either enqueue these tasks after commit (durable, retried, deduped) or delete them and their config and comments.

### 2D-08 (Info): Redis timeouts are implicit
- `app/services/redis.py:54-70` and `redis_async.py:86-96` set no `socket_timeout`/`socket_connect_timeout`. With redis-py 8.1.0 (pinned in uv.lock) both default to 5 s (verified: `ConnectionPool(host='x').make_connection().socket_timeout == 5`, sync and asyncio). Older redis-py (≤5.x) defaulted to `None`, meaning blocking forever, and pyproject declares an unpinned `"redis"`.
- Recommendation: state `socket_timeout=5, socket_connect_timeout=5, health_check_interval=30` explicitly in both pools.
- A real-Redis run of `tests/integration/test_real_redis_parity.py` (normally skipped: 56 tests) passed against redis-server: `56 passed in 14.82s`.

### 2D-09 (Info): migrations without `lock_timeout`
- Only `ada27dd39537` sets `SET LOCAL lock_timeout`. `76d14f9f12e5` (`op.create_check_constraint`, line 78) validates existing rows while holding ACCESS EXCLUSIVE, with no lock timeout. Behind a long transaction it queues, and every query on those tables queues behind it.
- Pre-release (small tables) this is harmless. For future migrations, use `NOT VALID` + `VALIDATE CONSTRAINT`, or a `lock_timeout` in `env.py` (`connection.exec_driver_sql("SET lock_timeout = '5s'")` for PG).

---

## 4. Model-vs-migration drift (empirical)

**An existing test covers this, but only on Postgres:** `tests/integration/test_postgres_only.py:336-375` (`test_migrations_round_trip_and_agree_with_the_session_schema`, `compare_metadata(...) == []`). It is gated by `requires_postgres` (`TEST_DATABASE_URL`), so it **is skipped in the default run**: the baseline shows 109 skipped. The default suite builds its SQLite schema with `Base.metadata.create_all` (`tests/conftest.py:319,359`). Migrations cannot run on SQLite at all: `alembic upgrade head` on SQLite fails with `NotImplementedError: No support for ALTER of constraints in SQLite dialect`, from c5e2a8f4b196 and others. Postgres is the only valid drift target.

**Commands run (PostgreSQL 16.15, fresh database):**
```
# 1. The project's own PG-only migration / drift / constraint tests
TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55432/postgres \
  uv run pytest -q tests/integration/test_postgres_only.py tests/integration/test_postgres_constraint_names.py \
  tests/integration/test_postgres_data_migrations.py tests/integration/test_reconciliation_migration.py \
  tests/integration/test_check_constraints.py
-> 48 passed in 16.58s
TEST_DATABASE_URL=… uv run pytest -q tests/integration/test_postgres_pool_recovery.py test_postgres_indexes.py \
  test_postgres_statement_timeout.py test_idempotency_fingerprint.py test_payment_paths.py
-> 58 passed, 6 skipped in 7.42s

# 2. Independent script (drift.py): alembic upgrade head into a new DB, then
#    compare_metadata(compare_type, compare_server_default) plus a manual
#    comparison of CHECK names, FK ondelete, and all constraint/index names per table
alembic rc 0 … Running upgrade b6f1c8d2a047 -> c5e2a8f4b196, name foreign keys and unique constraints by the naming convention
compare_metadata diff: []
(no "CHECK mismatch", "FK mismatch" or "NAME mismatch" lines)
tables: 12 ['addresses','alembic_version','categories','order_items','orders','payment_discrepancies',
            'payment_disputes','payment_refunds','payments','product_variants','products','users','webhook_events']

# 3. Alembic CLI
alembic downgrade base   -> OK (all 15 downgrades)
alembic upgrade head     -> OK
alembic check            -> "No new upgrade operations detected."
alembic heads            -> c5e2a8f4b196 (head)   (single head, no branches)
alembic upgrade head --sql -> rc 0, 543 lines (offline mode works; DO-block guards present)
```
**Result: no drift.** Column types, nullability, server defaults, indexes (including the partial and functional `uq_users_email_lower`), unique constraints, FK names and `ondelete`, CHECK names, and the naming convention all agree. `env.py` imports only `app.models.base`, but that import runs `app/models/__init__.py`, which registers all 12 tables, as the empty diff confirms.

Caveat: production runs `postgres:15-alpine` and this check ran on 16.15. None of the DDL used is version-sensitive between 15 and 16.

---

## 5. Stale or lying comments

Each item was checked with grep or by reading the referenced code.

| ID | File:line | Comment (quoted) | What the code actually does | Suggested comment |
|---|---|---|---|---|
| 2D-C01 | app/core/config.py:405-406 | "production requires https (check_payment_settings_for_production)." | No such function. The check is `check_deployment_settings` (config.py:503-549, rule at :543-544), and it applies to every environment except development/test, not only production. | "…every deployed environment requires https (check_deployment_settings)." |
| 2D-C02 | app/core/overload.py:12-14, 19-22 | "the event loop blocks waiting for one that only the loop itself could release (measured under locust - see README, "Load testing")" / "An authenticated request can hold two sync connections at once (get_current_user's session and the route's own), so the safe ceiling is (DB_POOL_SIZE + DB_MAX_OVERFLOW) / 2." | Routes run on one shared **async** session per request (database.py:202-208; security.py:296-302 and 363-369 say the second connection is gone). Waiting for an async pool slot yields to the loop. README has no "Load testing" section (it lives in docs/PERFORMANCE.md and docs/reports/load-testing-3x.md). The /2 advice makes operators set the limit at half capacity. | "…requests wait up to pool_timeout for a connection. Each request holds at most one async connection (the shared DbSession), so the ceiling is DB_POOL_SIZE + DB_MAX_OVERFLOW (see docs/PERFORMANCE.md)." |
| 2D-C03 | .env.example:35-36 | "Keep it at or below the sync pool (DB_POOL_SIZE + DB_MAX_OVERFLOW)." | Requests use the async pool (the sync pool serves only the admin reconciliation thread). Also contradicts C02's "/2". | "…at or below the async pool (DB_POOL_SIZE + DB_MAX_OVERFLOW): one connection per request." |
| 2D-C04 | app/core/request_context.py:14-16 | "no traceparent is sent on outgoing calls or echoed back (the spec requires neither; there is no tracing SDK in the app)" | app/core/telemetry.py:275-299 installs the OpenTelemetry SDK. With tracing on, Requests and Celery instrumentation inject `traceparent` into outgoing Stripe calls and task headers. | "With tracing off, no traceparent is sent on outgoing calls; with OTEL on, app/core/telemetry.py propagates it (requests, Celery). It is never echoed back…" |
| 2D-C05 | app/main.py:86-90; app/core/body_limit.py:29-30 | "Registered before everything else, so it is the innermost middleware" / "It is registered innermost, see app/main.py." | `HeadMiddleware` is added first (main.py:84), so it is innermost. BodySizeLimit is second-innermost. | "Registered right after HeadMiddleware, so it sits just outside it…" |
| 2D-C06 | app/main.py:111-112 | "Outside everything but the security headers: a request that is going to be shed should cost nothing" | `RequestContextMiddleware` (main.py:121) and the OTel middleware (main.py:132) are also outside it, as main.py:117-118 itself says. | "Outside everything but request-id, security headers (and OTel)…" |
| 2D-C07 | app/services/redis_async.py:55-57, :169, :3-10 | "Key formats written by the sync side: RedisService.add_to_blacklist … and invalidate_tokens_issued_before_now" / "Stored by RedisService.setex" / "This module is the non-blocking path for those two callers" | Both keys are written through `AsyncRedisService` (security.py:584, 607). All routes (cart, products, admin stats, security) use the async client, not just "two callers". | "Key formats written by AsyncRedisService.add_to_blacklist and .setex (via invalidate_tokens_issued_before_now)…"; "the path for every caller on the event loop." |
| 2D-C08 | app/services/email/__init__.py:10 | "EMAIL_PROVIDER=logging (the default outside production)" | `EMAIL_PROVIDER: str = "smtp"` in every environment (config.py:376). Only tests/conftest sets `logging`. | "EMAIL_PROVIDER defaults to smtp; set it to logging (as the test suite does) to only log." |
| 2D-C09 | app/services/gdpr.py:133-136 | "The scheduled cleanup_inactive_accounts Celery task (app/tasks.py) performs the actual hard delete once DATA_DELETION_GRACE_PERIOD_DAYS has passed." | The task anonymises in place and never hard-deletes (tasks.py:301-310, 370-397). | "…anonymises the account (PII scrubbed, orders kept per Art. 17(3)(b)) once…" |
| 2D-C10 | app/services/email/messages.py:46-52 | "order is always an OrderResponse (Pydantic), not the SQLAlchemy Order model" | The Celery task passes an expunged ORM `Order` (tasks.py:247, as the same function's comment at :66-73 admits), and the expiry sweep passes a `SimpleNamespace` to `send_order_cancellation_email` (tasks.py:506-525). | "order is an OrderResponse from the routes, or a fully loaded detached Order from app/tasks.py…" |
| 2D-C11 | app/services/payment/stripe_provider.py:4-11 | "This project has no real Stripe account and never will … no `stripe` PyPI package is required. … (see tests/test_payment.py's timeout test)." | `LiveStripePaymentProvider` exists and is required in production (config.py:522-523). `stripe` is a hard dependency (pyproject). The test is `tests/integration/test_payment.py`. | "The in-process mock of Stripe (the live provider is live_stripe_provider.py)… see tests/integration/test_payment.py." |
| 2D-C12 | app/services/payment/exceptions.py:5-8 (also base.py:4-6) | "We deliberately do not depend on the real `stripe` package (see app/services/payment/README notes in stripe_provider.py)" / "swapping the mock provider for the real stripe SDK later is mostly a matter of writing a thin adapter" | The package is a dependency, and live_stripe_provider.py:20-22 translates SDK errors into these classes. There is no README. The adapter has been written. | "Provider-neutral exceptions; live_stripe_provider.py maps the SDK's errors onto them." |
| 2D-C13 | app/models/product.py:28-35 | "list_products (app/api/v1/products.py) filters on Product.characteristics[key].astext, plain JSON here would raise AttributeError" | products.py:279 filters with `Product.characteristics[key]["value"].as_string()`, and products.py:271-278 explains that `.astext` was the bug. | "JSONB on Postgres for storage and indexing; list_products uses the cross-dialect .as_string()." |
| 2D-C14 | app/api/v1/admin.py:388-389 | "see the identical note in orders.update_order_status - this shadows the fastapi.status module" | `update_order_status` has no `status` parameter (it reads `body.status`, orders.py:451) and contains no such note. | "Named "status" for the query param; it shadows fastapi.status here, hence the http_status alias." |
| 2D-C15 | app/tasks.py:301-302 | "User.orders and Address.user both cascade="all, delete-orphan"" | It is `User.addresses` (user.py:55) and `User.orders` (user.py:56). `Address.user` has no cascade. | "User.orders and User.addresses both cascade=…" |
| 2D-C16 | app/core/rate_limit.py:43, :302-303; app/api/v1/orders.py:1179; app/services/gdpr.py:96; app/services/email/messages.py:71 | "scripts/loadtest/results/rate-limiting-research.md" / "the load-test tracer wraps RateLimiter.check_rate_limit" / "(scripts/loadtest, FINDINGS.md)" / "scripts/loadtest/FINDINGS.md" / "scripts/loadtest/traced_celery.py" | `git ls-files` has no `scripts/` directory. The research is in docs/reports/research.md and the load reports in docs/reports/. | Point to `docs/reports/research.md` / `docs/reports/load-testing-3x.md`, or drop the path. |
| 2D-C17 | app/api/v1/cart.py:102 | "(merge_guest_cart_on_login)" | The function is `merge_guest_cart_on_login_async` (cart.py:195). | "(merge_guest_cart_on_login_async)" |
| 2D-C18 | app/tasks.py:286-289 | "Each account costs a bcrypt hash (~0.25 s at 12), so a batch of 100 is ~25 s: well inside task_soft_time_limit" | Measured 0.30 s, so about 30 s. The binding limit is the 30 s `idle_in_transaction_session_timeout`, not the 240 s soft limit (2D-01). Also contradicts config.py:151 "~400ms per hash". | Replace after fixing 2D-01: "one hash per batch, computed before the transaction opens". |
| 2D-C19 | app/core/celery_context.py:7-8; app/core/database.py:27-37 | "restored into the same contextvar the JSON log formatter reads" / "…skips cm.__exit__ (i.e. session.close() of a sync generator dependency)…" | The worker has no JSON formatter (2D-02). No sync session dependency exists any more: DbSession is async, and get_async_db's own comment (database.py:182-184) says the leak has no equivalent. | Fix 2D-02, then keep. For database.py, reword the preamble as history: "This was a sync-dependency leak; the timeouts below remain as a server-side backstop for every engine." |

Minor items not tabled (accurate enough or historical): stripe_provider.py:207-208 "e.g. RedisService" as a Depends example, which no route uses any more; schemas/common.py:410-411 "re-stamped by RedisService" (now also AsyncRedisService via the codec); security.py:93-96 mention of sync generator dependencies.

---

## 6. mypy triage (`uvx mypy app --ignore-missing-imports`, 91 errors, from `.cloud_audit/_mypy.log`)

| Group | Count | Verdict |
|---|---|---|
| `X? has no attribute page/size/sort_by…`, `Variable … is not valid as a type` (products.py, admin.py, users.py: `sortable_pagination(...)` dynamic classes) | 61 | False positive: runtime-built pydantic classes. |
| `Missing named argument "currency"` (common.py:348,362; cart.py:665; orders.py:1351) | 4 | False positive: pydantic fields with defaults or default_factory (no pydantic mypy plugin). |
| `SecretStr \| None` has no `get_secret_value` (database.py:67,81,105; tasks.py:153,160) | 5 | False positive: `assemble_db_connection` always returns a str. |
| gdpr.py:67,68,75,141,150 (`db` may be None) | 5 | False positive by construction: the writing methods are always built with `db` (legal.py:285,330). |
| reconciliation.py:191 (`ours_r` None) | 2 | False positive: the branch means `theirs_r is None`, so the key came from `ours`. |
| security.py:335, :482; users.py:1034; legal.py:338; cart.py:190, :651; stripe_events.py:72; text_input.py:78; stripe_provider.py:323,521,563 | 11 | False positive: guarded at runtime (`user_id is None` raise, schema requires password for deletion, guest with items has an id, `except (TypeError, ValueError)`, `isinstance(name, str) and …`, `.get(None)` returns the default). |
| Variance/annotation (order.py:214, product.py:66, logging_config.py:52, celery_context.py:71, rate_limit.py:101 NamedTuple `count`, users.py:703 `ErrorDetails`, cart.py:646/656 Literal, common.py:356 Literal, cart.py:668-669 `Decimal \| None`, tasks.py:525 SimpleNamespace) | 13 | False positive or typing-only (cart.py:668-669 sits under `if changed:`, which implies both are set). |
| **redis.py:114 / redis_async.py:226: `update_cart(cart)` passes `cart.user_id` (None for a guest) to `as_cart_owner`** | 2 | **Latent bug in dead code.** For a guest cart, `CartOwner.account(None).key` is `None` and `redis.set(None, …)` raises `DataError`, which is caught, so it returns False. `update_cart` has **no caller** in app/ (grep). Delete both `update_cart` methods or make them take the owner. Not reachable at runtime today. |

**No mypy finding indicates a runtime bug reachable from current code.** Given the 61 dynamic-class false positives, adopting mypy would need the pydantic plugin plus `Annotated` aliases for the `sortable_pagination` classes first.

---

## 7. Checked and found sound
- **expire_on_commit / MissingGreenlet:** `AsyncSessionLocal` and `SessionLocal` both use `expire_on_commit=False`. Every serialized order query carries `selectinload`/`joinedload` for address, items→product→variant (orders.py:90-107, users.py:900-903, admin.py:399-406). `OrderItem.product_name` is safe because the product is eager-loaded. After a `rollback()` the code reloads explicitly (orders.py:265-268, 1140-1145, 1430-1433). FastAPI 0.153 `scope="function"` exits *after* `serialize_response` (verified in fastapi/routing.py:147-152), so serialization runs inside the session. Background tasks receive pre-built `OrderResponse` snapshots. `get_current_user` expunges and commits, leaving a detached, column-complete snapshot.
- **Celery sessions:** the `task_session()` context manager always closes. Email tasks `expunge_all()` and `rollback()` before SMTP. The expiry sweep reads what the email needs before the commit. `process_stored_event` sets `expire_on_commit=False` for the shared payment helpers. Reconciliation commits before each provider call (no connection idles during Stripe calls). The engine is created at import but never connects in the prefork parent; children get their own pools, with telemetry set up in `worker_process_init`.
- **Pool sizing:** 4 workers × 2 pools × (10+5) = 120, plus Celery (one connection per prefork child). `max_connections=200` (compose) fits. `pool_pre_ping=True` and `pool_recycle=3600` are set. `statement_timeout`/`idle_in_transaction` are applied to the API (sync and async) and Celery engines, and are correctly *not* applied to Alembic. `QueryCanceled` is not mis-classified as an outage.
- **Session release:** `get_async_db` closes in the same task (`async with`). `session_scope` rolls back and closes in `finally`. login, reset and register close the session before bcrypt (`tests/integration/test_db_pool_concurrency.py`). The admin reconciliation thread uses its own sync session after committing the route's transaction.
- **Redis:** one async client and `BlockingConnectionPool` per event loop (`timeout=REDIS_POOL_TIMEOUT`), created lazily, with stale-loop cleanup and closed in lifespan. The sync client is a class-level singleton pool (redis-py resets the pool after fork via pid check). `AsyncRedisService` is per request but holds no connection, so there is no per-request client leak. Fail-open and fail-closed semantics are consistent (rate limit open, token checks raise `TokenStateUnavailable` and answer 503, login counter closed). The real-Redis parity suite passes (56/56).
- **Lifespan:** spans are flushed, the sync engine is disposed, the async engine is disposed with await, and async Redis is closed on the worker's loop. No custom signal handlers (uvicorn's graceful shutdown).
- **OTel:** off unless `OTEL_*` is set. `BatchSpanProcessor` (background thread, does not block the loop on `on_end`). Probes are excluded. Orphan CLIENT spans are dropped. Instrumentation is per process (after fork for Celery). No opentelemetry import when tracing is off.
- **Logging and injection:** `JSONFormatter` uses `json.dumps`, so CR/LF and quotes in messages are escaped (no log injection). Incoming `X-Request-ID` is limited to `[A-Za-z0-9._:-]{1,64}` and `traceparent` is strictly parsed. Exception text is never returned to clients. SMTP errors log a masked recipient. Settings use `hide_input_in_errors=True`.
- **In-flight limiter:** single-threaded counter, decremented in `finally`, probes exempt, sits outside the rate limiter.
- **Docker/compose:** the worker consumes all `task_queues` by default (no `-Q`, correct). `stop_grace_period 330s` is at least `task_time_limit`. Beat's schedule file is in `/tmp` (writable). The healthcheck targets `/health`, which returns 503 on DB or Redis failure. Postgres and Redis are bound to loopback. Redis uses `noeviction` + AOF. The app role has DML only and migrations run as the owner.
- **Migrations:** single head; full downgrade/upgrade round trip; offline `--sql` works; data-safety prechecks (lower-email duplicates, CHECK violations) are present; CONCURRENTLY index builds use `autocommit_block`. Postgres-only test files all pass on PG 16 (106 passed, 6 intentionally skipped).
