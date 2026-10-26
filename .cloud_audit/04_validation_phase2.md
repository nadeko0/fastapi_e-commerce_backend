# 04 — Skeptical validation of Phase-2 findings (2A, 2B, 2C, 2D)

Validator: Skeptical Validator. Target: v4.0.3. Date: 2026-10-26. No file under `app/`, `tests/`, `migrations/` or docs was modified.

## Method

- Each finding was checked by opening the cited file:line. I then looked for mitigations: guards, DB constraints, reconciliation, and trade-offs documented in `docs/LIMITATIONS.md`, `docs/ARCHITECTURE.md`, `docs/API.md`, `docs/OPERATIONS.md`, `SECURITY.md` and code comments. I also checked that a real production caller can reach each path.
- **Independent re-runs:**
  - Existing repros:
    - `.cloud_audit/repro/2c/*` (11 passed).
    - `.cloud_audit/repro/2b/test_2b_repro.py` (3 passed).
  - New validator repro: `/tmp/claude-0/-home-user-fastapi-e-commerce-backend/d6bb2a49-b210-591b-b7be-8a1e71874dff/scratchpad/validator/test_validator_2a.py`. 3 passed, covering 2A-01, 2A-02 and 2A-03. The 2A auditor's scratch file no longer exists.
  - Throwaway **PostgreSQL 16** on :55441, stopped and deleted afterwards:
    - `deadlock_fk.py` (2B-01): checkout vs checkout gives `DeadlockDetected`, checkout vs cancel gives `DeadlockDetected`, and the `FOR NO KEY UPDATE` variant gives both committed.
    - `purge_repro.py` (2D-01): `BCRYPT_ROUNDS=12` purged 100 in **28.2 s**, which passes but only just. `BCRYPT_ROUNDS=13` **failed after 56.6 s** with `IdleInTransactionSessionTimeout`, and 100 accounts were still pending.
- **Correction to 2A-01's claim "reconciliation opens 0":** with the mock as-is it opens 1 discrepancy. That is a mock artefact, because the mock PaymentIntent stays `requires_action`. Once the mock says `succeeded`, as real Stripe would, the claim holds: 0 discrepancies.

Run the validator repro with:
`cd /home/user/fastapi_e-commerce_backend && unset UV_NATIVE_TLS; PYTHONPATH=.:tests:tests/integration uv run pytest -q -p no:cacheprovider -p conftest --rootdir=. <file>`

---

## 1. Verdict table

Reproducible-on: **SQLite** = yes in the normal harness (SQLite + fakeredis). **PG** = needs Postgres (`TEST_DATABASE_URL`). **read** = a code fact, with no behavioural test needed or practical.

### 2A — Payments

