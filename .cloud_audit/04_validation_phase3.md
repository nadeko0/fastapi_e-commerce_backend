# 04: Skeptical validation of phase 3 (3A docs drift, 3B tests audit, 3C spot-check)

Validator: phase-3 skeptic. I modified no repository file; this is the only file I wrote. Scratch work is in
`/tmp/claude-0/-home-user-fastapi-e-commerce-backend/d6bb2a49-b210-591b-b7be-8a1e71874dff/scratchpad/validator3/`:
- `oa.py`: OpenAPI dump.
- `probe/test_v3_overflow.py`: Numeric(10,2) probe on SQLite and PG.
- `probe/test_v3_cart_blip.py`: BUG-NEW-1 repro.

Runs:
- I re-ran the PostgreSQL mutations on a throwaway PG 16 cluster (`/var/tmp/pgv3`, port 55434). The cluster is stopped and deleted.
- I used 3B's `mut_nolock.py` plugin read-only.
- I did not run the full suite and did not touch `tests/audit_verification/`.

Severity scale:
- **Docs:** High means it misleads an operator into a broken or insecure deployment, or a client into a wrong integration. Medium means it documents wrong API behaviour. Low means a stale number or a cosmetic issue.
- **Tests:** High means the test hid a confirmed production bug. Medium means a regression guard on a money or stock invariant that cannot fail. Low means a weak assertion, a metric issue or a hazard that has not been observed.

---

## 1. 3A verdicts (docs drift)

