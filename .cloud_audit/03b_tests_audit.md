# 03B: Test-suite audit (mock tautologies, SQLite/PostgreSQL blind spots, flakiness, gaps)

Subagent 3B. Read-only on the repository: this file is the only one written. Every probe and mutation lived in
`/tmp/claude-0/-home-user-fastapi-e-commerce-backend/d6bb2a49-b210-591b-b7be-8a1e71874dff/scratchpad/3b/`:

- `mut_nolock.py`: a pytest plugin loaded with `-p mut_nolock`. The `MUT` environment variable selects the mutations:
  - `nolock`: `lock_stock_rows` becomes a no-op.
  - `nofu`: every `with_for_update()` is stripped.
  - `noskiplocked`: only `skip_locked` is dropped.
  - `asynchook`: the hook in `test_postgres_only` is rebound to `async_engine.sync_engine`.
  - `spy`, `putspy`: statement and response spies.
- `probe/test_3b_probe.py` and `probe/test_3b_welcome.py`: scratch tests, run with `-p conftest`.

For the PostgreSQL runs I started a throwaway PostgreSQL 16.15 cluster (initdb/pg_ctl, port 55433, `/var/tmp/pg3b`).
I used `/var/tmp` because the harness keeps resetting `/tmp/claude-0` to mode 700, which the postgres user cannot
traverse. The cluster was stopped and deleted afterwards, and so was its OS user.

Commands used (all from the repo root, after `source …/scratchpad/env.sh; unset UV_NATIVE_TLS`):

```
export PYTHONPATH=$PYTHONPATH:<scratch>/3b TEST_DATABASE_URL=postgresql+psycopg://test:x@localhost:55433/postgres
MUT=<m> uv run pytest <files> -q -p no:cacheprovider -p mut_nolock [-n 4] [-k ...]
```

---

## 0. Summary

| ID | Finding | Risk |
|---|---|---|
| 3B-01 | The PostgreSQL deadlock tests detect a missing `lock_stock_rows` only by luck of thread timing. Under `-n 4` (as in CI) they pass with all row locks removed. Builds on 2B-02 and corrects it in part | **High** (test gap on a correctness lock) |
| 3B-02 | `test_pay_waits_while_another_connection_holds_the_order_row` passes with **every** `FOR UPDATE` in the app removed. It proves the test's own lock, not the app's | Medium |
| 3B-03 | `test_two_concurrent_payments_for_one_order_charge_it_once` catches a double charge 3/3 when run alone, and 0/1 under `-n 4` | Medium |
| 3B-04 | 6 of the 16 app `FOR UPDATE` sites have no test that fails when the lock is removed. That includes refund and dispute webhooks, admin stock writes and replay | Medium |
| 3B-05 | `test_orders.py::test_cancel_from_{confirmed,processing,sent}` never leave `new`: the forward transitions get 409 and nobody asserts them. Three misnamed tests | Medium |
| 3B-06 | The `client` fixture stubs `send_welcome_email` for every test, which hides the 2C-05 crash (verified) | **High** (hid a confirmed bug) |
| 3B-07 | The PostgreSQL test engines omit `idle_in_transaction_session_timeout`, and the suite runs bcrypt at 4 rounds or with a stub. Together these hide 2D-01 (purge never completes) | **High** (hid a confirmed bug) |
| 3B-08 | `Numeric(10,2)` overflow: SQLite stores 120,000,000.00 and answers 201, PostgreSQL answers 500. No test exists, and the property strategies stop at 9,999.99 (verified) | Medium |
| 3B-09 | The `db_sensitive` file list leaves out files that are DB-dependent by their own docstring (NUL text, int4 bounds, Numeric, search, GDPR purge, pagination). They run on PostgreSQL only in the PG-15 full job, and never locally | Low-Medium |
| 3B-10 | The stateful stock property test cannot reach the bugs in its own domain: one product, no variants, success card only, no webhooks or refunds, no forward admin transitions, totals far below the overflow limit. Its invariant also pins 2B-06 | Medium |
| 3B-11 | SKIP LOCKED is "tested" through a Python fake of `_claim_event` and a SQL-shape check. No test has two PostgreSQL workers claim one event | Low |
| 3B-12 | Weak or tautological assertions (spy-only "enforces tolerance", a comment that claims an `unknown` check never made, `_decimal_to_cents` on both sides, string-built URI) | Low |
| 3B-13 | Provider methods covered only by direct calls (`expire_checkout_session`, `retrieve_checkout_session`) count toward the 100 % payment gate although no app code calls them. The in-app mock provider also counts | Low-Medium |
| 3B-14 | `pragma: no cover` (2 sites), `noqa`, skips and xfails: all reviewed, none risky. Dead `noqa` codes | Info |
| 3B-15 | Flakiness and order-dependence review: mostly sound; listed hazards | Low |
| 3B-16 | Gaps: behaviour with no test at all (per confirmed 2A/2B/2C/2D bug) | see table §6 |

---

## 1. Mock tautologies and assertions that cannot fail

### 3B-01: The deadlock regression tests depend on thread timing, not on the hook (High; refines 2B-02)

- **Test:** `tests/integration/test_postgres_only.py:148-181` (`_run_holding_after_first_stock_update`, which hooks
  `engine`, the **sync** test engine). The tests are `:190` and `:210-233`.