| ID | Orig | Final | Verdict | Repro | Justification / most direct pytest |
|---|---|---|---|---|---|
| 2A-01 | High | **High** | CONFIRMED (merged → **PAY-01**) | SQLite | `tasks.py:479-499` selects by status/age only and ignores `processing/requires_action/unknown` attempts. `live_stripe_provider.py:347-367` sends no `expires_at`, and `expire_checkout_session` has no caller. `orders.py:765-767` only flags the late charge. `/refund` then refuses it (`orders.py:1540` not PAID; `:1558` skips flagged rows), and no admin list exposes `requires_manual_review`. The existing `test_payment_chain.py:426` pins the flag but not the remedy. **Repro:** `Shop(client, db_session)`, `pay(token=PAY_3DS)`, `expire_unpaid` (`tasks_db`), `intent_event("payment_intent.succeeded")`, then `refund` gives 409 (validator test). |
| 2A-02 | High | **Medium** | CONFIRMED-DOWNGRADED (**PAY-02**) | SQLite | `reconciliation.py:288-291` skips `pending-…` rows; `retrieve_checkout_session` is never called. The trigger needs a lost or misconfigured webhook endpoint, an infrastructure failure that Stripe itself alerts on (failing-endpoint emails, Dashboard). The exhausted-retries case is visible in `GET /admin/webhook-events`. The charge is visible in Stripe, so this is a detection gap, not silent loss at scale. **Repro:** `Shop.checkout_session`, then `reconcile_payments(db_session, StripePaymentProvider(), now=+2h)` gives `checked==0, skipped==1`. |
| 2A-03 | Medium | **Medium** | CONFIRMED (**PAY-03**) | SQLite | `stripe_events.py:253-257` lets any non-final status overwrite `succeeded`. `_settle_refunds` (`orders.py:1795-1804`) then returns a refunded order to PAID, and `PUT /status processing` gives 200. Next-day reconciliation flags `refund_status` but repairs nothing. It needs out-of-order delivery, which Stripe explicitly does not guarantee against. **Repro:** `pay`, `refund`, then `webhook("refund.created", {...,"status":"pending"})`; `order_state` shows `paid` (validator test). |
| 2A-04 | Medium | **Medium** | CONFIRMED (merged into **PAY-01** as 2nd trigger) | SQLite | `_guard_no_attempt_in_flight` (`orders.py:919-926`) stops blocking after 5 min. Its docstring accepts that the second success "is flagged for manual review, as before", which is intentional but not in LIMITATIONS. The consequence is the same as 2A-01: a flagged charge with no refund path or queue, and the same fix (`expires_at`, expire sibling sessions). **Repro:** two `checkout_session` calls more than 5 min apart (monkeypatch `utc_now`), `session_event("checkout.session.completed")` for both; the second has `requires_manual_review=True`. |
| 2A-05 | Medium | **Low** | CONFIRMED-DOWNGRADED (partly DOCUMENTED-TRADEOFF) | SQLite | The cap and newest-first order are documented in `config.py:458-465` ("At most RECONCILIATION_MAX_PAYMENTS are checked per run (newest first)") and logged as `truncated`. The residual defects are minor: skipped `pending-` rows consume the cap (`reconciliation.py:278-289`), and `SoftTimeLimitExceeded` is not caught. The time-limit overrun is plausible, not shown. **Repro:** monkeypatch `RECONCILIATION_MAX_PAYMENTS=2`, add 2 pending-cs rows and 1 real row; the real one is never checked. |
| 2A-06 | Low | **Low** | CONFIRMED | SQLite (monkeypatch) | `orders.py:1292-1307` discards `_apply_payment_success`'s return value and always answers `PAID`. The window is one Stripe call, during which a customer cancel (no in-flight guard, see PAY-01) or an expiry must land. **Repro:** monkeypatch `StripePaymentProvider.confirm_payment_intent` to cancel the order through `TestingSessionLocal` before returning; assert the response says PAID while the order is cancelled. |
| 2A-07 | Low | **Low** | CONFIRMED | SQLite | `orders.py:695-704` answers 409 forever for a `processing` row orphaned by a crash. No money is at risk, and a new key works after 5 min. Related to the documented "Delayed Payment Methods Returning 409" limitation, but that entry covers a different cause. **Repro:** insert a `Payment(status="processing", idempotency_key=K)` row, then `pay(key=K)` gives 409 with Retry-After. |
| 2A-08 | Low | **Low** | DOCUMENTED-TRADEOFF | read | `REFUND_KEY_HEADER_DESCRIPTION` (`idempotency.py:73-78`) states the 24 h limit, and LIMITATIONS.md ("Provider-Enforced Over-Refunds") names the missing `payment_refunds.idempotency_key` column as the known fix. |
| 2A-09 | Info | — | NITPICK (discard) | read | Unused body `order_id`; unbounded `STRIPE_WEBHOOK_TOLERANCE_SECONDS` (operator config only); random event-id fallback, which needs a valid signature; zero-total orders cannot be paid (a product decision). None is a reachable defect. |

### 2B — Catalog, cart, orders