| ID | Orig | Final | Verdict | Evidence (doc line opened; code line opened; check run) |
|---|---|---|---|---|
| 3A-01 | High | **High** | CONFIRMED | SETUP.md:87-89 is quoted exactly. `grep POSTGRES_SERVER\|REDIS_HOST docker-compose.yml` returns nothing. The `environment:` blocks (31-37 and similar) override only POSTGRES_USER/PASSWORD. `.env.example:57`/`:75` are `localhost`, and README.md:54 says only "set ENVIRONMENT, passwords and secrets". Following the README, every container points at its own loopback. |
| 3A-02 | High | **High** | CONFIRMED | OPERATIONS.md:124/127 is quoted exactly. The dump line lacks `-T`, so the TTY mangles the binary `-Fc` stream. `"$POSTGRES_USER"` expands in the host shell; contrast SETUP.md:137, which uses `exec -T db sh -c '...'`. T5 ran a different command (T5-migrations-backup.md:38). The result is a silently corrupt or failed backup. |
| 3A-03 | Medium | **Medium** | CONFIRMED | API.md:105 says `DELETE /api/v1/cart` returns 204. The OpenAPI dump shows `DELETE /api/v1/cart ['200',…]` and `DELETE /cart/items/{id} ['200',…]`. cart.py:602-606 has `response_model=CartResponse`. A client that expects no body would break. |
| 3A-04 | Medium | **Medium** | CONFIRMED (minor miscount) | ARCHITECTURE.md:199-200 says 409. idempotency.py:55 is `HTTP_422_UNPROCESSABLE_CONTENT`. API.md:94/109/124 correctly say 422, so the docs contradict each other. Retry-After appears only at orders.py:703 and at :731 (only when the attempt is processing). There are **4** other IN_USE raises (1091, 1219, 1477, 1606), not "five". |
| 3A-05 | Medium | **Low** | DOWNGRADED | ARCHITECTURE.md:66/71/91 are quoted exactly. OpenAPI: OrderCreate={shipping_address_id}, PaymentCreate={order_id,payment_method,amount,currency,payment_method_token}, RefundCreate={amount}; `additionalProperties: False`. This is real drift, but it is an informal mermaid sequence diagram. The API reference and OpenAPI are correct, so I rated it Low. |
| 3A-06 | Medium | **Medium** | CONFIRMED | RESILIENCE.md:80 says type `service-unavailable`. security.py:289-293 builds `Problem(503, detail, retry_after=30)` with no type, so errors.py:145 gives `about:blank`. No `service-unavailable` ProblemType exists. A client that branches on that URI never matches. |
| 3A-07 | Medium | **High** | CONFIRMED (upgraded) | README.md:26, SECURITY.md:115 ("Bounded, eager-loaded queries export … in JSON format") and API.md:16 are quoted exactly. gdpr.py:100-109 says "In production, the export payload would be assembled here" and only ids/dates are emailed. Phase 2 confirmed this as GDPR-05. An operator relying on SECURITY.md deploys believing Art. 15/20 is met. It is not, which is a compliance-level misstatement, so I upgraded it. |
| 3A-08 | Medium | **Medium** | CONFIRMED | API.md:98 says "unverified, deactivated, or suspended". security.py:390-395 raises `account-inactive` only for `not is_active`. An unverified user gets a plain 403 with `about:blank` (orders.py:186-189). `is_email_verified` is checked only at orders.py:186. There is no "suspended" state. |
| 3A-09 | Medium | **Medium** | CONFIRMED | API.md:108 says deleting a default address is 409. users.py:785-831: the route is 204 only, and the newest other active address is promoted. There is no 409 path. |
| 3A-10 | Medium | **Low** | DOWNGRADED | API.md:128 and :199 are quoted exactly. orders.py:598 returns `f"{kind}-order-{order.id}:{client_key}"`, and idempotency.py:18-21 and :67-71 say payment keys never expire. The key format is internal (clients never see it). The expiry difference is in the safe direction, a longer replay window. Low. |
| 3A-11 | Medium | **Low** | DOWNGRADED | API.md:154-158 is quoted exactly. admin.py:286-287 and :831-832 use a top-level `setattr`. Phase 2 (2B-04) already judged this label-only: the doc itself says "Properties included in the patch replace stored values". Only the "RFC 7396" label and the nested-null case are wrong. |
| 3A-12 | Low | **Low** | CONFIRMED (line drift) | ARCHITECTURE.md:120 and RESILIENCE.md:54 say "every minute". The code is at tasks.py:**682-685** (not 686-689), `timedelta(minutes=5)`. |
| 3A-13 | Low | **Low** | CONFIRMED | OPERATIONS.md:116 says `statement_timeout=30s`. database.py:60-61 has `statement_timeout=60000`. |
| 3A-14 | Low | **Low** | CONFIRMED | SECURITY.md:29 says 24 hours. config.py:371 is `=48`, used at security.py:706 and in the email text (messages.py:219). |
| 3A-15 | Low | **Low** | CONFIRMED | SECURITY.md:103 and PERFORMANCE.md:57 give the `/2` formula. database.py:202-208 shares one `DbSession` per request (scope="function", cached), and get_current_user takes `db: DbSession` (security.py:313-314). The stale source is overload.py:20-22. `.env.example:35-37` ships 15. The stale formula is conservative, not unsafe. |
| 3A-16 | Low | **Low** | CONFIRMED | OPERATIONS.md:82, LIMITATIONS.md:37 and the ARCHITECTURE diagram are as quoted. `grep '\.delay(\|apply_async(' app` finds only stripe_events.py:474. Emails go out through `background_tasks.add_task` (orders.py:396, 510, 570, 781). |
| 3A-17 | Low | **Low** | CONFIRMED | ARCHITECTURE.md:227/230 are quoted exactly. ci.yml has one workflow and 9 job keys (16 with the matrix). No `STRIPE` variable and no chaos job. |
| 3A-18 | Low | — | NITPICK | API.md:9 "7 domain routers" is defensible: 7 routers are included, with `probes.router` at main.py:206, matching API.md:13-19's 7 tags. ARCHITECTURE.md:154 "7 domain modules" in app/api/v1 is off by one, which is immaterial. |
| 3A-19 | Low | **Low** | CONFIRMED | TESTING.md:11 and final-test-campaign.md:10 say "~4800". `--collect-only`: tests/unit 2,650, tests/integration 2,694. |
| 3A-20 | Low | — | NITPICK | A 98.8 % vs 98.9 % split between documents (README:11/88 vs TESTING:139). fixes-4.0.x.md:227 shows 98.8 → 98.9 across versions. 0.1 pp is immaterial, and it cannot be re-derived without a full run. |
| 3A-21 | Low | **Low** | CONFIRMED | README:87 says "400", TESTING:9/137 and final-test-campaign:81 say "406". The 4 files named at TESTING.md:95 collect **49** tests. 406 cannot be reproduced from the repo. |
| 3A-22 | Low | — | NITPICK | SETUP.md:53 says "halved". config.py:193-199 has `max(5, v//2)`, so the default overflow of 5 stays 5. This is a floor detail. |
| 3A-23 | Low | **Low** | CONFIRMED (partly) | `.env.example:5-6` already says "anything but development or test", so the "production-only" framing applies only to lines 158 and 173. The real omission is that the header list leaves out the required `STRIPE_SECRET_KEY` and the non-secret refusals (config.py:512-549). It fails closed with a clear message, so Low. |
| 3A-24 | Low | **Low** | CONFIRMED | OPERATIONS.md:91 says "sales velocity and view statistics". tasks.py:579-620 caches the top 20 by quantity and revenue only; there are no view statistics. |
| 3A-25 | Low | **Low** | CONFIRMED | SECURITY.md:105 is quoted exactly. rate_limit.py:320-325: RATE_LIMIT_AUTH routes fail open on RedisError. Only the login counter (rate_limit.py:398-406 / 414-420) and the token checks fail closed. Brute force on login is still blocked; the auth limit on register and reset is not. |
| 3A-26 | Low | **Low** | CONFIRMED (line drift) | RESILIENCE.md:67 says "requeues immediately". `visibility_timeout: 3600` is at tasks.py:**128** (not 119-120). docker-compose.yml:69-73 states the 1 h redelivery itself. |
| 3A-27 | Low | **Low** | CONFIRMED | ARCHITECTURE.md:106-108 shows `Store --> Ack --> Enqueue`. orders.py:1914-1915 enqueues and then returns the ack. Note: API.md:205-206 has the same wrong order (Ack before Enqueue), which 3A missed. |
| 3A-28 | Low | **Low** | CONFIRMED | LIMITATIONS.md:55 says `skip`/`limit`. common.py:155-159 uses `page`/`size`. |
| 3A-29 | Low | **Low** | CONFIRMED | load-testing-3x.md:100/103/104 cite three tests; `grep -rlw` over tests/ finds none of them. |
| 3A-30 | Low | **Low** | CONFIRMED | SECURITY.md:114 says `current_password`. The field in legal.py:44 is `password`, and RequestBody forbids extras, so `current_password` gives a 422. |
| 3A-31 | Low | **Low** | CONFIRMED | ARCHITECTURE.md:197 says "user ID, URL, and normalized JSON payload". orders.py:203-205 hashes `operation="create_order", shipping_address_id` only (idempotency.py:36-48). The user scope comes from the key lookup. |
| 3A-32 | Low | — | NITPICK | API.md:196 "(PENDING)" and :207 "PAID" are loose shorthand for payment_status pending/paid. Order is new→confirmed (orders.py:776-777). It is an informal diagram, so the difference is immaterial. |
| 3A-33 | Info | Info | CONFIRMED (spot-checked) | ci.yml:14-15 says "random seeds", but conftest.py:90-98 sets `derandomize=True`. `scripts/` does not exist (`ls`). The reference is at rate_limit.py:**43** (not 41), gdpr.py:96, and also messages.py:71 and orders.py:1179. `EMAIL_PROVIDER` is absent from .env.example, with default `smtp` (config.py:376). |

