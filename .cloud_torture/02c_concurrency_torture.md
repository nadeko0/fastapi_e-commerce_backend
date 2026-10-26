# Phase 2c — Concurrency & race torture

Date: 2026-10-26 · Project 4.0.3 · File: `tests/torture_suite/test_concurrency_torture.py`
(97 collected tests: 21 test functions, most parametrized over rounds). Scratch artefacts
(raw pytest output of every run, fix-prototype plugins): `.cloud_torture/scratch_2c/`.

## Harness

* **One event loop, real concurrency.** `burst()` runs `asyncio.run()` over an
  `httpx2.AsyncClient(transport=ASGITransport(app, raise_app_exceptions=False))`; each of the
  20–50 requests awaits one `asyncio.Barrier(n)` and then all enter the app together
  (`asyncio.gather`). `raise_app_exceptions=False` lets an unhandled error come back as the app's
  own 500 so it is counted instead of aborting the burst. The async engine is the suite's NullPool
  engine (one PG connection per in-flight request, ≤ 50 per burst). Provider calls
  (`run_in_threadpool`) really overlap in AnyIO's thread pool.
* Setup (catalog, verified buyers, admins, carts, logins, paid orders) goes through the project's
  `payment_support.Shop` and the sync `client` fixture; webhook *worker* runs use
  `stripe_event_support.run_event` from a `ThreadPoolExecutor` (≤ 10 threads, sync engine).
* Two-transaction interleavings that need a request to *block inside a sync section*
  (`db.run_sync`) use one thread + one `asyncio.run` loop per request
  (`test_bug_variant_line_…`), and poll `pg_stat_activity.wait_event_type = 'Lock'`.
* `check_responses()` after every burst: no 5xx, status set ⊆ allowed, every ≥ 400 is
  `application/problem+json` with `status` matching. It prints `HIST {...}` (visible with `-s`).
* After every burst DB invariants are checked with a **fresh** `TestingSessionLocal()` session
  (and the fake Redis keyspace for token state).
* `test_harness_runs_requests_concurrently_on_one_loop` (also runs on SQLite) proves overlap:
  ≥ 10 of 20 requests were inside the route at the same time.
* `test_bug_*` = deterministic regressions; the race window is widened only from the test
  (a hook around an app function, a slow fake-Redis `SMEMBERS`, a slow mock-provider lookup).
  No `app/` edit, no xfail markers.

## Scenarios, concurrency, rounds