| ID | Orig | Final | Verdict | Repro | Justification / most direct pytest |
|---|---|---|---|---|---|
| 2B-01 | Medium | **Medium** | CONFIRMED (**DB-01**) | PG | Re-run on PG 16 against the real `lock_stock_rows` (`services/orders.py:81-91`): `FOR UPDATE` on P/V plus the commit-time FK `FOR KEY SHARE` on the variant line's product gives `DeadlockDetected` (503 via `database.py:140`); `with_for_update(key_share=True)` fixes it. The trigger needs a plain line and a variant line of the same product in concurrent carts or orders. Retry is safe. **Repro:** PG-only, like `test_postgres_only.py`, hooking `async_engine.sync_engine` (see 2B-02). |
| 2B-02 | Medium | **Low** | CONFIRMED-DOWNGRADED | PG | `test_postgres_only.py:177` listens on the sync `engine`, while routes run on `async_engine` (`conftest.py:220-223,270`). The barrier therefore never fires, and the comment at :170-172 ("async defs on a synchronous session") is stale. This is a test-quality gap with no production effect. **Repro:** attach a counter on `engine` around `Shop.place_order` on PG; it sees 0 stock UPDATEs. |
| 2B-03 | High | **High** | CONFIRMED — DUPLICATE of 2A-01 (merged → **PAY-01**) | SQLite | Same root cause. It adds the `cancel_own_order` path (`orders.py:557`, `is_unpaid_and_new` only, no in-flight check). |
| 2B-04 | Medium | **Low** | CONFIRMED-DOWNGRADED | SQLite | `admin.py:286-287,831-832` uses top-level `setattr`. `docs/API.md:154-158` documents exactly that: "Properties included in the patch replace stored values". So replacing nested objects matches the documented contract, and the defect is only the "RFC 7396" label. A `null` inside variant `attributes` is stored as a value. Admin-only; no integrity loss beyond the admin's own edit. **Repro:** `.cloud_audit/repro/2b/test_2b_repro.py::test_merge_patch_*`. |
| 2B-05 | Medium | **Low** | DOCUMENTED-TRADEOFF | read | `admin.py:246-250` docstring: "Without it the update is unconditional, as before". Absolute PUT/PATCH semantics are standard REST, and If-Match exists to prevent this lost update. A volatile ETag on a selling product is inherent to a strong ETag over stock. Suggesting a `stock_delta` operation is a feature request. |
| 2B-06 | Medium | **Low** | DOCUMENTED-TRADEOFF | SQLite | `docs/ARCHITECTURE.md:136-142` draws `SENT --> CANCELLED: Admin cancels (restocks inventory)`. `test_orders.py:678-682` and `test_payment_chain.py:404-415` pin both behaviours, and a cancelled-paid order stays refundable through `/refund` ("support's job"). This is a product decision, not a bug. |
| 2B-07 | Low | **Low** | CONFIRMED | SQLite (Numeric not enforced) / PG | Nothing bounds `total_amount` (`orders.py:366-368`) against `Numeric(10,2)`, giving `DataError` and a 500 on PG. It needs a total above 99,999,999.99, so it is an edge case. **Repro:** PG; product price 1000.00, stock 200000, cart qty 100001, `place_order` gives 500. |
| 2B-08 | Low | **Low** | CONFIRMED | PG | Count-then-delete (`admin.py:339-349`) racing a checkout that locked the product gives an FK IntegrityError and a 500. Rare: the product must never have been ordered and be bought during the delete. No corruption. |
| 2B-09 | Low | **Low** | CONFIRMED | SQLite | `schemas/common.py:496-499` plus `redis_codec.py:288-293` only log the dropped lines. It needs more than 100 combined lines. **Repro:** `fake_redis`; a guest cart with 100 lines and an account cart with 1 distinct line; login; `GET /cart` has 100 lines. |
| 2B-10 | Low | — | FALSE-POSITIVE (money impact) | read | The window is real (`orders.py:1571-1617`), but real Stripe refuses to refund a charged-back charge (error code `charge_disputed`), so "money returned twice" cannot happen with the live provider. Only the mock lacks this check. At most an Info hardening item (re-check under the lock). |
| 2B-11 | Low | **Low** | CONFIRMED | SQLite (monkeypatch) | `tasks.py:492-526` has no per-order try. The next beat 5 min later picks up the rest, and committed orders stay committed. Minor. |
| 2B-12 | Low | — | NITPICK (discard) | read | Unlocked read of the parent product's `is_active`/`price` (`orders.py:290-301`). The worst case is an order placed for a product that was active when read. No integrity impact. |
| 2B-13 | Info | — | Info (discard) | — | Design notes, not defects. Accepted live price, cache-aside TTL, live product name on old orders, and the CHECK migration lock (see 2D-09). |