- **Production code:** `app/services/orders.py:64-91` (`lock_stock_rows`), called at `app/api/v1/orders.py:285` and
  `app/services/orders.py:117`.
- **What the tests claim:** "each is held, right after its first stock UPDATE, until the other has reached the same
  point".
- **What they actually verify:** the hook never fires, because the routes run on `async_engine` (2B-02). Whether the
  two requests overlap is left to thread scheduling.

**Measured** (PG 16, `-k deadlock`):

| Mutation | Run | Opposite-order checkout test | cancel-vs-checkout `[True]` | `[False]` |
|---|---|---|---|---|
| none | alone | pass | pass | pass |
| `nolock` (hook unchanged) | alone, 5 runs | **fails 5/5** | fails 1/5 | pass 5/5 |
| `nolock,asynchook` (hook fixed) | alone, 5 runs | fails 5/5 | fails 5/5 | pass 5/5 |
| `nofu` (all `FOR UPDATE` stripped) | **`-n 4` with 13 other files** | **pass** | **pass** | pass |
| `asynchook` only (code intact) | alone | pass (≈10 s: the barrier times out because the lock serialises, as designed) | pass | pass |

- So 2B-02's "would very likely pass with `lock_stock_rows` deleted" holds under CI load. On an idle machine the
  checkout test does catch it.
- `[False]` passing is correct, not a bug. The spy showed that the cancel restocks in `(2, 1)` order, so `[False]`
  is the same-order case.
- The variant-line FK `KEY SHARE` deadlock (2B-01) has **no** test at all. Every scenario here uses plain products.

**Fix:**
- `event.listen(async_engine.sync_engine, "after_cursor_execute", hold)`.
- Assert `len(seen) == len(actions)`, so that the test fails when the hook did not fire.
- Mark these tests `timing_sensitive`, or keep the barrier deterministic so that they do not depend on load.
- Add the 2B-01 variant scenario: A = `[variant V of P]`, B = `[plain P, variant V]`; assert `[201, 201]`.

### 3B-02: "pay waits for the order row lock" passes without any app lock (Medium)

- **Test:** `test_postgres_only.py:88-114`. **Production code:** `app/api/v1/orders.py:1047` (process_payment
  `FOR UPDATE`) and `:806` (`_lock_order_and_reload`).
- **What it claims:** "/pay waits while another connection holds the order row", which the module docstring presents
  as proof that the order row lock in the pay path works.
- **What it verifies:** the holder takes `FOR UPDATE` on the order. Any INSERT into `payments` (FK `KEY SHARE` on
  `orders`) or any UPDATE of the order then blocks, whether the app locks or not.
- **Mutation:** `MUT=nofu … -k "pay_waits or two_concurrent_payments"` → `pay_waits` **passed** 3/3 with every
  `with_for_update()` stripped.
- **Better test:** have the holder take `FOR KEY SHARE` (or `FOR SHARE`). That conflicts with the app's `FOR UPDATE`
  but not with the FK check. Then assert that `/pay` waits *before* it has written anything: no `payments` row exists
  while it is blocked. Alternatively, assert on `pg_locks` that the `/pay` backend waits on a `tuple`/`transactionid`
  lock whose query is the `SELECT … FOR UPDATE`.

### 3B-03: The double-charge test is load-dependent (Medium)

- **Test:** `test_postgres_only.py:117-143`. Two threads share one `client` (one portal loop), with a `Barrier(2)`
  before the POST.
- **Under `nofu`, run alone:** **fails 3/3** with `[200, 200]` (two successful charges for one order).
- **Same mutation in the 14-file `-n 4` run:** **passed**.
- **Improvement:** add a server-side barrier. Hook `async_engine.sync_engine` `after_cursor_execute` on the order
  `SELECT … FOR UPDATE`, and hold each request until both have read the order (with `timeout=3`, as in 3B-01).

### 3B-04: Lock sites with no failing test when the lock is removed (Medium)

`MUT=nofu` over 28 PG-run files (976 tests, `-n 4`). The 8 failures were all SQL-shape or structural tests in
`test_payment_concurrency.py` and `test_stripe_event_pipeline.py:307`. They cover:

- `orders.py:1047` (/pay)
- `orders.py:1358` (checkout-session)
- `stripe_events.py:131` (payment_intent webhooks)
- `orders.py:549` (customer cancel)
- `orders.py:463` (admin cancel)
- `tasks.py:494` (expiry)
- `stripe_events.py:539` (SKIP LOCKED)

**Not covered by any failing test:**

| Lock site | What it protects |
|---|---|
| `app/services/orders.py:84-91` (`lock_stock_rows`) | deadlock avoidance (only the timing-dependent 3B-01 tests) |
| `app/services/stripe_events.py:209` | refund events vs concurrent /refund or webhook (order total math) |
| `app/services/stripe_events.py:341` | dispute events |
| `app/services/stripe_events.py:585` | `_record_failure` (attempt counter) |
| `app/api/v1/admin.py:257`, `:810` | If-Match conditional product/variant write |
| `app/api/v1/admin.py:1050` | webhook replay |