## 2. 3B verdicts (tests audit)

| ID | Orig | Final | Verdict | Evidence |
|---|---|---|---|---|
| 3B-01 | High | **Medium** | CONFIRMED (downgraded: production lock is correct; guard is weak) | test_postgres_only.py:172 does `event.listen(engine, ...)`, the **sync** engine. Routes run on `async_engine` (conftest.py:220-224 and 270). **Re-run on PG 16:** with the code intact, the 3 deadlock tests pass in 0.99 s total. If the hook fired, the barrier would serialise them for about 3 s each, so the hook never fires. With `MUT=nolock` run alone, the opposite-order checkout test FAILS (1/1) and the cancel tests pass. I did not reproduce the "passes under `-n 4`" leg. I accept it as plausible: CI uses `-n auto --dist worksteal` (ci.yml:234). |
| 3B-02 | Medium | **Medium** | CONFIRMED (re-run) | `MUT=nofu -k "pay_waits or two_concurrent"` on PG 16: `test_pay_waits…` **PASSED** with every `Select.with_for_update()` stripped. Mechanism: /pay commits a `payments` INSERT (orders.py:1124-1137) whose FK takes KEY SHARE on the holder's FOR-UPDATE-locked order row. Caveat: the mutation does not strip `db.refresh(..., with_for_update=True)` (orders.py:806), but that runs only after the provider call, so the conclusion stands. |
| 3B-03 | Medium | **Medium** | CONFIRMED (partly re-run) | The same run, alone: `test_two_concurrent_payments…` FAILED under nofu, so it does detect the mutation when idle. I did not re-run the load-dependence (`-n 4`) part. The test has no server-side barrier (lines 117-143), which makes it plausible. |
| 3B-04 | Medium | **Medium** | CONFIRMED (re-run subset) | All 16 `with_for_update` sites match the listed lines. `MUT=nofu` over test_refund_lifecycle, test_refund_bookkeeping, test_disputes, test_etag and test_admin_webhook_events on PG, `-n 4`: **67 passed, 0 failed**. So stripe_events.py:209/341, admin.py:257/810/1050 have no failing guard. The SQL-shape tests in test_payment_concurrency.py:110-181 cover only pay, checkout-session, webhook, cancel and expiry. |
| 3B-05 | Medium | **Medium** | CONFIRMED (re-run) | test_orders.py:614/637/661. The PUT spy on SQLite showed `confirmed→409 "Order is not paid"`, `processing→409`, `sent→409`, then `cancelled→200`. The `_transition` results are never asserted, so all three tests cancel a `new` order. |
| 3B-06 | High | **High** | CONFIRMED (re-run) | conftest.py:447 stubs `app.api.v1.users.send_welcome_email` for every `client`. A direct call `send_welcome_email('a@example.com', None, 'tok')` gives `AttributeError: 'NoneType' object has no attribute 'replace'` (messages.py:207 `html.escape(full_name)`). The stub hid confirmed bug REG-01/2C-05. |
| 3B-07 | High | **Medium** | CONFIRMED (downgraded) | conftest.py:219 has `_pg_options = "-c statement_timeout=60000"` only, vs. production database.py:60-61 (which adds `idle_in_transaction_session_timeout=30000`). BCRYPT_ROUNDS=4 (conftest.py:41), and test_tasks_gdpr.py:208/248/262 stub `get_password_hash`. So the suite could never see 2D-01. Downgraded because the bug it hides was itself rated **Medium** (volume-dependent) in phase 2. 3B's arithmetic ("25-40 s > 30 s") overstates it: phase 2 measured 28.2 s at cost 12 (pass) and failure at cost 13. |
| 3B-08 | Medium | **Medium** | CONFIRMED (re-run) | Probe (2 × 60,000,000.00): on SQLite, **201** with total `120000000.00` stored. On PG 16, **500** `NumericValueOutOfRange` at the orders `total_amount` UPDATE. The strategy at test_order_invariants.py:50-53 is capped at 9,999.99. |
| 3B-09 | Low-Medium | **Low** | DOWNGRADED | The `_DB_SENSITIVE_FILES` glob (conftest.py:138-157) indeed omits the listed files. But the PG-15 CI job runs the **full** suite, so every one of them does run on PostgreSQL in CI. The gap is PG 16/18 only. |
| 3B-10 | Medium | **Low** | DOWNGRADED (partly subjective) | The alphabet at test_order_invariants.py:200-212 is as described: one product at 5.00, STOCK=6, success-card pay only. It is a scope observation about what a property test *could* also cover, not a defect that makes it pass wrongly. The "pins 2B-06" point is moot, since it cannot reach `sent`. |
| 3B-11 | Low | **Low** | CONFIRMED | test_stripe_event_pipeline.py:307-327 is a SQL-shape check only. The two-workers test (:329+) wraps the Python `_claim_event`. There is no two-connection PG test. |
| 3B-12 | Low | **Low** | CONFIRMED (some parts nitpick) | test_live_stripe_provider.py:586-602 is a pure spy: it never sends a stale timestamp, a real tautology. test_stripe_rate_limit_mapping.py:118-123 has a comment with no matching assert. test_config_matrix.py:131-133 uses an alphanumeric password, which is why 2D-04 was missed. The `_decimal_to_cents` on both sides (test_order_invariants.py:192) is a NITPICK, because the converter is pinned separately. |
| 3B-13 | Low-Medium | **Low** | CONFIRMED | `grep` finds no app caller of `expire_checkout_session`/`retrieve_checkout_session`. The ci.yml:88 include list gates `app/services/payment/*.py` (mock included). This is metric inflation only. |
| 3B-14 | Info | Info | CONFIRMED | 2 `pragma: no cover` (orders.py:278, 1440), both re-raises. |
| 3B-15 | Low | **Low** | CONFIRMED (hazard, unobserved) | The class-level mock state is real (stripe_provider.py:215-220). None of the three named files resets it. No flake was observed. |
| 3B-16 | — | Info | AGGREGATE | This is a cross-reference table of phase-2 bugs, not a separate finding. Spot-checked: test_reconciliation.py:468-479 pins `skipped == 1` (2A-02), and test_payment_chain.py:426-445 pins the expiry-during-3DS outcome (2A-01). Both are accurate. |