### 2C — Auth, rate limiting, GDPR

| ID | Orig | Final | Verdict | Repro | Justification / most direct pytest |
|---|---|---|---|---|---|
| 2C-01 | Medium | **Medium** | CONFIRMED (**GDPR-01**, absorbs the `last_error` part of 2D-06) | SQLite | The purges exclude anonymised users (`tasks.py:332-342, 426-433`). `redact_users_events` runs only at erasure (`tasks.py:363`). `orders.py:1903-1909` stores full `event.data`, and no retention job ever deletes `webhook_events`. **Repro:** `.cloud_audit/repro/2c/test_audit_2c.py::test_2c01_*`. |
| 2C-02 | Medium | **Medium** | CONFIRMED (**AUTH-01**, merged with 2C-03) | SQLite | `users.py:332-388` mints tokens with no re-check that the hash is still the one verified. Reset (`users.py:1055-1072`) only moves the watermark and revokes no refresh families. The attacker needs the old password and must keep a login in flight across the reset, which is achievable with sustained login traffic from about 12 IPs. "Sign out everywhere" is a manual escape. **Repro:** `repro/2c/test_audit_2c_races.py::test_2c02_*` (monkeypatch `users.verify_password_async`). |
| 2C-03 | Low | **Low** | CONFIRMED — merged into **AUTH-01** | SQLite | Same root cause (reset relies on the watermark only), the rotation path (`security.py:527-539`). A sliding refresh with no absolute lifetime is a common design and not a defect on its own. **Repro:** `test_2c03_*`. |
| 2C-04 | Medium | **Medium** | CONFIRMED (**AUTH-02**) | SQLite | `/legal/data-request` is missing from `_ROUTE_LIMITS` (`rate_limit.py:75-87`), so it gets the default 300/min. `legal.py:338-343` never calls `check_login_attempt`. The precondition is a stolen access or refresh token; the gain is password disclosure plus irreversible erasure. **Repro:** `test_2c04_*`. Tests disable the limiter, so the 300/min ceiling is shown by reading. |
| 2C-05 | Medium | **Medium** | CONFIRMED (**REG-01**) | SQLite | `messages.py:207` calls `html.escape(full_name)`, and `full_name` is optional (`schemas/user.py:92`). The call site `users.py:212-217` passes it unchanged, so the background task dies and no verification mail is sent. Checkout requires verification (`orders.py:186`). The only workaround is `/verify-email/resend`, which a client must already know about. **Repro:** `test_2c05_*`; better, `client.post("/users/register", json={...no full_name})` with `Outbox`/monkeypatched `send_welcome_email` raising. |
| 2C-06 | Medium | **Medium** | CONFIRMED (**DATA-01**) | SQLite | `users.py:777-780` edits the address in place, and orders reference it by FK with no snapshot. Delete is soft, unlike update. Historical and delivered orders change retroactively. **Repro:** `test_2c06_*`. |
| 2C-07 | Medium | **Medium** | CONFIRMED (**GDPR-03**) | SQLite | `UserUpdate.marketing_consent` (`schemas/user.py:159`) is applied by raw `setattr` (`users.py:541-542`), with no `consent_history` entry and no date. `gdpr.py:40-49` shows the intended path. **Repro:** `test_2c07_*`. |
| 2C-08 | Low | **Info** | CONFIRMED-DOWNGRADED (discard) | SQLite | This is Starlette's stock preflight rejection. Browsers consume preflights, and no API client reads that body. **Repro:** `test_2c08_*`. |
| 2C-09 | Low | **Low** | DOCUMENTED-TRADEOFF | SQLite | LIMITATIONS.md "Concurrent Refresh-Token Reuse Window (Campaign Finding)" describes this exact behaviour and the grace-period fix. **Repro:** `test_2c09_*`. |
| 2C-10 | Medium | **Medium** | CONFIRMED (**GDPR-04**) | SQLite | `gdpr.py:119-147` and `tasks.py:315-397` erase with no check for paid-unshipped orders or open disputes, and redact addresses after 1 day. Reachable by any customer with an open paid order. **Repro:** `Shop.pay` (PAY_OK), set `data_deletion_requested` with an old `data_deletion_date`, run `purge_expired_deletion_requests(db_session)`, then `GET /orders/{id}` shows street `REDACTED` while status is `confirmed`. |
| 2C-11 | Medium | **Medium** | CONFIRMED (**GDPR-05**) | SQLite | `gdpr.py:100-109` builds no export ("In production, the export payload would be assembled here"), yet `messages.py:330` says "here is your personal data export". **Additional evidence the auditor missed:** `SECURITY.md:115` claims "Bounded, eager-loaded queries export user account records, addresses, order histories, and consent audits in JSON", which is false. **Repro:** monkeypatch `send_gdpr_export_email` and assert the `GDPRExport` carries no user data. |
| 2C-12 | Low | **Low** | CONFIRMED | SQLite | `gdpr.py:143-145` sends "personal data has been deleted" (`messages.py:373`) at request time. |
| 2C-13 | Low | **Low** | DOCUMENTED-TRADEOFF | read | LIMITATIONS.md "Data Retention for Inactive Live Accounts" and OPERATIONS.md:146 ("Active customer accounts are never touched"). The unused `INACTIVE_ACCOUNT_DELETE_DAYS` and the misleading `validate_retention_period` remain as Info. |
| 2C-14 | Low | **Low** | CONFIRMED (shares a fix with **AUTH-02**) | read | `gdpr.py:98,109` makes two synchronous SMTP sends per export at 300/min per user. Plausible impact only. |
| 2C-15 | Low | **Low** | DOCUMENTED-TRADEOFF | read | The trade-off is documented at `rate_limit.py:368-370`. Counting IPv6 per /64 is the documented choice. |
| 2C-16 | Info | — | Info (discard) | read | No CORS headers on shed 503s and 500s; a missing `retry_after` member. Cosmetic. |
| 2C-17 | Info | — | NITPICK (discard) | read | Hygiene preferences (`iss`/`aud`/`kid`, rehash on cost change, deployment checks). `type` separation is enforced. |
| 2C-18 | Info | — | Info (discard) | read | Latent hazards only: no code deletes users, and the sync consume path is dead. |
| 2C-19 | Info | — | NITPICK (discard) | read | A client can only move itself into the shared `unknown` bucket. |