| # | Test | Concurrency × rounds | Invariants asserted |
|---|---|---|---|
| 1a | `test_refresh_token_reuse_race_has_one_winner_and_burns_the_family` | 30 × 6 | exactly one 200, 29 × 401 problem+json; winner's new refresh token is **revoked** (reuse detection burns the family, the documented design of `rotate_refresh_token`); no live jti of the family in Redis; original token spent |
| 1b | `test_refresh_racing_logout_leaves_no_live_refresh_token` | 30 (1 logout + 29 refresh) × 6 | logout 200; ≤ 1 refresh 200; afterwards no redeemable refresh token of the family |
| 1c | `test_bug_logout_racing_refresh_leaves_a_live_refresh_token` | deterministic | see BUG-1 |
| 2a | `test_order_same_key_twins_create_one_order` | 30 × 6 | all 201, one id/Location, one order, one item row, one stock decrement |
| 2b | `test_order_same_key_different_address_executes_once` | 30 (15/15) × 6 | one order; winner's payload → 201 replay, other → 422 `idempotency-key-reused` |
| 2c | `test_bug_order_replay_racing_its_first_request_answers_cart_empty` | deterministic (PG + SQLite) | see BUG-2 |
| 2d | `test_pay_same_key_twins_charge_once` | 30 × 6 | 200 (paid) or 409 `idempotency-key-in-use`; one Payment row, one PaymentIntent, order paid |
| 2e | `test_pay_same_key_different_bodies_execute_once` | 30 (15/15) × 6 | one attempt/charge; other body → 422 |
| 2f | `test_pay_distinct_keys_charge_once` | 30 keys × 6 | exactly one 200, rest 409; one charge |
| 2g | `test_refund_same_key_twins_refund_once` | 30 × 6 | one refund id, one `payment_refunds` row, `amount_refunded == amount`, order refunded |
| 2h | `test_refund_same_key_different_amounts_execute_once` | 30 (15/15) × 6 | one refund; other amount → 409 |
| 2i | `test_refund_distinct_keys_never_over_refund` | 30 × 6 (1.00 each on 19.99) | 19 × 200, 11 × 400; provider total == recorded total == `amount_refunded` == 1900 |
| 2j | `test_bug_mock_provider_refund_key_is_not_atomic` | 5, deterministic | see BUG-5 |
| 2k | `test_webhook_redeliveries_and_workers_apply_once` | 30 deliveries + 10 workers × 6 | 1 `received` + 29 `already_processed`; one stored row; applied once (one email), attempt succeeded, order paid |
| 2l | `test_concurrent_cancellations_restock_once` | 30 (15 customer /cancel + 15 admin PUT status) × 6 | one 200, rest 409; product and variant stock restored exactly once |
| 3a | `test_last_unit_contention_never_oversells_or_deadlocks` | 40 buyers × 8 | stocks 1–3, 2–3-line carts over plain + variant lines of the same product, random line order (A,B vs B,A); stock ≥ 0, `initial − final == Σ ordered`, every order complete (all-or-nothing), 409 only `insufficient-stock` and only if some line is short even at the end (no spurious 409), refused/failed checkouts leave no order; **no 5xx** |
| 3b | `test_plain_and_variant_lines_of_one_product_do_not_deadlock` | 30 × 6, ample stock | every checkout 201 (no 40P01 → 503) + invariants above |
| 3c | `test_bug_variant_line_checkout_deadlocks_with_plain_line_checkout` | 2 threads, deterministic | see BUG-3 |
| 4a | `test_erasure_racing_the_accounts_activity` | 30 × 5 (1 erasure + 8 cart adds + 4 checkouts + 2 /pay + 4 refresh + 4 /me + 2 address creates + 5 webhook deliveries incl. 3 duplicates, shuffled) then purge task ∥ 4 webhook workers | no 5xx; erasure 200; account inactive; every refresh token ever handed out (incl. ones minted during the race) → 401; `/me` → 403 `account-inactive`; login → 401; purge anonymises once; all events processed, order paid; no PII left in `users`, `addresses` (incl. addresses created during the race), `webhook_events` |
| 4b | `test_bug_webhook_stored_after_erasure_keeps_personal_data` | deterministic (PG + SQLite) | see BUG-4 |

## Runs

PostgreSQL: `TEST_DATABASE_URL=postgresql+psycopg://torture:torture@localhost:5432/postgres uv run pytest tests/torture_suite/test_concurrency_torture.py -p no:cacheprovider -v`
(outputs in `.cloud_torture/scratch_2c/pg_run_{1,2,3}.txt`, `pg_final_{1,2,3,4}.txt`).

| Run | File version | Result | Time |
|---|---|---|---|
| PG 1 | 84 tests (before 2j/2k/2l) | 18 failed, 66 passed | 128 s |
| PG 2 | 84 tests | 18 failed, 66 passed | 161 s |
| PG 3 | 84 tests | 18 failed, 66 passed | 167 s |
| PG 4 | 97 tests | 21 failed, 76 passed | 157 s |
| PG 5 | 97 tests | 20 failed, 77 passed | 147 s |
| PG 6 | 97 tests | 20 failed, 77 passed | 160 s |
| PG 7 (final file) | 97 tests | **19 failed, 78 passed** | 149 s |
| SQLite (final file) | 97 tests | 2 failed, 1 passed, 94 skipped | 0.6 s |