---

## 3. Canonical confirmed lists

### Docs (DOC-*), ordered by final severity
| ID | Sev | Summary |
|---|---|---|
| 3A-01 | High | Compose quick start: nothing sets `POSTGRES_SERVER=db`/`REDIS_HOST=redis`, and .env.example uses localhost. SETUP.md:87-89 is wrong. |
| 3A-02 | High | The OPERATIONS.md:124 backup command lacks `-T` and expands `$POSTGRES_USER` on the host, giving a corrupt or failed dump. |
| 3A-07 | High | SECURITY.md:115, README:26 and API.md:16 claim a GDPR Art. 15/20 export that is not implemented. |
| 3A-03 | Medium | `DELETE /cart` returns 200 with a body, not 204 (API.md:105). |
| 3A-04 + DOC-NEW-1 | Medium | ARCHITECTURE.md:199-200: a reused key is 422, not 409, and Retry-After is conditional. |
| 3A-06 | Medium | RESILIENCE.md:80: the 503 has type `about:blank`, not `service-unavailable`. |
| 3A-08 | Medium | API.md:98: `account-inactive` is not used for unverified accounts. |
| 3A-09 | Medium | API.md:108: deleting the default address is 204, never 409. |
| 3A-05 | Low | ARCHITECTURE sequence-diagram body fields are wrong. |
| 3A-10 | Low | Provider key format and expiry. |
| 3A-11 | Low | "RFC 7396" label (nested objects replaced). |
| 3A-12 | Low | Sweeper runs every 5 min, not every minute. |
| 3A-13 | Low | statement_timeout is 60 s. |
| 3A-14 | Low | Verification token lives 48 h. |
| 3A-15 | Low | Stale `/2` in-flight formula. |
| 3A-16 | Low | Order-email Celery tasks are never enqueued. |
| 3A-17 | Low | CI has no live-Stripe or chaos jobs. |
| 3A-19 | Low | "~4800 unit tests". |
| 3A-21 | Low | "400/406" live Stripe tests (the repo has 49). |
| 3A-23 | Low | .env.example omits STRIPE_SECRET_KEY and the non-secret checks, and says "production" for staging too. |
| 3A-24 | Low | update_product_stats description. |
| 3A-25 | Low | Auth rate limit fails open. |
| 3A-26 | Low | Redelivery after a whole-worker kill takes 1 h. |
| 3A-27 | Low | Webhook flow diagram order; also API.md:205-206. |
| 3A-28 | Low | `skip`/`limit` should be `page`/`size`. |
| 3A-29 | Low | Three test names that do not exist. |
| 3A-30 | Low | The field is `password`, not `current_password`. |
| 3A-31 | Low | Fingerprint contents. |
| 3A-33 | Info | Comment and config drift. |