### 2D — Infrastructure

| ID | Orig | Final | Verdict | Repro | Justification / most direct pytest |
|---|---|---|---|---|---|
| 2D-01 | High | **Medium** | CONFIRMED-DOWNGRADED (**GDPR-02**, absorbs 2D-C18) | PG | Re-run on PG 16. Rounds 12: 28.2 s (pass, 1.8 s of margin). Rounds 13 (allowed): **fails, 100 still pending**. The auditor missed that a batch only holds `min(due, 100)` accounts, so it fails only when about 95 or more erasures are due at once at cost 12 (about 50 at cost 13, or fewer under CPU contention). Once tripped, every daily run fails on the same first batch (keyset from id 0), silently. That makes it real, but volume-dependent: Medium. **Repro:** PG with `BCRYPT_ROUNDS=13` and 100 due users, then `purge_expired_deletion_requests` raises `InternalError` (`.cloud_audit/repro/2d/purge_repro.py`). |
| 2D-02 | Medium | **Low** | CONFIRMED-DOWNGRADED (**OBS-01**, absorbs the Celery half of 2D-C19) | SQLite | Verified: `import app.tasks` leaves `app.main` unloaded, with no root handlers and `app` level 0. Only `main.py:26` calls `setup_logging`. Worker logs are plain text with no request/trace ids, and `LOG_FILE`/`LOG_LEVEL` are ignored. An observability gap; no data impact. **Repro:** `import app.tasks; assert not logging.getLogger().handlers`. |
| 2D-03 | Low-Med | **Low** | CONFIRMED | read | `legal.py:346-348` → `gdpr.py:126-141`: SMTP runs inside the transaction opened by `_load_user`. It needs more than 30 s of SMTP to fail. |
| 2D-04 | Low-Med | **Low** | CONFIRMED | SQLite | Verified: `POSTGRES_PASSWORD='p@ssw0rd-…'` produces host `ssw0rd-…@db` (`config.py:208`). It fails loudly on the first request or `/readyz`, and setting `DATABASE_URI` directly avoids it. **Repro:** `Settings()` with that password, then `make_url(...).host`. |
| 2D-05 | Low | **Low** | CONFIRMED | read | No `connect_timeout` (`database.py:56-64`). `/health` calls `database_is_up()` with no limit (`main.py:188`). Bounded by the in-flight limiter. |
| 2D-06 | Low | **Low** | CONFIRMED (log part); `last_error` part merged into **GDPR-01** at Info weight | read | There is no `hide_parameters`. `last_error` stores `str(exc)` (`stripe_events.py:594`), but event processing binds payment and refund ids and amounts, not PII, so that part is mostly theoretical. |
| 2D-07 | Low/Info | **Low** | CONFIRMED (dead code) / DOCUMENTED-TRADEOFF (durability) | read | Verified: only `process_stripe_event` and `check_payment_reconciliation` are ever `apply_async`-ed. LIMITATIONS.md "Dedicated Email Queue and Retries" admits emails have no retry broker, though it wrongly implies Celery tasks are used. |
| 2D-08 | Info | — | NITPICK (discard) | read | redis-py 8.x defaults (5 s), locked in `uv.lock`. |
| 2D-09 | Info | — | Info (discard) | read | Pre-release migration that has already run; advice for future migrations. |