The second run (`test_admin_orders`, `test_admin_webhook_events`, `test_refund_bookkeeping`, `test_etag`,
`test_merge_patch`, `test_products`, `test_webhook_matching`, `test_tasks`, `test_reconciliation`,
`test_order_status_machine`, `test_order_invariants`, `test_checkout_session`, `test_payment_state_machine`,
`test_payment_rules`) gave **448 passed under `nofu` and 448 under `noskiplocked`**.

**Improvement:** extend the cheap SQL-shape pattern of `test_payment_concurrency.py` (the `postgres_sql` fixture
compiling every ORM statement with the PG dialect) to the refund and dispute event handlers and to admin
PUT/PATCH-with-If-Match. For example: `assert all("FOR UPDATE" in s for s in _order_reads(postgres_sql))` after
`shop.webhook("refund.updated", …)`.

### 3B-05: Three cancellation tests never reach the status their name says (Medium)

- **Tests:** `tests/integration/test_orders.py:614` (`…from_confirmed…`), `:637` (`…from_processing…`) and `:661`
  (`…from_sent…`).
- **Setup:** the order is placed and never paid. `_transition` (`:579`) results for confirmed, processing and sent are
  not asserted.
- **Spy** (`MUT=putspy -k cancel_from`): `confirmed → 409 "Order is not paid…"`, then `processing → 409`,
  `sent → 409`, and finally `cancelled → 200`. All three tests cancel a **`new`** order.
- **Correction to 2B-06:** `test_orders.py:678-682` does not actually pin the restock of `SENT → CANCELLED`. The real
  pin is `tests/integration/test_order_status_machine.py:96-115` (`_order_in(..., payment_status="paid")`; it asserts
  `stock == 10` for `("sent","cancelled")`), together with `test_payment_chain.py:404-421` (`(cancelled, paid)` is the
  expected end state).
- **Fix:** pay the order first, `assert _transition(...).status_code == 200` at each step, and assert
  `order.status == "sent"` before the cancel.

### 3B-06: The global welcome-email stub hid 2C-05 (High; hid a confirmed bug)

- `tests/conftest.py:447`: the `client` fixture does
  `monkeypatch.setattr("app.api.v1.users.send_welcome_email", lambda *a, **k: True)`. Every HTTP test therefore
  registers with a stub.
- `test_auth.py:54-67` (`_registration_mail`) replaces the stub with a recorder, which never builds the message.
  `tests/unit/test_email_service.py:132-155` calls the real function with a **non-None** name.
- **Verified** (`probe/test_3b_welcome.py`, registering without `full_name`):
  - with the suite's stub: `202`;
  - with the real `send_welcome_email`: `AttributeError: 'NoneType' object has no attribute 'replace'`, from
    `app/services/email/messages.py:207` via `app/api/v1/users.py:212-217`.
- **Fix:**
  - Stub at the transport (`EMAIL_PROVIDER=logging` is already set in `conftest.py:32`) instead of at the
    message-builder name, so the builders run in every test.
  - Add a unit test `send_welcome_email("a@example.com", None, "tok")` and parametrize the registration payload with
    and without `full_name`.

### 3B-07: The test engines omit the server-side idle timeout, and bcrypt is near-free. Together they hid 2D-01 (High)

- **Production:** `app/core/database.py:56-62` connects with
  `-c idle_in_transaction_session_timeout=30000 -c statement_timeout=60000`.
- **Tests:** `tests/conftest.py:219-225` uses `_pg_options = "-c statement_timeout=60000"` only, for both `engine`
  (used by `TestingSessionLocal`, `tasks_db` and `session_scope`) and `async_engine`.
  `tests/integration/pg_pool_support.py` (`app_pool`) does use the production args, but only for 4 pool tests.
- **Bcrypt cost:**
  - `conftest.py:41` sets `BCRYPT_ROUNDS=4`.
  - `test_tasks_gdpr.py:208`, `:248` and `:262` replace `app.tasks.get_password_hash` with `lambda p: "hashed"`.
  - `PURGE_BATCH_SIZE=100` (`app/tasks.py:290`) times about 0.25-0.4 s per hash at production cost gives 25-40 s in
    one transaction, which exceeds 30 s. Not one test can come near that.
- **Fix:**
  - Build the test engines with `database_module._build_connect_args(TEST_DB_URL)`, so that the suite runs under the
    production server timeouts.
  - Add a PG test that runs `purge_expired_deletion_requests` with `idle_in_transaction_session_timeout` set to
    `'200ms'` (SET on the session) and a `get_password_hash` stub that sleeps 50 ms. Assert that all accounts are
    anonymised. Today that test would fail, which is the regression guard for 2D-01.

### 3B-12: Smaller tautologies and assertions that miss the point (Low)

- `tests/unit/test_live_stripe_provider.py:586-602`: `test_construct_webhook_event_enforces_custom_tolerance` wraps
  `verify_header` with a spy and asserts only that `42` was forwarded. It never sends a stale timestamp. The live
  adapter's own check (`live_stripe_provider.py:518`) is covered in `tests/unit/test_webhook_signature.py`
  (parametrized over providers), so this test adds nothing. Rename it, or sign with `timestamp=now-43` and assert
  `SignatureVerificationError`.
- `tests/unit/test_stripe_rate_limit_mapping.py:118-123`: the comment says "Verify the attempt was flagged as unknown
  status", but only the order's status is asserted, never `Payment.status == "unknown"`. The behaviour is covered at
  `test_payment.py:498` and `test_payment_paths.py:351`, so the risk is low. Add
  `assert [p.status for p in reloaded_order.payments] == ["unknown"]`.