Discarded as NITPICK: 3A-18, 3A-20, 3A-22, 3A-32.

### Tests (TEST-*)
| ID | Sev | Summary |
|---|---|---|
| 3B-06 | High | The global welcome-email stub hid REG-01/2C-05. |
| 3B-01 | Medium | The deadlock-test hook is on the sync engine and never fires; detection is timing luck. |
| 3B-02 | Medium | pay-waits passes with every app `FOR UPDATE` removed (FK KEY SHARE). |
| 3B-03 | Medium | The double-charge test has no server-side barrier and is load-dependent. |
| 3B-04 | Medium | 6 lock sites (refund/dispute events, `_record_failure`, admin If-Match writes, replay) have no failing guard. |
| 3B-05 | Medium | 3 cancel-from-X tests never leave `new`. |
| 3B-07 | Medium | Test engines lack the idle-in-transaction timeout and bcrypt is stubbed, which hid 2D-01. |
| 3B-08 | Medium | Numeric(10,2) overflow: 201 on SQLite vs 500 on PG; no test, and strategies are capped. |
| 3B-09 | Low | db_sensitive glob gaps (PG 16/18 only). |
| 3B-10 | Low | Narrow stateful alphabet. |
| 3B-11 | Low | SKIP LOCKED is never tested with two PG workers. |
| 3B-12 | Low | Tautological tolerance spy, missing `unknown` assert, alphanumeric URI password. |
| 3B-13 | Low | Dead provider methods and the mock count toward the 100 % gate. |
| 3B-15 | Low | Class-level mock state is not reset in 3 files. |
| 3B-14, 3B-16 | Info | |