### 2D-C — Stale or misleading comments (all confirmed by grep or read; none has runtime effect)

| ID | Final | Verdict | Evidence |
|---|---|---|---|
| 2D-C01 | Info | CONFIRMED | `config.py:406` names `check_payment_settings_for_production`, which does not exist. |
| 2D-C02 | **Low** | CONFIRMED (operator-facing) | `overload.py:10-22` says "two sync connections … / 2" and points to README "Load testing", which does not exist. Auth commits and shares the one async session (`security.py:296-370`). The advice halves capacity. |
| 2D-C03 | **Low** | CONFIRMED (operator-facing) | `.env.example:35-36` says "sync pool"; requests use the async pool. |
| 2D-C04 | Info | CONFIRMED | `request_context.py:14-16` says "no tracing SDK"; `telemetry.py:171-181` instruments Celery and requests. |
| 2D-C05 | Info | CONFIRMED | `main.py:86` claims BodySizeLimit is innermost; `HeadMiddleware` is added first (`main.py:84`). |
| 2D-C06 | Info | CONFIRMED | `main.py:111` "Outside everything but the security headers"; RequestContext and OTel are also outside. |
| 2D-C07 | Info | CONFIRMED | `redis_async.py:55-57,169` attributes the writes to the sync `RedisService`; the writes go through `AsyncRedisService` (`security.py:584,607`). |
| 2D-C08 | **Low** | CONFIRMED (operator-facing) | `email/__init__.py:10` "logging (the default outside production)"; `config.py:376` defaults to `smtp` everywhere. |
| 2D-C09 | Info | CONFIRMED | `gdpr.py:133-136` "performs the actual hard delete"; the task anonymises instead. |
| 2D-C10 | — | NITPICK (discard) | The live callers of `send_order_confirmation_email` all pass an `OrderResponse`. Only the dead Celery task (2D-07) passes an ORM Order, and the SimpleNamespace goes to a different function. |
| 2D-C11 | Info | CONFIRMED | `stripe_provider.py:4-11` "no real Stripe account … never will"; a live provider exists and `tests/test_payment.py` is really `tests/integration/test_payment.py`. |
| 2D-C12 | Info | CONFIRMED | `exceptions.py:5-8` "We deliberately do not depend on the real stripe package"; it is a dependency. |
| 2D-C13 | Info | CONFIRMED | `product.py:28-35` cites `.astext`; `products.py:279` uses `.as_string()`. |
| 2D-C14 | Info | CONFIRMED | `admin.py:388` "identical note in orders.update_order_status"; no such note exists. |
| 2D-C15 | — | NITPICK (discard) | `tasks.py:301` says `Address.user` where it means `User.addresses`. The point it makes (cascade) is still correct. |
| 2D-C16 | Info | CONFIRMED | Four comments cite `scripts/loadtest/...`; there is no `scripts/` directory. |
| 2D-C17 | — | NITPICK (discard) | `cart.py:102` omits the `_async` suffix. |
| 2D-C18 | — | merged into **GDPR-02** | `tasks.py:286-289` "~25 s, well inside task_soft_time_limit": the binding limit is the 30 s idle timeout. |
| 2D-C19 | Info | CONFIRMED (Celery half merged into **OBS-01**; `database.py:27-37` half is NITPICK) | The comment describes a sync-dependency leak that no longer exists, but it is accurate as history. |