- `tests/integration/test_order_invariants.py:192`: `payment.amount == _decimal_to_cents(expected_total)` uses the
  production converter on both sides. A broken converter would agree with itself; the conversion is separately
  pinned in `tests/unit/test_money_properties.py:57-73`. Prefer
  `payment.amount == int(expected_total * 100)`.
- `tests/unit/test_config_matrix.py:129-133`: `DATABASE_URI` is compared with a hand-written f-string, using an
  alphanumeric password (`"p"`; `test_config_secrets.py:18` uses `"p"*24`). This is why 2D-04 (no URL-encoding) was
  missed.
  - **Better:** `make_url(s.DATABASE_URI.get_secret_value()).password == "p@ss:w/rd%"` with
    `POSTGRES_PASSWORD="p@ss:w/rd%"`.
- Status-code-only tests: an AST scan flagged about 60 mutating-request tests whose every assert is on
  `status_code`. Almost all are refusals (401/403/404/422), where status is the point.
  - Worth strengthening: `test_admin_orders.py:496` (`refund amount must be positive`). Also assert that no
    `PaymentRefund` row was written and that the provider was not called.
  - Worth strengthening: `test_admin.py:593` (variant SKU race → 409). Also assert the variant count is unchanged.
- No case was found of a test patching the very function under test. The `monkeypatch` targets in the payment,
  webhook and auth suites patch collaborators (provider calls, `apply_event`, `_claim_event`, the clock, email),
  usually to inject a failure, and the assertions then check DB state. Two examples:
  - `test_stripe_event_pipeline.py:329-361`;
  - the `_mark_paid_from_another_session` race simulation in `test_payment_concurrency.py`.

  The in-app mock provider (`app/services/payment/stripe_provider.py`) is not an "always success" fake: decline, 3DS,
  timeout and refund-failure branches are driven through documented test tokens (`tests/unit/test_mock_test_cards.py`,
  `test_payment_paths.py`). The live adapter is checked against `tests/unit/fake_stripe_api.py`, which is written from
  Stripe's docs and shares its contract tests with the mock (`test_payment_provider_contract.py:83`,
  `params=["mock","live"]`). The real-Stripe tests are skipped in CI (no key).

---

## 2. SQLite vs PostgreSQL blind spots

### How the database is chosen and the marker applied

- `tests/conftest.py:118-121`: `TEST_DATABASE_URL` selects PostgreSQL; otherwise a temp SQLite file, with WAL,
  `StaticPool` for sync and `NullPool` aiosqlite.
- `:138-157`: `db_sensitive` is applied by **file-name glob** (`:171-178`). It selects 2,319 of 5,344 tests.
- `:126-129`: `requires_postgres` skips **32** tests on the default SQLite run. They are in the `test_postgres_*`
  files plus `test_idempotency_fingerprint.py:348`.
- `test_real_redis_parity.py` skips **28** `real` parametrizations without `REAL_REDIS_URL`.
- Six tests run **only on SQLite** (`_needs_no_row_locks`: `test_payment_paths.py:187,305,616` and
  `test_idempotency_fingerprint.py:151,229,322`). They simulate a race that PG row locks prevent.

CI (`.github/workflows/ci.yml`):

| Job | Database | Selection |
|---|---|---|
| `test` | SQLite | all |
| `test-postgres` 15 | PostgreSQL 15 | full suite minus `timing_sensitive` |
| `test-postgres` 16, 18 | PostgreSQL 16, 18 | `db_sensitive` only |
| `test-redis` | real Redis | `real_redis` only |

The 100 % payment-layer coverage gate is computed from the **SQLite** job only, so branches reachable only on PG
(deadlock → 503, `QueryCanceled`) do not count toward it.

### Behaviour matrix