---

## 4. 3C reference spot-check (30 references opened)

All 30 point at the cited content. Two have minor offsets; none is wrong.

| 3C reference | Result |
|---|---|
| orders.py:203-209 (fingerprint + pre-check) | exact |
| orders.py:257-278 (flush/IntegrityError/replay; `raise # pragma` at 278) | exact |
| models/order.py:68-73 (unique partial index) | exact |
| models/payment.py:76-78 (unique order_id+key) | exact |
| idempotency.py:36-58, `:55` = 422 | exact (line 55 is `status.HTTP_422_UNPROCESSABLE_CONTENT`) |
| idempotency.py:19-22 (payment keys never expire) | exact |
| orders.py:589-599 (provider key namespacing) | exact |
| orders.py:1124-1149 (processing row before Stripe) | exact |
| orders.py:684-705 (409 for processing) | ok: the range starts at `_replay_payment`; the 409 is at 695-705 |
| tasks.py:700-743 (order key expiry) | exact (`clear_expired_idempotency_keys`) |
| LIMITATIONS.md:48-50 (refund key) | ok: the refund-key remedy is at :50 |
| orders.py:1864-1880 (verify signature) | exact |
| orders.py:1903-1915 (store + enqueue) | exact |
| stripe_events.py:438 / 462 / 531-541 / 613 (store, soft publish, SKIP LOCKED, sweeper) | exact |
| config.py:442-451 (retry/sweep settings, `le=3000` at 444) | exact |
| tasks.py:479-499, 492-499 (expiry candidates, lock + re-check) | exact |
| live_stripe_provider.py:347 (session params), :384 (`retrieve_checkout_session`) | exact |
| orders.py:1540-1545 (refund refuses non-PAID) | exact |
| reconciliation.py:277-285 cap, 289-291 skip, 293-294 commit | exact |
| config.py:463-466 (reconciliation window/cap) | exact |
| tasks.py:139 (soft limit 240), tasks.py:676-679 (03:15 schedule) | exact |
| tasks.py:152-162 (Celery engine, plain sessionmaker) | exact |
| stripe_events.py:561 (`expire_on_commit = False`) | exact |
| database.py:84, 109 (autoflush=False), :140 (OperationalError→503) | exact |
| docker-compose.yml:170-174 (max_connections) | exact |
| security.py:205-216 (float iat), :344-354 (fail closed), :487 (rotation) | exact |
| users.py:332-336 (login read), :386-388 (mint), :437-440 (503), :1045 (reset watermark) | exact |
| redis_async.py:405-410 (MULTI/EXEC consume) | exact |
| rate_limit.py:414-420 (login counter fails closed); cart.py:308-316 (cart 503) | exact |
| RESILIENCE.md:15 (50/50 events, 0 duplicates); ARCHITECTURE.md:166-170 | exact |