---

## 2. Confirmed findings: canonical list (ranked)

### High
1. **PAY-01: Late or second charges on expired, cancelled or superseded orders cannot be refunded or seen in the app** (2A-01 + 2B-03 + 2A-04).
   - The expiry (`tasks.py:479-499`) and customer cancel (`orders.py:557`) ignore attempts in flight, and Checkout Sessions get no `expires_at` (Stripe default 24 h vs a 30 min order life).
   - `expire_checkout_session` is never called, and the 5-min stale window allows parallel payable attempts.
   - Late charges are only flagged. `/refund` refuses them, there is no admin queue, and reconciliation stays silent; the only signal is a WARNING log.
   - Reproducible on SQLite.

### Medium
2. **PAY-02 (2A-02):** Reconciliation skips Checkout attempts whose completion webhook never arrived.
3. **PAY-03 (2A-03):** An out-of-order `refund.*` event regresses `succeeded` to `pending`, so a refunded order goes back to PAID and can be shipped.
4. **GDPR-02 (2D-01 + C18):** The erasure and retention purges hash bcrypt inside the transaction. With about 95 or more due accounts (about 50 at cost 13), the purge fails permanently and silently (PG).
5. **DB-01 (2B-01):** `FOR UPDATE` stock locks vs FK `KEY SHARE` deadlock when the same product is ordered both plain and with variants. The result is a 503 that looks like a DB outage (PG).
6. **GDPR-01 (2C-01 + 2D-06 `last_error`):** Webhook payloads stored after erasure keep PII forever.
7. **GDPR-04 (2C-10):** Erasure ignores paid-unshipped orders and open disputes, and addresses are redacted after 1 day.
8. **GDPR-05 (2C-11):** The Art. 15/20 export is not implemented, but the email and `SECURITY.md:115` claim it is.
9. **GDPR-03 (2C-07):** `PATCH /users/me` changes marketing consent with no history entry and no date.
10. **AUTH-01 (2C-02 + 2C-03):** A password reset does not evict a login or refresh rotation that is in flight; the watermark is the only defence and no families are revoked.
11. **AUTH-02 (2C-04, fix shared with 2C-14):** The deletion re-auth is a password oracle at 300/min with no lockout.
12. **REG-01 (2C-05):** Registering without `full_name` crashes the verification email, which blocks checkout.
13. **DATA-01 (2C-06):** Editing an address rewrites the shipping address of historical and paid orders.