| Behaviour | On SQLite | Real PG test? | Verdict |
|---|---|---|---|
| `FOR UPDATE` (16 sites) | dropped from SQL | Partly: the SQL-shape tests (§3B-04) cover 7 sites. The real-blocking tests are weak (3B-01/02/03) | **Gap** for 6 sites and for every real interleaving |
| `SKIP LOCKED` (`stripe_events.py:539`) | dropped | Only the SQL shape (`test_stripe_event_pipeline.py:307`) and a Python fake (`:329-361`, used on PG too) | 3B-11: no two-worker PG test |
| `NOWAIT` | not used | n/a | — |
| Deadlocks (40P01 → 503) | impossible | Only the 3B-01 tests, which are load-dependent; the FK `KEY SHARE` variant (2B-01) is untested | **Gap** |
| Isolation (READ COMMITTED re-read after lock) | serialised writer | Simulated with a second session (`test_payment_concurrency.py`) | acceptable |
| CHECK constraints | **enforced** by SQLite too (`create_all`) | `test_postgres_only.py:236-269` and `test_postgres_constraint_names.py` assert sqlstate 23514 and the names | covered |
| `Numeric(10,2)` precision/overflow | **not enforced**: verified 120,000,000.00 is stored and answers 201 | none; PG answers **500** (verified, `probe/test_3b_probe.py`) | **Gap** (3B-08, 2B-07) |
| Integer int4 bounds | 64-bit | `test_int_bounds.py` (not `db_sensitive`; PG 15 job only) | covered on PG 15 only |
| NUL in text | stored | `test_text_input.py` (not `db_sensitive`; PG 15 only) | covered on PG 15 only |
| VARCHAR(n) | not enforced | only `String(3)`/`String(64)` columns, all server-set | n/a |
| tz-aware datetimes | naive storage, `UTCDateTime` decorator | `test_timestamps.py`, `test_dst_clock.py`, the TZ matrix job (SQLite) | covered |
| `lower(email)` unique index | index exists, `lower()` is ASCII-only | `test_postgres_only.py:272-296` (ASCII case only) | Low: non-ASCII case-fold relies on Python `normalize_email`; no PG test with e.g. `É` |
| JSON characteristics filter (`.as_string()` → `->>` / `json_extract`) | json_extract | `test_products.py` (not `db_sensitive`; PG 15 only) | covered on PG 15 only |
| ILIKE search, collation ordering | LIKE (ASCII-insensitive), BINARY collation | `test_catalog_search.py`, `test_pagination_order.py`, `test_list_sorting.py` (PG 15 only) | Low |
| `server_default` | rendered in DDL by `create_all` | the PG schema is built by Alembic (`conftest.py:200-213`) and `compare_metadata` checks drift (`test_postgres_only.py:350-361`) | covered |
| `idle_in_transaction_session_timeout` | n/a | **not set on the test engines** (3B-07); only `app_pool` (4 tests) | **Gap** (hid 2D-01) |
| `statement_timeout` | n/a | `test_postgres_statement_timeout.py` (`SET LOCAL 300ms` on `app_pool`) | covered |
| Pool pre-ping and terminated backends | n/a | `test_postgres_pool_recovery.py` | covered |
| Migrations up/down/up and drift | n/a | `test_postgres_only.py:324-380`, `test_postgres_data_migrations.py` | covered |
| FK `KEY SHARE` conflicts (INSERT `order_items`/`payments` vs `FOR UPDATE`) | none | none intended. It accidentally makes 3B-02 pass | **Gap** |

### 3B-09: The `db_sensitive` glob misses files that are DB-dependent by their own docstring (Low-Medium)

These files are not matched by `conftest.py:138-157`, so they never run on PG 16/18 and never run locally:

- `test_text_input.py` ("only a PostgreSQL …")
- `test_int_bounds.py` ("integer out of range on PostgreSQL")
- `test_catalog_money.py` (Numeric)
- `test_catalog_search.py`
- `test_pagination_order.py`
- `test_list_sorting.py`
- `test_products.py` (JSON filter)
- `test_gdpr_flows.py`, `test_data_retention.py` (purge, the 2D-01 area)
- `test_stored_rows_read.py`
- `test_timestamps.py`
- `test_db_session_scope.py`
- `test_db_dependency_errors.py`
- `test_admin.py` (product lock `admin.py:257`)

**Fix:** add these names to the tuple. Better still, invert the rule: mark everything that uses `db_session`, `client`
or `tasks_db` as `db_sensitive` and opt files out.

### Redis

- fakeredis backs every test (`conftest.py:378-411`). `REDIS_PORT=1` (`:22`) makes any test that escapes the fake
  fail loudly, which is good.
- `test_real_redis_parity.py` (19 tests, run on both backends) covers what the app uses:
  - the Lua counter and `register_script` reload;
  - `SET NX` (mark_once);
  - `WATCH`/`MULTI` retries for cart and merge;
  - TTL and `setex`;
  - the blacklist and the watermark.
- No parity gap was found. The rate-limit lockout routes are, however, tested only with the limiter switched on per
  test (`RATE_LIMIT_ENABLED=false` globally, `conftest.py:36`). See 3B-16 on 2C-04.

---

## 3. `pragma: no cover`, `type: ignore`, `noqa`, skip/xfail, coverage config (3B-14)

- **Coverage config** (`pyproject.toml [tool.coverage.*]`):
  - `branch = true`, `concurrency = ["greenlet","thread"]`, `fail_under = 85`;
  - **no `exclude_lines`/`exclude_also`**, so only coverage's default `pragma: no cover` applies;
  - CI adds a 100 % gate on 14 payment files (ci.yml, "Payment layer coverage gate").
- **`pragma: no cover` in `app/`: 2 sites, both re-raises after an `IntegrityError`:**
  - `app/api/v1/orders.py:278` (create_order flush). The only unique index on `orders` is
    `uix_order_user_idempotency_key` (`models/order.py:68-73`). The FKs point at a user and an address that are never
    hard-deleted (addresses are soft-deleted, `users.py:791-826`; erasure anonymises). The CHECKs cannot fail at flush,
    because the total is 0 and the statuses are constants.
  - `app/api/v1/orders.py:1440` (checkout-session payment INSERT): `payment_intent_id` is a fresh uuid placeholder,
    the amount is ≥ 0, and the order row is locked.
  - **Verdict:** both are genuinely unreachable today, and both fail safe (re-raise → generic 500, no money moved).
    **Low risk.** Note that they sit inside the 100 %-gated payment file, so adding a CHECK or unique index to
    `orders`/`payments` later would silently make them reachable without failing the gate.