Failures in every PG run: the 5 deterministic `test_bug_*` (BUG-1..5; BUG-5 test exists from run 4),
`test_plain_and_variant_lines_of_one_product_do_not_deadlock` (all 6 rounds) and most rounds of
`test_last_unit_contention_never_oversells_or_deadlocks`. SQLite failures: BUG-2 and BUG-4
(the only two non-PG-gated bug tests; both reproduce there too).

### Flake rates (nondeterministic failures)

| Test | Failed rounds / rounds run | Runs affected | Cause |
|---|---|---|---|
| `test_plain_and_variant_lines_of_one_product_do_not_deadlock` | 42 / 42 (100 %) | 7 / 7 | BUG-3 (2–28 of 30 checkouts answered 503) |
| `test_last_unit_contention_never_oversells_or_deadlocks` | 52 / 56 (93 %) | 7 / 7 | BUG-3 (1–11 of 40 checkouts answered 503) |
| `test_refund_same_key_twins_refund_once` | 5 / 42 (12 %) | 4 / 7 | BUG-5 (one twin answered 400 "Refund amount (0) exceeds the refundable amount (0)", i.e. processed twice) |
| `test_refund_same_key_different_amounts_execute_once` | 3 / 42 (7 %) | 3 / 7 | BUG-5 (2 refunds recorded for one key: 1.00 and 2.00) |
| everything else | 0 | 0 | — |

## Suspected bugs

### BUG-1 (security, medium) — a refresh token rotated while logout runs survives the logout
* Tests: `test_bug_logout_racing_refresh_leaves_a_live_refresh_token` (deterministic, 7/7 runs);
  the un-widened burst 1b never hit it with fakeredis (0/42 rounds) — the window is the network
  round-trip between two Redis commands, which fakeredis does not have.
* Code: `app/services/redis_async.py:424` `revoke_refresh_family` (SMEMBERS, then DEL in a second
  round trip, no atomicity); `app/core/security.py:537-539` `rotate_refresh_token` (registers the
  new jti, then redeems the old one).
* Interleaving (observed): logout `SMEMBERS refresh_family:F → {J1}` (slow reply) → refresh
  `SET refresh_active:J2; SADD F J2; GET+DEL refresh_active:J1 → ok` → 200 with J2 → logout
  `DEL refresh_active:J1; DEL refresh_family:F` → 200 "Logged out". J2 is still redeemable
  (`POST /refresh` with it → **200**, and it is no longer in any family set, so a later logout
  with it cannot find it either). Only the fake's SMEMBERS reply is delayed (0.2 s) — real
  Redis latency; no app code is altered.