### Low
14. **2A-05:** Reconciliation coverage residue (skipped rows count against the cap; no soft-limit handling).
15. **2A-06:** `/pay` answers PAID when the success was not applied.
16. **2A-07:** An orphaned `processing` attempt makes its key answer 409 forever.
17. **2B-02:** The PG lock-order tests hook the wrong engine.
18. **2B-04:** Nested merge-patch replaces objects; only the "RFC 7396" label is wrong, and a variant-attribute `null` is stored.
19. **2B-07:** Total overflow gives a 500.
20. **2B-08:** Hard delete racing checkout gives a 500.
21. **2B-09:** Cart-merge lines are dropped silently.
22. **2B-11:** The expiry sweep aborts on the first error.
23. **2C-12:** A premature deletion confirmation.
24. **2C-14:** Export email amplification.
25. **OBS-01 (2D-02 + C19):** Celery logging is not configured.
26. **2D-03:** SMTP inside the deletion transaction.
27. **2D-04:** `DATABASE_URI` is not URL-encoded.
28. **2D-05:** No DB connect timeout, and `/health` is unbounded.
29. **2D-06:** No `hide_parameters`, so SQL parameters reach the logs.
30. **2D-07:** Dead order-email Celery tasks.
31. **DOC-01:** Operator-facing stale comments 2D-C02, C03 and C08 (pool-sizing and email-default guidance). The other confirmed C-entries are Info.

### Documented trade-offs (still listed, known)
2A-08 (refund key, 24 h), 2B-05 (absolute stock writes; If-Match optional), 2B-06 (SENT→CANCELLED restock; paid cancel needs a manual refund), 2C-09 (refresh reuse window), 2C-13 (retention only for deactivated accounts), 2C-15 (IPv6 /64 lockout DoS). The durability half of 2D-07 is also documented.

---

## 3. Discarded

| ID | Reason |
|---|---|
| 2A-09 | Info or nitpicks: operator-config bounds, a field that is never compared, a fallback that cannot be reached with a valid signature, a product decision on zero totals. |
| 2B-10 | False positive for money impact: live Stripe refuses refunds of charged-back charges (`charge_disputed`). Only the mock lacks the check. |
| 2B-12 | A benign race with no integrity outcome. |
| 2B-13 | Design notes, not defects. |
| 2C-08 | Starlette's stock preflight response, which browsers consume; Info. |
| 2C-16, 2C-17, 2C-18, 2C-19 | Cosmetic, hygiene preferences, latent issues in dead code, or self-inflicted only. |
| 2D-08, 2D-09 | Defaults pinned by the lockfile; a migration that has already run (advice only). |
| 2D-C10, 2D-C15, 2D-C17, the `database.py` half of 2D-C19 | Comment nitpicks: accurate for live code, a trivial name slip, or correct as history. |

Duplicates merged: 2B-03 and 2A-04 into PAY-01; 2C-03 into AUTH-01; the `last_error` half of 2D-06 into GDPR-01; 2D-C18 into GDPR-02; the Celery half of 2D-C19 into OBS-01.