- **`type: ignore`:** 0 in `app/` and `tests/`.
- **`noqa` in `app/`:** 5, all import placement or the documented SQLAlchemy `== False`. Fine.
- **`noqa` in `tests/`:** 23, mostly `F401`/`F811` for fixtures imported across files. `DTZ005`, `DTZ001`, `DTZ003`
  and `B017` (`test_payment_time.py:136`, `test_clock.py:46,62,112`, `test_checkout_session_live_stripe.py:113`) name
  rules that ruff does not select (`select = ["E","F","I","W"]`), so they are dead suppressions. The `ignore` list also
  names `UP042`/`UP046`/`SIM300`, which are not selected either. Info only.
- **Skips and xfails:**
  - no `pytest.mark.skip` and no `xfail`; `strict_xfail = true` is set;
  - every `skipif` is environmental: `requires_postgres`, `REAL_REDIS_URL`, the live Stripe key, the 6 SQLite-only
    `_needs_no_row_locks` tests (sound: PG prevents the simulated race), and `test_payment_live_journey.py:81`
    (`STRIPE_LIVE_SLOW`).

---

## 4. Flaky, order-dependent and time-dependent tests (3B-15)

- **Timing-sensitive tests** run alone, by marker (`conftest.py:165-178`; CI runs them in a separate serial step).
  The bcrypt overlap tests (`test_auth.py:1405-1470`, `elapsed < delay*3`) are correctly in that list.
- **The PG concurrency tests in `test_postgres_only.py` are not marked `timing_sensitive`**, yet their outcome depends
  on scheduling (3B-01, 3B-03). Under `-n auto --dist worksteal` they stay green but stop detecting regressions. They
  are not flaky in the red direction: they are flaky in the *green* direction, which is worse for a regression guard.
- **freezegun:** about 80 uses. `tests/integration/time_support.py` patches freezegun internals and `time.timezone`
  via `monkeypatch`, which is undone after each test. `default_ignore_list = []` freezes the TestClient threads too,
  so this is deterministic. No `tick=True` was found.
- **Sleeps:** `test_real_redis_parity.py` sleeps 1.2 s waiting for TTL expiry against the real server (fine, only in
  the `test-redis` job). `test_health.py:195` sleeps 30 s in a coroutine that is cancelled. No naked sleeps in the
  DB tests except the 3 s barrier timeouts.
- **Global state:**
  - `settings` mutations use `monkeypatch`, except `tests/unit/test_rate_limit.py:193-198`, which uses a
    `try/finally` restore (fine).
  - `app.dependency_overrides` is restored in `finally` (`test_stripe_rate_limit_mapping.py:105,149,197`).
  - **`StripePaymentProvider`'s mock state is class-level** (`stripe_provider.py:215-220`). Most payment files reset
    it with an autouse fixture. `test_response_headers.py`, `test_db_dependency_errors.py` and
    `test_no_blocking_on_loop.py` do not. With worksteal, a fixed `Idempotency-Key` in one of them could replay an
    intent left by an earlier test on the same worker.
  - **Fix:** move `StripePaymentProvider.reset_mock_state()` into a root autouse fixture.
- **DB isolation:** dirty-table DELETE before each test (`conftest.py:302-361`), with a schema rebuild after DDL on
  SQLite. On PG, `lock_timeout 10s` turns a leaked lock into an error instead of a hang. Each xdist worker gets its own
  SQLite file or PG database. Sound.
- **Hypothesis:** the `ci` profile is `derandomize=True, database=None`, so the runs are reproducible.

---

## 5. Property-based tests and Schemathesis (3B-10)

- **Schemathesis:** not used (not in the dependencies, no import). OpenAPI-driven negative testing is approximated by
  `test_int_bounds.py` (bounds derived from `app.openapi()`) and `tests/unit/test_response_round_trip.py`.
- **Hypothesis:** 44 `@given` tests in 17 files. Strategy quality:
  - **Good:**
    - `test_payment_provider_contract.py:281-305` (refund partials, every store currency, mock and live);
    - `test_money_properties.py` (the full 0.01 to 99,999,999.99 range, float rejection);
    - `test_cart_properties.py`;
    - `test_dst_zones.py`.
  - **`test_order_invariants.py:217` (stateful stock invariant).** The operation alphabet (`:200-212`) is
    `order/add/checkout/pay(success card only)/cancel_by_customer/cancel_by_admin/expire`, over **one product, no
    variants**, single-threaded. It therefore cannot generate:
    - 2A-01/2B-03 (expiry while an attempt is `requires_action`): no 3DS token, no checkout session;
    - 2A-03 (out-of-order refund events): no webhooks, no refunds;
    - 2B-01 (plain + variant line of one product);
    - 2B-06 (cancel from `sent`): no forward admin transitions;
    - 2B-07 (overflow): price 5.00, quantity ≤ 4.

    Its invariant `stock == STOCK - live_quantity` also **encodes** "every cancellation restocks", so it would pin
    2B-06 even if it reached `sent`. Money is never asserted: a paid order cancelled by an admin keeps
    `payment_status=paid` with no refund.
  - **Improvements:**
    - Add the operations `pay_3ds` (`PAY_3DS`), `complete_3ds` (a `payment_intent.succeeded` webhook), `refund`,
      `refund_event(stale status)`, `advance_status`, and a second product with a variant.
    - Add the money invariant `sum(succeeded payments) - sum(succeeded refunds) == (order paid ? total : 0)` for every
      non-flagged order, plus "no succeeded payment on a cancelled order without a refund or manual-review entry
      listed somewhere".
  - **`test_order_invariants.py:142` (order total):** prices ≤ 9,999.99, ≤ 4 lines, quantity ≤ 5, so totals stay
    below 200,000 and never approach the `Numeric(10,2)` limit. Each line is its own product, so it never builds the
    plain + variant combination of one product. Widen `prices` to the schema maximum (99,999,999.99) and expect 201
    or a clean 4xx, never 500.