* Not a test bug: the test asserts the documented contract of `/logout` ("revoke its whole token
  family so it (and any refresh token rotated from it) can no longer be redeemed").
* Fix (prototype validated: `.cloud_torture/scratch_2c/fix_probe_tombstone.py`, all refresh tests
  green): write a family tombstone before reading the members, and refuse redemption in a revoked
  family.
  ```diff
   async def revoke_refresh_family(self, family_id: str) -> bool:
       try:
  +        await self._redis.set(f"refresh_revoked:{family_id}", 1,
  +                              ex=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400)
           family_key = f"refresh_family:{family_id}"
  @@ async def consume_refresh_token(self, jti):
               if not data or not deleted:
                   return None
  -            return json.loads(data)
  +            payload = json.loads(data)
  +            if await self._redis.exists(f"refresh_revoked:{payload['family']}"):
  +                return None   # caller revokes the family again, burning the new jti
  +            return payload
  ```
  (Or do SMEMBERS+DEL in one Lua script / MULTI with WATCH on the family key.)

### BUG-2 (correctness, medium) — replay of an in-flight POST /orders key answers 400 "Cart is empty"
* Test: `test_bug_order_replay_racing_its_first_request_answers_cart_empty` (deterministic,
  PG 7/7 and SQLite). Not hit by the 30-twin burst 2a (0/42 rounds: most twins block on the unique
  index and replay correctly).
* Code: `app/api/v1/orders.py:206` (key pre-check) → `:211-215` (`get_cart` → 400).
* Interleaving: retry looks the key up (first request not committed → None) → first request
  commits and `remove_ordered_items_atomic` empties the cart → retry reads an empty cart → 400
  problem+json "Cart is empty". The client, which retried precisely because it lost the first
  answer, is told the checkout failed although the order exists. Documented contract: "Repeating a
  request with a key already used by this user returns the order created the first time".
* Fix: re-check the key before refusing an empty cart (the cart is emptied only after the
  commit, so the order is visible then):
  ```diff
       cart = await redis.get_cart(current_user.id)
       if not cart or not cart.items:
  +        existing = await _order_by_idempotency_key(db, current_user.id, idempotency_key)
  +        if existing:
  +            ensure_same_request(existing.idempotency_fingerprint, fingerprint)
  +            return _created_order(request, response, existing)
           raise HTTPException(status_code=400, detail="Cart is empty")
  ```
  (Same re-check is advisable before the 409 insufficient-stock answer.)

### BUG-3 (availability, high) — checkouts deadlock (40P01) when a variant line and a plain line of the same product meet; surfaced as 503
* Tests: `test_plain_and_variant_lines_of_one_product_do_not_deadlock` (100 % of rounds),
  `test_last_unit_contention_never_oversells_or_deadlocks` (93 %),
  `test_bug_variant_line_checkout_deadlocks_with_plain_line_checkout` (deterministic, 7/7).
* Code: `app/services/orders.py:64-91` `lock_stock_rows` takes `FOR UPDATE` on the products of
  plain lines and the variants of variant lines only; called at `app/api/v1/orders.py:284-285`.
  The `order_items → products` FK check at commit takes `FOR KEY SHARE` on the **parent product**
  of a variant line, which `FOR UPDATE` blocks.
* Interleaving (deterministic test): A (cart {V of P}) locks V → B (cart {P, V}) locks P, waits
  for V → A commits, `INSERT order_items(product_id=P)` waits for KEY SHARE on P (held FOR UPDATE
  by B) → PostgreSQL: `DeadlockDetected … while locking tuple (0,1) in relation
  "product_variants"` (victim B at `app/services/orders.py:86`). `psycopg.errors.DeadlockDetected`
  is an `OperationalError`, so `is_database_unavailable_error` (`app/core/database.py:140`)
  answers **503 "The database is temporarily unavailable"** (not 500, but misleading and it fails
  the docstring's promise that checkouts "cannot deadlock").
* Evidence: bursts of 30: `{201: 7, 503: 23}`, `{201: 12, 503: 18}`, …; bursts of 40 with
  stock 1–3: `{201: 4, 409: 32, 503: 4}`, `{201: 3, 409: 28, 503: 9}`. Stock invariants held in
  every round even then: no oversell, no partial order, no spurious 409, deadlock victims left no
  order (checked before the status check).
* Fix (prototype validated: `.cloud_torture/scratch_2c/fix_probe_nokey.py` — all 14
  contention rounds, 6 cancellation rounds and the deterministic test pass): lock with
  `FOR NO KEY UPDATE`, which is what the stock `UPDATE`s take anyway and does not conflict with
  the FK's `FOR KEY SHARE`.
  ```diff
  -    select(Product.id)...order_by(Product.id).with_for_update()
  +    select(Product.id)...order_by(Product.id).with_for_update(key_share=True)  # FOR NO KEY UPDATE
  @@
  -        .with_for_update()
  +        .with_for_update(key_share=True)
  ```
  Also: classify `TransactionRollbackError` (40P01/40001) as a retryable conflict (retry the
  transaction once, or 409/503 with an accurate detail) instead of "database unavailable".

### BUG-4 (GDPR, medium) — webhook events stored after the erasure purge keep the customer's personal data forever
* Test: `test_bug_webhook_stored_after_erasure_keeps_personal_data` (deterministic, PG 7/7 and
  SQLite).
* Code: `app/tasks.py:341` excludes already anonymised accounts from every later run, and
  `:363` redacts the stored events only once; `app/services/stripe_events.py:438` `store_event`
  (called from `stripe_webhook`, `app/api/v1/orders.py`) and the worker never redact.
* Evidence: after the purge `_pii_left()` is `[]`; a `payment_intent.succeeded` for the erased
  account's order (with `receipt_email` and billing name) delivered afterwards is stored and
  processed verbatim; a second purge returns 0; `webhook_events` row `evt_…` still contains the
  email and name. Late events are normal (Stripe retries up to 72 h; refunds/disputes months
  later), and an event committed while the purge transaction runs is missed the same way.
* Fix: redact on the way in for an erased owner, e.g. in `process_stored_event` after the order is
  found (`if order.user.data_deletion_requested: row.payload = redact_event_payload(row.payload)`),
  plus a periodic `redact_users_events` over anonymised accounts (not re-hashing them) for events
  that match no payment yet.

### BUG-5 (mock provider fidelity, low) — the in-process mock's refund Idempotency-Key is not atomic
* Tests: `test_bug_mock_provider_refund_key_is_not_atomic` (deterministic: 5 concurrent same-key
  refunds → **5** refunds recorded); flaky 2g (12 %) / 2h (7 %).
* Code: `app/services/payment/stripe_provider.py:486` (cache check) … `:532` (cache insert),
  no lock, while `refund_payment` calls it via `run_in_threadpool` (`app/api/v1/orders.py:1591`).
  The route relies on the provider alone for refund idempotency (no local key row).
* Real Stripe answers a concurrent same-key request with an idempotency error (→ 409 via the live
  provider), so production with `PAYMENT_PROVIDER=stripe` is not affected; dev/CI (mock) can
  double-refund and the suite's refund-idempotency guarantees are only as good as this mock.
* Fix: a class-level `threading.Lock` around check-and-record in `create_refund` (and the same in
  `create_payment_intent` / `create_checkout_session`). Optionally store the refund key locally
  (`payment_refunds.idempotency_key`, unique) so the guarantee does not depend on the provider.

## Guarantees that held (0 failures in all runs)

* Refresh reuse: exactly 1 winner of 30 in 42/42 rounds; the family (incl. the winner's new token)
  is always burnt; never two usable refresh tokens from one.
* POST /orders same key: one order, one decrement, 30 × identical 201 (42/42); different payloads
  → one order, exact 201/422 split.
* /pay: one Payment row and one PaymentIntent for same-key twins, different-body twins (422) and
  30 distinct keys (1 × 200, 29 × 409); 409s carry `idempotency-key-in-use`.
* Refunds with distinct keys never over-refund; provider total == DB total; CHECK never tripped.
* Webhook: 30 concurrent redeliveries → one stored row; 10 concurrent workers → applied once
  (SKIP LOCKED claim), one email.
* Cancellation: 30 concurrent customer/admin cancels → one 200, stock restored once.
* Stock: zero oversell, all-or-nothing orders, no spurious 409 — in every round, including the
  rounds that deadlocked.
* GDPR race: erasure always 200; all tokens dead afterwards; purge ∥ webhook workers never
  crashed, every event processed, no PII left (for events stored before the purge).
* No 500 in any burst; every 4xx/5xx was `application/problem+json` (the only 5xx seen were the
  BUG-3 503s).

Observed GDPR burst histogram: `{200: 23, 201: 5, 409: 2}` in every round — the erasure's bcrypt
re-check lets the other 29 requests pass authentication first, so orders/addresses created "during"
the erasure are accepted (they are covered by the later anonymisation; not flagged as a bug).