---

## 5. Verdicts on 3C's new claims

### DOC-NEW-1 (Medium): CONFIRMED, duplicate of 3A-04 and 3A-31
- **The 409 claim:** ARCHITECTURE.md:199 says a reused key with a mutated body returns "HTTP 409 `idempotency-key-reused`". The code raises 422 (idempotency.py:50-57, line 55 `HTTP_422_UNPROCESSABLE_CONTENT`). API.md:94, :109 and :124 say 422, so the two documents contradict each other. A client built from ARCHITECTURE.md would branch on the wrong status.
- **The fingerprint claim:** checkout's fingerprint is `request_fingerprint(operation="create_order", shipping_address_id=...)` (orders.py:203-205), so it covers only the shipping address. That contradicts ARCHITECTURE.md:197 ("user ID, URL, and normalized JSON payload").
- **Not a defect:** the same key with a *different cart* replays the first order instead of answering 422. That is intended (docstring at orders.py:178-181: "the cart is server state").
- **Fix:** in ARCHITECTURE.md §6, say 422, describe the fingerprint as operation plus validated fields, and add "Retry-After only while the attempt is processing".

### BUG-NEW-1 (Low): CONFIRMED by running it
- **The code path:**
  - `AsyncRedisService.get_cart` (redis_async.py:213-222) catches `RedisError` and returns `None`.
  - `create_order` (orders.py:211-216) maps `not cart` to **400 "Cart is empty"**.
- **Repro** (`validator3/probe/test_v3_cart_blip.py`, SQLite + fakeredis): a RedisError is raised inside `get_cart`'s try block for one request.
  - `POST /orders` → **400** `{"detail":"Cart is empty"}`, with no Retry-After.
  - After the blip, the same order → **201**. The cart was intact all along.
- **Also found:** `GET /api/v1/cart` during the blip → **200 with an empty cart** (cart.py:402 `get_cart(owner) or empty_cart(owner)`).
  - Guests have no token check, so they see this for a whole Redis outage, not just a blip.
  - Authenticated users would get 503 from the token check first.
- **Impact:** a wrong status and a misleading message ("your cart is empty") on a transient fault. There is no data loss: mutations are WATCH/MULTI and return 503 through `_cart_or_503`, cart.py:308-318. There is no money impact.
- **Severity:** Low.
- **Fix:** make `get_cart` raise a `CartUnavailable` (or return a sentinel) and map it to 503 with Retry-After 5 in both checkout and `GET /cart`.