---

## 6. Why the suite did not catch the confirmed bugs, and gaps (3B-16)

| Bug | Existing test(s) | What they assert / what they miss | Suggested test |
|---|---|---|---|
| 2A-01 / 2B-03 expiry during an in-flight attempt | `test_payment_chain.py:426-445`, `test_order_expiry.py:327` | Both **assert the flawed outcome as intended** (cancelled, flagged `requires_manual_review`). They never check that the money can be refunded (`/refund` → 409) or that the flag appears in any list. `live_stripe_provider.py` session params (no `expires_at`) are never asserted | After `pay(PAY_3DS)` and aging: `assert shop.expire_unpaid(order_id) == 0`; separately, spy `SessionService.create` and assert `"expires_at" in params` |
| 2A-02 reconciliation skips lost Checkout webhooks | `test_reconciliation.py:470-479`, `:678-687` | **Pin `skipped == 1` as correct** | Set `_sessions_by_id[sid]` to complete/paid and assert a `KIND_STATUS` discrepancy |
| 2A-03 stale refund event regresses `succeeded` | `test_refund_lifecycle.py:179-195` | Covers only "failed stays failed". No "succeeded then pending" sequence | Refund → `refund.updated(succeeded)` → `refund.created(pending)`; assert the order stays `refunded` and the ledger stays `succeeded` |
| 2A-04 two payable sessions | docstring of `test_payment_concurrency.py:15-23` | **Documents the double charge as accepted** (flagged) | After session 2 succeeds, assert session 1 is `expired` at the mock |
| 2A-05 reconciliation cap | `test_reconciliation.py:483-499` | Asserts `truncated` and nothing about older rows never being reached | Two runs with cap 2 and 3 payments: assert the oldest is checked on the second run |
| 2A-07 stale `processing` → 409 forever | `test_payment_rules.py` (409 while in flight) | No test ages a `processing` row past the stale window with the same key | Age the attempt and replay the key: expect a final state, not 409 |
| 2B-01 FK `KEY SHARE` deadlock | none (3B-01) | — | §3B-01 variant scenario |
| 2B-02 hook on the wrong engine | §3B-01 | — | — |
| 2B-04 merge patch is not RFC 7396 | `test_merge_patch.py:260-430` | Top-level members only; `attributes` appears only as a null-on-non-nullable case (`:388`) | PATCH `{"characteristics":{"color":{…}}}` keeps `size`; `{"attributes":{"size":null}}` removes the key |
| 2B-05 absolute stock write | `test_etag.py`, `test_admin.py` | No sale between GET and PUT | GET → checkout → PATCH without If-Match: assert the sold units are not resurrected (or 428) |
| 2B-06 `sent→cancelled` restock | `test_order_status_machine.py:96-115`, `test_payment_chain.py:404-421` (pin); `test_orders.py:661` is misnamed (3B-05) | Pins the behaviour as intended | — (business decision) |
| 2B-07 `Numeric(10,2)` overflow | none; strategies capped (3B-10) | SQLite stores 120,000,000.00 → 201; PG → 500 (verified) | 2 × 60,000,000.00: assert 409/422 on both databases |
| 2B-08 delete vs checkout race | `test_admin.py:369` (no orders) | Status only | PG test holding a checkout's lock, then DELETE: expect 409 or a soft delete, never 500 |
| 2B-09 guest merge drops lines | `test_guest_cart_limit.py` | — (not checked in detail) | Merge 100 + 5 lines and assert the user is told |
| 2B-11 expiry sweep aborts | `test_order_expiry.py` | No failing order in the middle of a batch | Make one order's restock raise and assert the others still expire |
| 2C-01 events after erasure keep PII | `test_data_retention.py` | Covers only events stored *before* erasure | Erase, then store an event for that customer, run the purge, assert it is redacted |
| 2C-02 / 2C-03 login/refresh racing a reset | `test_auth.py:710` (reset invalidates *issued* tokens) | No interleaving | Block login in `verify_password_async`, reset meanwhile, assert the minted token is rejected |
| 2C-04 deletion endpoint is an unthrottled password oracle | `test_rate_limit_api.py:76` checks named routes exist | No test enumerates every route calling `verify_password_async` | AST/route scan: every route that verifies a password is in the limiter's map **and** counts toward the per-email lockout |
| 2C-05 welcome email crash | stubbed (3B-06) | — | §3B-06 |
| 2C-06 address edit rewrites orders | `test_merge_patch.py:149` (address PATCH) | Never re-reads an order | PATCH the address of a delivered order: assert `GET /orders/{id}` still shows the old street |
| 2C-07 consent via `/users/me` without history | `test_users.py:169`, `test_merge_patch.py:110` | Null handling only | PATCH `marketing_consent:true`: assert `consent_history[-1].granted` and `marketing_consent_date` |
| 2C-10..13 GDPR | `test_tasks_gdpr.py` | Hash stubbed; no in-flight/paid/disputed orders during erasure | Erase a user with a paid, unshipped order: assert the address is kept or the erasure is deferred |
| 2D-01 purge vs idle timeout | stubbed hashes, no server timeout (3B-07) | — | §3B-07 |
| 2D-02 Celery logging | `test_celery_context.py`, `test_logging_config.py` | In-process only | Import `app.tasks` in a subprocess with `celery worker` signals and assert JSON log lines |
| 2D-04 URI not URL-encoded | `test_config_matrix.py:129-133` (3B-12) | Alphanumeric password | §3B-12 |

### 3B-13: Coverage inflated by code no caller uses (Low-Medium)

- `PaymentProvider.expire_checkout_session` and `retrieve_checkout_session` are covered by direct calls
  (`test_checkout_session.py:184-199`, `test_payment_provider_contract.py:392-432`).
- **No app code calls them** (2A-01, 2A-02). The 100 % payment gate is therefore met by dead code paths.
- `app/services/payment/stripe_provider.py` (the in-app mock) is inside the gated file set. Its test-card branches
  count toward "payment layer 100 %".
- **Suggestions:**
  - exclude the mock from the gate, or gate `live_stripe_provider.py` separately;
  - add a "no unused provider method" check (an AST grep that each `PaymentProvider` abstract method has a non-test
    caller).

---

## 7. Checklist of files reviewed

**Read in full or for the relevant parts:**

- `tests/conftest.py` (the only conftest)
- `pyproject.toml` (pytest/coverage/ruff)
- `.github/workflows/ci.yml`
- `tests/integration/test_postgres_only.py`
- `tests/integration/test_postgres_statement_timeout.py`
- `tests/integration/test_postgres_pool_recovery.py`
- `tests/integration/test_postgres_indexes.py`
- `tests/integration/pg_pool_support.py`
- `tests/integration/payment_support.py`
- `tests/integration/time_support.py`
- `tests/integration/test_payment_concurrency.py`
- `tests/integration/test_payment_chain.py`
- `tests/integration/test_order_invariants.py`
- `tests/integration/test_order_status_machine.py`
- `tests/integration/test_orders.py` (cancel section)
- `tests/integration/test_order_expiry.py` (late-payment section)
- `tests/integration/test_refund_lifecycle.py`
- `tests/integration/test_reconciliation.py` (skip/cap tests)
- `tests/integration/test_stripe_event_pipeline.py` (SKIP LOCKED tests)
- `tests/integration/test_tasks_gdpr.py`
- `tests/integration/test_webhook_signature_sdk.py`
- `tests/integration/test_auth.py` (registration-mail helper, bcrypt timing tests)
- `tests/integration/test_rate_limit_api.py` (test list)
- `tests/integration/test_merge_patch.py` (test list)
- `tests/integration/test_real_redis_parity.py` (backend fixture, test list)
- `tests/integration/test_db_pool_concurrency.py` (header)
- `tests/unit/test_stripe_events_enqueue.py`
- `tests/unit/test_stripe_rate_limit_mapping.py`
- `tests/unit/test_live_stripe_provider.py` (webhook tests)
- `tests/unit/fake_stripe_api.py` (header)
- `tests/unit/test_payment_provider_contract.py` (test list)
- `tests/unit/test_money_properties.py`
- `tests/unit/test_config_matrix.py` (URI test)

**Sampled (headers, greps, AST scan):**

- `test_postgres_constraint_names.py`
- `test_postgres_data_migrations.py`
- `test_payment.py`
- `test_payment_paths.py`
- `test_idempotency_fingerprint.py`
- `test_checkout_session.py`
- `test_disputes.py`
- `test_admin.py`
- `test_admin_orders.py`
- `test_int_bounds.py`
- `test_text_input.py`
- `test_catalog_money.py`
- `test_catalog_search.py`
- `test_pagination_order.py`
- `test_list_sorting.py`
- `test_gdpr_flows.py`
- `test_data_retention.py`
- `test_timestamps.py`
- `test_stored_rows_read.py`
- `test_products.py`
- `test_cart.py`
- `test_users.py`
- `test_health.py`
- `tests/unit/test_config_secrets.py`
- `tests/unit/test_email_service.py`
- `tests/unit/test_rate_limit.py`

**Whole-tree:**
- greps over all 182 test files for mocks, `monkeypatch` targets, skip/xfail, `noqa`, sleeps and freezegun;
- an AST scan for status-only and assert-free tests.

**Executed:**

| Run | Database | Result |
|---|---|---|
| deadlock tests, 4 mutation combos × up to 5 runs | PG | see 3B-01 |
| 28 files (976 tests) with `nofu` and `noskiplocked` | PG | see 3B-04 |
| `pay_waits`/`two_concurrent` under `nofu` | PG | see 3B-02, 3B-03 |
| `test_orders.py -k cancel_from` with the PUT spy | SQLite | see 3B-05 |
| overflow probe | SQLite and PG | see 3B-08 |
| welcome-email probe | SQLite | see 3B-06 |
| PG-only/real-Redis skip count, `db_sensitive` collection count | — | §2 |

The baseline, without mutation, was green in every run.
