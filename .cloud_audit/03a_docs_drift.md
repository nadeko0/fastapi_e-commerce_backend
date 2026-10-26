# 3A: Documentation-to-code truth verification (v4.0.3)

Scope: README.md, SETUP.md, SECURITY.md, CHANGELOG.md (4.0.x and present-tense claims), .env.example, docs/*.md, docs/reports/*.md (only claims that describe the current code or cite current numbers), and the docker-compose.yml/Dockerfile claims the docs make.

Ground truth used:
- `app.openapi()` dump: 54 paths, 67 operations, per-operation status codes and header parameters.
- `Settings.model_fields` defaults compared by script with every `KEY=value` in `.env.example`. All defaults match; the only non-Settings keys are compose or OTEL variables.
- `pytest --collect-only` per marker: 5,344 tests in total, 21 `timing_sensitive`, 2,319 `db_sensitive`, 28 `real_redis`, 5,323 `not timing_sensitive`, 2,650 under `tests/unit`, 2,694 under `tests/integration`.
- A run of the 8 files that touch the live Stripe provider: `137 passed, 49 skipped`. Only 49 tests need a real Stripe key.
- `.github/workflows/ci.yml`: 16 jobs (lint, test, 7 x test-timezones, 3 x test-postgres, test-redis, audit, secrets, docker-build).
- `celery.conf.beat_schedule` (app/tasks.py:652-702) and `docker-compose.yml`.
- A script that checked every file path and every `test_*` name cited in the docs.

Nothing outside this file was modified.

---

## Findings

### 3A-01 (High): Docker Compose does not set `POSTGRES_SERVER=db` / `REDIS_HOST=redis`, so the documented quick start cannot reach the database or Redis
- **Doc:** SETUP.md:87-89
  > Inside `docker-compose.yml`, services connect using internal container hostnames:
  > - `POSTGRES_SERVER=db`
  > - `REDIS_HOST=redis`

  Related: README.md:54-56 (`cp .env.example .env  # set ENVIRONMENT, passwords and secrets` followed by `docker compose up -d --build` and the migrate step). SETUP.md:39/43 say "(or `db` inside Docker Compose)".
- **Code:** docker-compose.yml:31-37, 75-81, 117-123 and 152-153. `api`, `celery-worker`, `celery-beat` and `migrate` read `env_file: .env`, and their `environment:` overrides only `POSTGRES_USER`/`POSTGRES_PASSWORD`. Nothing in the file sets `POSTGRES_SERVER` or `REDIS_HOST` (`grep -n "POSTGRES_SERVER\|REDIS_HOST" docker-compose.yml` returns nothing). `.env.example:57` has `POSTGRES_SERVER=localhost` and `.env.example:75` has `REDIS_HOST=localhost`. Following the README literally, every container connects to its own loopback: the `migrate` service fails, `/health` answers 503, and the api never becomes healthy.
- **Corrected text (SETUP.md:87-90):**
  > `docker-compose.yml` does not set the service host names. In `.env`, set `POSTGRES_SERVER=db` and `REDIS_HOST=redis` (the Compose service names) before `docker compose up`. The `localhost` values in `.env.example` are for running the app directly on the host. `REDIS_PASSWORD` is required (`:?` expansion stops Compose from starting Redis without one).

  Also change README.md:54 to `# set ENVIRONMENT, POSTGRES_SERVER=db, REDIS_HOST=redis, passwords and secrets; see SETUP.md`. The alternative is a code fix: add `POSTGRES_SERVER: db` and `REDIS_HOST: redis` to each service's `environment:`.

### 3A-02 (High): The documented backup command produces a broken or failed dump
- **Doc:** docs/OPERATIONS.md:123-127
  > `docker compose exec db pg_dump -U "$POSTGRES_USER" -Fc ecommerce > backup_$(date +%Y%m%d_%H%M%S).dump`
  > `docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d ecommerce --exit-on-error --no-owner < backup.dump`

  OPERATIONS.md:120 adds "Backup and restore fidelity was proven during Stage T5".
- **Code/evidence:**
  1. The dump line has no `-T`. When run from a terminal, `docker compose exec` allocates a pseudo-TTY, so the binary `-Fc` stream passes through the TTY (newline translation) on its way into the file. The restore line in the same block does use `-T`, and SETUP.md:137 uses `exec -T` for the same reason.
  2. `"$POSTGRES_USER"` is expanded by the operator's host shell, which does not load `.env`. It is empty unless the operator exported it. SETUP.md:135-137 deliberately wraps the command in `sh -c '...'` so the container's own variables are used.
  3. The database name is hardcoded as `ecommerce`, not `$POSTGRES_DB`.
  4. T5 never ran this command. It ran `pg_dump -Fc -f <work dir>/ecommerce-final.dump ecommerce` inside the container (docs/reports/T5-migrations-backup.md:38), so the "proven" claim does not cover the command shown.
- **Corrected text:**
  ```bash
  # Create a custom-format backup (-T: no TTY, the output is binary; variables expand inside the container)
  docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > backup_$(date +%Y%m%d_%H%M%S).dump
  # Restore into an empty database
  docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exit-on-error --no-owner' < backup.dump
  ```

### 3A-03 (Medium): `DELETE /api/v1/cart` answers 200 with the (empty) cart, not 204
- **Doc:** docs/API.md:105
  > **204 No Content:** Returned on successful deletion (`DELETE /api/v1/cart`, `DELETE /api/v1/users/addresses/{id}`). Contains no body.
- **Code:** app/api/v1/cart.py:602-606 (`@router.delete("", response_model=CartResponse)`, default status 200). The OpenAPI dump shows `DELETE /api/v1/cart 200` with a `CartResponse` body. `DELETE /cart/items/{product_id}` is also 200 with a body. The 204 routes are `DELETE /users/addresses/{id}` (users.py:787) and `DELETE /admin/products/{id}` (admin.py:314).
- **Corrected text:**
  > **204 No Content:** Returned on successful deletion of an address (`DELETE /api/v1/users/addresses/{id}`) and a product (`DELETE /api/v1/admin/products/{id}`). There is no body. Deleting the same resource again returns 404. `DELETE /api/v1/cart` and `DELETE /api/v1/cart/items/{product_id}` return 200 with the updated cart.

### 3A-04 (Medium): `idempotency-key-reused` is 422, not 409. Retry-After is not always present on `idempotency-key-in-use`
- **Doc:** docs/ARCHITECTURE.md:199-200
  > ...prevents reuse of identical keys with mutated request bodies (returning HTTP 409 `idempotency-key-reused`).
  > Concurrent requests with the same key while the first is in flight receive HTTP 409 with `Retry-After: 5` (`idempotency-key-in-use`).
- **Code:** app/core/idempotency.py:50-57 returns `Problem(HTTP_422_UNPROCESSABLE_CONTENT, type=IDEMPOTENCY_KEY_REUSED)`. API.md:94 and CHANGELOG 3.3.0 both correctly say 422. On `idempotency-key-in-use`, `retry_after=5` is set only at orders.py:703, and at orders.py:731 only when the attempt is `processing`. The other five raises (orders.py:1090, 1218, 1476, 1605) carry no Retry-After.
- **Corrected text:**
  > ...returning HTTP 422 `idempotency-key-reused`. ... A request whose key belongs to an attempt still in flight receives HTTP 409 `idempotency-key-in-use`, with `Retry-After: 5` when that attempt is still being processed.

### 3A-05 (Medium): The request bodies in the architecture sequence diagram would be rejected (`extra="forbid"`)
- **Doc:** docs/ARCHITECTURE.md:66, 71 and 91
  > `POST /api/v1/orders (Idempotency-Key, address_id)`
  > `POST /api/v1/orders/{id}/pay (Idempotency-Key, payment_method_id)`
  > `POST /api/v1/orders/{id}/refund (Idempotency-Key, amount, reason)`
- **Code:** OpenAPI schemas:
  - `OrderCreate` = {`shipping_address_id`}.
  - `PaymentCreate` = {`order_id`*, `payment_method`*, `amount`*, `currency`, `payment_method_token`}.
  - `RefundCreate` = {`amount`}.

  `RequestBody` forbids extra fields (app/schemas/common.py:63), so a client that copies `reason` or `payment_method_id` gets 422 `extra_forbidden`.
- **Corrected text:** `POST /api/v1/orders (Idempotency-Key, shipping_address_id)`; `POST /api/v1/orders/{id}/pay (Idempotency-Key, order_id, payment_method, amount, payment_method_token)`; `POST /api/v1/orders/{id}/refund (Idempotency-Key, optional amount)`.

### 3A-06 (Medium): The 503 for an unverifiable token has no problem type `service-unavailable`
- **Doc:** docs/RESILIENCE.md:80
  > ...the API returns HTTP 503 with problem details type `service-unavailable` and `Retry-After: 30`
- **Code:** app/core/security.py:289-293 `token_check_unavailable()` builds `Problem(503, detail=..., retry_after=30)` with no `type`, so the response has `"type": "about:blank"` (errors.py:145). `ProblemType` (errors.py:76-90) has no `service-unavailable` member. A client branching on that URI never matches.
- **Corrected text:**
  > ...the API returns HTTP 503 problem details (`type: "about:blank"`, `title: "Service Unavailable"`) with `Retry-After: 30` and the `retry_after` member...

### 3A-07 (Medium): GDPR data export (Art. 15/20) is documented as implemented. The code only sends an email with no data
- **Doc:**
  - README.md:26 > **Privacy:** GDPR export and erasure, consent records, retention jobs.
  - SECURITY.md:115 > **Article 15 and 20 Portability:** Bounded, eager-loaded queries export user account records, addresses, order histories, and consent audits in JSON format.
  - docs/API.md:16 > ... data export, and account erasure.
- **Code:** app/services/gdpr.py:100-109: "In production, the export payload would be assembled here...". Only `request_id/request_date/expires_at` are emailed (`send_gdpr_export_email`, messages.py:325-343, which says "here is your personal data export"). No query collects the user's records. This matches the prior finding 2C-11.
- **Corrected text (SECURITY.md:115):**
  > **Article 15 and 20 (not implemented):** `POST /api/v1/legal/data-request` with `request_type: "export"` records the request and emails an acknowledgement. No export of the user's data is assembled or delivered yet. Exports must be produced manually.

  Change README.md:26 to "GDPR erasure, consent records, retention jobs (data export: request intake only)".

### 3A-08 (Medium): `account-inactive` is not used for unverified accounts
- **Doc:** docs/API.md:98
  > `account-inactive` | 403 | Authenticated credentials are valid, but user account is unverified, deactivated, or suspended.
- **Code:** app/core/security.py:390-395 raises `account-inactive` only when `is_active` is false. There is no "suspended" state. An unverified account placing an order gets a plain `403` with `about:blank` and the detail "Email verification required before placing orders" (app/api/v1/orders.py:186-189).
- **Corrected text:**
  > `account-inactive` | 403 | The token is valid but the account is deactivated (`is_active` false). (An unverified email blocks `POST /orders` with a plain 403, type `about:blank`.)

### 3A-09 (Medium): Deleting the default address is not a 409
- **Doc:** docs/API.md:108
  > `409 Conflict`: ... (`insufficient-stock`, `idempotency-key-in-use`, deleting a default address, or cancelling an already-shipped order).
- **Code:** app/api/v1/users.py:820-831. Deleting the default address succeeds (204), and the newest other active address becomes the default. No 409 is raised.
- **Corrected text:**
  > `409 Conflict`: Request conflicts with current resource state (`insufficient-stock`, `idempotency-key-in-use`, an invalid order status transition, cancelling an order that is paid or no longer new, replaying a webhook event that has not failed).

### 3A-10 (Medium): The payment Idempotency-Key format and expiry are documented wrongly
- **Doc:** docs/API.md:128
  > Payment and refund idempotency keys are namespaced (e.g. `pay:{order_id}:{client_key}`) and passed directly to Stripe, where provider-side deduplication guarantees 24-hour protection.

  docs/API.md:199 also uses `pay:42:k2`.
- **Code:** app/api/v1/orders.py:589-599 builds the key as `f"{kind}-order-{order.id}:{client_key}"` (for example `pay-order-42:k2`, `checkout-order-42:k2`). The `/pay` and `/checkout-session` keys are also stored locally in `payments` and **never expire** (app/core/idempotency.py:16-22 and `PAYMENT_KEY_HEADER_DESCRIPTION`: "does not expire"). Only the refund key relies on Stripe's 24 h (`REFUND_KEY_HEADER_DESCRIPTION`).
- **Corrected text:**
  > Payment-attempt keys (`/pay`, `/checkout-session`) are stored with the attempt and never expire. A failed attempt needs a new key. They are forwarded to Stripe namespaced as `pay-order-{order_id}:{key}` / `checkout-order-{order_id}:{key}`. Refund keys are not stored locally: Stripe honours them for at least 24 hours, after which the same value is a new refund.

### 3A-11 (Medium): PATCH is documented as RFC 7396, but nested objects are replaced, not merged
- **Doc:** docs/API.md:154-158
  > ...implement RFC 7396 semantics: ... Properties included in the patch replace stored values. Explicit `null` clears nullable fields ... Properties omitted from the payload remain completely unmodified.
- **Code:** app/api/v1/admin.py:286-287 and 831-832 run `setattr(obj, field, value)` per top-level key. For `characteristics` and variant `attributes`, the whole object is replaced: omitted nested members are deleted, and a nested `null` gives 422 (characteristics) or is stored as a value (attributes). Reproduced in 2B-04.
- **Corrected text (append):**
  > Merging is one level deep: a top-level member replaces the stored value whole. A JSON object value (`characteristics`, `attributes`) is not merged recursively as RFC 7396 §2 describes. Send the complete object.

### 3A-12 (Low): The webhook sweeper runs every 5 minutes, not every minute
- **Doc:** docs/ARCHITECTURE.md:120 `sweep_stripe_events (Every minute)` and docs/RESILIENCE.md:54 `Background Sweeper (Runs every minute)`.
- **Code:** app/tasks.py:686-689 `'sweep-stripe-events': {'schedule': timedelta(minutes=5)}`. OPERATIONS.md:95 and .env.example:209 correctly say 5 minutes. A stranded event is republished after `STRIPE_EVENT_SWEEP_AFTER_MINUTES` (20) plus up to 5 minutes.
- **Corrected text:** `sweep_stripe_events (every 5 minutes)` and `Background Sweeper (every 5 minutes)`.

### 3A-13 (Low): `statement_timeout` is 60 s, not 30 s
- **Doc:** docs/OPERATIONS.md:116 > Connections enforce `idle_in_transaction_session_timeout=30s` and `statement_timeout=30s`...
- **Code:** app/core/database.py:59-61 `-c idle_in_transaction_session_timeout=30000 -c statement_timeout=60000`.
- **Corrected text:** `idle_in_transaction_session_timeout=30s` and `statement_timeout=60s` (PostgreSQL only).

### 3A-14 (Low): The email-verification token lives 48 h, not 24 h
- **Doc:** SECURITY.md:29 `| Email Verification | 24 hours | ...`
- **Code:** app/core/config.py:371 `EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS: int = 48`, used in app/core/security.py:706. The email text (messages.py:219) also says 48.
- **Corrected text:** `| Email Verification | 48 hours (EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS) | ...`

### 3A-15 (Low): The "safe in-flight ceiling = (pool + overflow) / 2" rule is stale and contradicts `.env.example`
- **Doc:** SECURITY.md:103 and docs/PERFORMANCE.md:57
  > Safe ceiling formula: `(DB_POOL_SIZE + DB_MAX_OVERFLOW) / 2` because an authenticated request can hold two database connections simultaneously (auth session and route session).
- **Code:** `get_current_user` takes the same per-request `DbSession` as the route (app/core/security.py:313-314, app/core/database.py:208, `Depends(get_async_db, scope="function")` is cached per request), so one session is used. `.env.example:35-37` says "Keep it at or below the sync pool (DB_POOL_SIZE + DB_MAX_OVERFLOW)" and ships `15` (= 10 + 5), the value used for every benchmark. The source of the formula is the stale docstring at app/core/overload.py:20-22.
- **Corrected text:**
  > Keep `MAX_IN_FLIGHT_REQUESTS` at or below `DB_POOL_SIZE + DB_MAX_OVERFLOW` (15 with the defaults, the value used in the benchmarks). A request uses one session for its auth dependency and its route.

### 3A-16 (Low): The order-confirmation Celery task is documented as the email path, but it is never enqueued
- **Doc:**
  - docs/ARCHITECTURE.md:87-88 > `Worker->>Redis: Enqueue send_order_confirmation(order_id)` / `Worker->>SMTP: Send confirmation email`
  - docs/OPERATIONS.md:82 > **Deduplication:** Tasks retried after transient database errors (`OperationalError`) verify Redis markers (`email_sent:*`, 7-day TTL) ... to prevent sending duplicate order confirmation emails.
  - docs/LIMITATIONS.md:37 > Transactional emails currently rely on Celery tasks and BackgroundTasks...
- **Code:** `grep -rn "\.delay(\|apply_async(" app` finds only `process_stripe_event` (stripe_events.py:474). `send_order_confirmation`/`send_order_status_update` (tasks.py:194-284) are never called. The emails go out from FastAPI `BackgroundTasks` (orders.py:397, 511, 782) and the after-commit hook in stripe_events.py. Same as prior finding 2D-07.
- **Corrected text (OPERATIONS.md:82):**
  > Order emails are sent in-process after commit (FastAPI BackgroundTasks, or an after-commit hook in the webhook worker). They are not retried, and a crash after commit loses the email. The `send_order_*` Celery tasks and their `email_sent:*` markers exist but are not used.

  Change ARCHITECTURE.md:87 to `Worker->>SMTP: Send confirmation email (after commit)`.

### 3A-17 (Low): CI coverage is overstated (no Stripe test-mode or chaos jobs; one workflow, not 16)
- **Doc:** docs/ARCHITECTURE.md:227, 230
  > CI matrix workflows validate integration and chaos suites against real container services: PostgreSQL 15, 16, 18, real Redis, and live Stripe test mode across 7 timezones.
  > (~5 minutes across 16 parallel workflows)
- **Code:** .github/workflows/ci.yml has one workflow with 16 jobs. No job sets `STRIPE_SECRET_KEY` (so the 49 live tests are skipped). No chaos job exists. The timezone matrix runs SQLite plus fakeredis with the mock provider (ci.yml:96-157).
- **Corrected text:**
  > CI (one workflow, 16 jobs) runs the suite on SQLite, the database-sensitive suite on PostgreSQL 15/16/18, the Redis parity tests on a real Redis, and the payment/order tests under 7 time zones. Live Stripe test-mode and chaos runs were manual campaign stages (T4, T8), not CI.

### 3A-18 (Low): `app/api/v1` has 6 router modules, not 7
- **Doc:** docs/ARCHITECTURE.md:154 "across 7 domain modules". docs/API.md:9 "across 7 domain routers".
- **Code:** app/api/v1 holds `admin, cart, legal, orders, products, users`. `/health` is declared in app/main.py, and `/livez`/`/readyz` come from app/core/probes.py. The 54 paths and 67 operations in API.md:9 are correct.
- **Corrected text:** "6 domain routers (admin, cart, catalog/products, legal, orders, users) plus 3 probe routes".

### 3A-19 (Low): The "~4800 unit tests" figure is wrong
- **Doc:** docs/TESTING.md:11 `Unit Tests (... ~4800 tests)`. docs/reports/final-test-campaign.md:10 "(~4800 tests, 98.9% branch coverage)".
- **Code:** collection gives 2,650 tests under `tests/unit`, 2,694 under `tests/integration`, and 5,344 in total.
- **Corrected text:** `Unit Tests (~2,650 tests)` and `Integration Tests (~2,700 tests)`.

### 3A-20 (Low): The overall coverage figure differs between documents
- **Doc:** README.md:11 and 88, and final-test-campaign.md:83 say **98.8 %**. docs/TESTING.md:139, docs/reports/fixes-4.0.x.md:226 (v4.0.3 column) and final-test-campaign.md:10 say **98.9 %**.
- **Code:** the gate is `fail_under = 85` (pyproject.toml:104) plus 100 % on the payment include list (ci.yml:85-88). Both match the docs. The measured value cannot be re-derived without a full suite run, which is out of scope here.
- **Fix:** use one measured value everywhere.

### 3A-21 (Low): The live Stripe test count ("400" / "406") does not match the repository
- **Doc:** README.md:87 "400 against Stripe test mode". docs/TESTING.md:9 and 137 "406". final-test-campaign.md:81 "406 passed".
- **Code:** only 49 tests skip without a real `STRIPE_SECRET_KEY` (8-file run: `137 passed, 49 skipped`). The four files TESTING.md:95 names hold 49 tests together. 406 cannot be reproduced from the repository.
- **Fix:** state what the 406 counted (for example "the payment suite run with `PAYMENT_PROVIDER=stripe`"), or say "49 live Stripe tests".

### 3A-22 (Low): Development mode does not halve the overflow pool
- **Doc:** SETUP.md:53 > Connection pool sizes (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`) are halved.
- **Code:** app/core/config.py:193-199 `max(5, v // 2)`. Checked: with the defaults, `DB_POOL_SIZE` 10 becomes 5 and `DB_MAX_OVERFLOW` stays 5.
- **Corrected text:** "...are halved, with a floor of 5 (defaults: pool 5, overflow 5)".

### 3A-23 (Low): `.env.example` describes the deployment checks as production-only and incomplete
- **Doc:** .env.example:7-9 "SMTP_PASSWORD only must not be a placeholder." .env.example:158 "ENVIRONMENT=production refuses to start with mock". .env.example:173 "Must be https:// in production."
- **Code:** app/core/config.py:503-549. The checks apply to every ENVIRONMENT except development/test, so **staging too**. `STRIPE_SECRET_KEY` must also be set and must not be a placeholder (line 526). The same check refuses `LOG_LEVEL=DEBUG` and `BCRYPT_ROUNDS<12`, and requires `PAYMENT_PROVIDER=stripe` and an https `FRONTEND_URL`.
- **Corrected text (lines 7-9):**
  > ...STRIPE_WEBHOOK_SECRET 32, POSTGRES_PASSWORD and REDIS_PASSWORD 16 (REDIS_PASSWORD must be set). SMTP_PASSWORD and STRIPE_SECRET_KEY must be set and not placeholders. Also refused: PAYMENT_PROVIDER other than stripe, LOG_LEVEL=DEBUG, BCRYPT_ROUNDS below 12, a FRONTEND_URL that is not https://.

  At lines 158 and 173, replace "production" with "staging and production".

### 3A-24 (Low): `update_product_stats` is described wrongly
- **Doc:** docs/OPERATIONS.md:91 > Recalculates product sales velocity and view statistics.
- **Code:** app/tasks.py:579-620 caches the top 20 products by quantity sold (paid, non-cancelled orders, last 30 days) with their revenue as Redis `product_stats` (TTL 1 h). No view statistics exist.
- **Corrected text:** "Caches the 20 best-selling products of the last 30 days (quantity, revenue) in Redis `product_stats`."

### 3A-25 (Low): The auth rate limit fails open, not closed
- **Doc:** SECURITY.md:105 > ...authentication and failed-login limits fail closed (503)...
- **Code:** app/core/rate_limit.py:318-326. Every route limit, `RATE_LIMIT_AUTH` included, fails open on `RedisError`. Only the failed-login counter (rate_limit.py:395-405, Retry-After 30) and token revocation checks (security.py:289-293) fail closed. PERFORMANCE.md:65 states this correctly.
- **Corrected text:** "...per-route limits (auth routes included) fail open; the failed-login counter and token revocation checks fail closed (503, Retry-After: 30)."

### 3A-26 (Low): Redelivery after a worker kill is not immediate
- **Doc:** docs/RESILIENCE.md:67 > If a worker dies mid-task, the broker requeues the task immediately.
- **Code:** app/tasks.py:119-120 `visibility_timeout: 3600`. docker-compose.yml:69-73 says so directly: "a task killed mid-run is redelivered only after the broker's visibility_timeout (1 h, acks_late)". `reject_on_worker_lost` requeues at once only when a pool child dies and the main worker process survives.
- **Corrected text:** "If a pool process dies, the task is requeued at once. If the whole worker is killed, the Redis broker redelivers the unacknowledged task after `visibility_timeout` (1 hour)."

### 3A-27 (Low): Webhook flow diagram ordering
- **Doc:** docs/ARCHITECTURE.md:106-108 `Store --> Ack --> Enqueue`.
- **Code:** app/api/v1/orders.py:1917-1919 publishes (`enqueue_event`) before it returns the 200. The ARCHITECTURE.md sequence diagram (lines 79-82) and RESILIENCE.md show the right order.
- **Corrected text:** `Store --> Enqueue --> Ack`, noting that a broker failure is logged and the event is still acknowledged.

### 3A-28 (Low): Pagination parameter names
- **Doc:** docs/LIMITATIONS.md:55 > Offset pagination (`skip` / `limit`)...
- **Code:** app/schemas/common.py:155-159 `page` (≥1) / `size` (1-100). API.md:141-142 is correct.
- **Corrected text:** "Offset pagination (`page` / `size`)".

### 3A-29 (Low): A historical report cites regression tests that do not exist
- **Doc:** docs/reports/load-testing-3x.md:100, 103, 104 cite `test_overload_middleware_rejects_beyond_capacity`, `test_cart_pricing_matches_catalog_at_checkout` and `test_celery_task_leaves_no_idle_transactions`.
- **Code:** `grep -rw <name> tests` finds none of the three. Every other `test_*` name and `.py` path cited in the docs exists.
- **Fix:** replace them with the real test names, or drop the column.

### 3A-30 (Low): The deletion request field name
- **Doc:** SECURITY.md:114 > Deletion requests ... require verification of `current_password`.
- **Code:** app/schemas/legal.py:44-49. The field is `password` (`current_password` would be 422 `extra_forbidden`).
- **Corrected text:** "...require the account's current password in the `password` field."

### 3A-31 (Low): What the idempotency fingerprint covers
- **Doc:** docs/ARCHITECTURE.md:197 > ...a SHA-256 fingerprint of the user ID, URL, and normalized JSON payload.
- **Code:** app/core/idempotency.py:37-48 and app/api/v1/orders.py:203-205, 1069-1071, 1374-1376. The fingerprint is SHA-256 of an operation name plus the validated body (`shipping_address_id` for orders). The user scope comes from the key lookup, not from the hash.
- **Corrected text:** "...a SHA-256 fingerprint of the operation and the validated request fields, stored next to the key (keys are unique per user)."

### 3A-32 (Low): Status names in the API.md checkout sequence
- **Doc:** docs/API.md:196 `Persist Order (PENDING)`. docs/API.md:207 `Transition Order to PAID`.
- **Code:** app/models/enums.py and app/services/orders.py:20-27. The order is created with `status=new, payment_status=pending`. A successful payment sets `payment_status=paid` and `status=confirmed`.
- **Corrected text:** `Persist Order (status new, payment pending)` and `Set payment_status=paid, status=confirmed, insert ledger record`.

### 3A-33 (Info): Comment and config drift found along the way (not user docs)
- .github/workflows/ci.yml:14-15 says the `ci` Hypothesis profile uses "random seeds". tests/conftest.py:90-98 sets `derandomize=True`. TESTING.md:101 is correct.
- app/core/rate_limit.py:41 ("scripts/loadtest/results/rate-limiting-research.md") and app/services/gdpr.py:96 ("scripts/loadtest/FINDINGS.md") point to a `scripts/` directory that does not exist (`ls scripts`: no such directory; not in `git ls-files`). docs/reports/T7-load.md:24 cites `scripts/loadtest/FINDINGS.md` too. load-testing-3x.md:6 at least discloses that its scripts are unpublished.
- app/tasks.py:690-691 comment says "between the erasure purge (02:30)". That is right, but the 02:30 job is named `cleanup_inactive_accounts` while it runs the erasure purge. OPERATIONS.md:90 describes it correctly, but the name misleads.
- SECURITY.md:126 says `redis:7.4.11-alpine`. docker-compose.yml:205 pins `redis:7-alpine@sha256:57e4255c…`, which T1 identifies as the 7.4.11 build. The pinned digest is right; the tag in the doc is cosmetic.
- `.env.example` does not set `EMAIL_PROVIDER` (default `smtp`, config.py:376), so a Compose quick start tries SMTP on `localhost:587` for every email. Consider documenting `EMAIL_PROVIDER=logging` for local use.
- The SETUP.md:133 registration example omits `full_name`, which triggers the welcome-email crash from prior finding 2C-05. The documented SQL promotion still works.

---

## Per-document checklist

| Document | Claims checked | Discrepancies |
|---|---|---|
| README.md | ✔ 24 (badges/versions, features, quick start, numbers table, doc links) | 3A-01, 3A-07, 3A-20, 3A-21 |
| SETUP.md | ✔ 30 (required settings, ENVIRONMENT modes, run commands, Compose roles/ports, admin SQL, payments, Stripe CLI path) | 3A-01, 3A-22 |
| SECURITY.md | ✔ 45 (token table, revocation, bcrypt, password policy, lockout, authz table, headers table, CSP, overload, secrets, GDPR, supply chain, OWASP list) | 3A-07, 3A-14, 3A-15, 3A-25, 3A-30 |
| CHANGELOG.md (4.0.0-4.0.3 plus current-behaviour items of 3.3/3.4) | ✔ 40 | none in the 4.0.x entries (all checked items match) |
| .env.example | ✔ 75 (every key vs Settings default by script, comments vs validators and compose) | 3A-23 |
| docs/ARCHITECTURE.md | ✔ 50 (middleware order, flows, state machine, layout, decisions 1-12) | 3A-04, 3A-05, 3A-12, 3A-16, 3A-17, 3A-18, 3A-27, 3A-31 |
| docs/API.md | ✔ 60 (paths/ops/tags counts, auth, shapes, problem types, status codes, idempotency, input, pagination, ETag, merge patch, HEAD, rate-limit headers, email links, webhook) | 3A-03, 3A-06 (via RES), 3A-08, 3A-09, 3A-10, 3A-11, 3A-18, 3A-32 |
| docs/OPERATIONS.md | ✔ 45 (deployment checks, proxy, body limit, cookie, probes, logging, OTel, Celery config, beat table, roles, pools, backup, Redis, GDPR jobs, knobs table) | 3A-02, 3A-13, 3A-16, 3A-24 |
| docs/TESTING.md | ✔ 35 (markers, conftest behaviour, commands, file paths, profiles, snapshot, coverage gate, CI jobs, metrics) | 3A-19, 3A-20, 3A-21 |
| docs/PERFORMANCE.md | ✔ 25 (config, overload middleware, rate-limit headers, tuning table) | 3A-15 |
| docs/RESILIENCE.md | ✔ 20 (failure matrix Retry-After values, webhook flow, guarantees, 503-vs-401, DB error mapping) | 3A-06, 3A-12, 3A-26 |
| docs/LIMITATIONS.md | ✔ 14 | 3A-16, 3A-28 |
| docs/HOW-THIS-WAS-BUILT.md | ✔ 6 (only checkable facts: password min 8, rate limiter module, 503-not-401) | none |
| docs/reports/*.md (17 files) | ✔ 40 (current-tense numbers, file paths, test names, commands, migration/table counts) | 3A-02 (T5 vs OPS), 3A-19, 3A-20, 3A-21, 3A-29, 3A-33 |
| docker-compose.yml / Dockerfile (as cited by docs) | ✔ 20 | 3A-01, 3A-26 |

---

## Verified accurate (important claims that checked out)

- **Versions:** v4.0.3 (pyproject.toml:3; the app reports `Version: 4.0.3`). Python 3.13, FastAPI 0.153.0, Starlette 1.9.0, Pydantic 2.14.1, SQLAlchemy 2.1.5, Celery 5.6.3, Stripe 16.0.0 (uv.lock). PostgreSQL 15/16/18 in CI. Compose uses postgres:15 and redis:7.
- **Settings:** every default in `.env.example` equals the Settings default, including all rate limits (`300/minute`, `60/minute`, `20/minute`, `10/minute`, `10/15 minutes`, `100/hour`, `100/hour`), token lifetimes (15 min access, 7 d refresh, 60 min reset), `MAX_REQUEST_BODY_BYTES` 1 MiB, cart TTLs 30/7 d, `CART_MAX_LINES` 100, reconciliation 7 d/60 min/200/5, Stripe timeouts 5/20/2, event retries 10/30/900 (≤3000), sweep 20 min/200, unpaid expiry 30 min and stale attempt 5 min. `RATE_LIMIT_LOGIN_FAILURES` is refused at startup. `ENVIRONMENT` is required.
- **Deployment checks:** OPERATIONS.md:7-19 and SETUP.md:55 match `check_deployment_settings` exactly: JWT key 32/48/64 bytes, webhook secret 32, Postgres and Redis passwords 16, placeholder markers, PAYMENT_PROVIDER=stripe, BCRYPT_ROUNDS ≥ 12, LOG_LEVEL not DEBUG, FRONTEND_URL https. The docs are disabled only when `ENVIRONMENT=production` (main.py:37), as documented.
- **Endpoints:** 54 paths and 67 operations. Per-tag counts in API.md:13-19 match exactly (admin 16, cart 6, catalog 6, Legal & GDPR 7, orders 9, users 20, probes 3). 201 + Location on orders, addresses, categories, products and variants. 202 on register, with the same answer for existing emails. 204 on address and product delete.
- **Problem details:** members `type/title/status/detail/instance/request_id/errors/retry_after` (errors.py:139-164). The 12 `ProblemType` slugs and their statuses in API.md:87-98 match, except the account-inactive scope (3A-08). The type base URI matches CHANGELOG 4.0.0. 415 carries `Accept` (`application/merge-patch+json, application/json` for PATCH).
- **Retry-After values:** overload 1, token or login Redis outage 30, DB outage 5, logout/reset/cart Redis outage 5, probes 5.
- **Rate limiting:** route→setting map (rate_limit.py:71-83). Headers `RateLimit-Policy: "<name>";q=;w=` and `RateLimit: "<name>";r=;t=` with the policy name derived from the setting (`default`, `search`, ...). IPv6 /64 grouping. Fail-open except the login counter. Webhook and probes exempt.
- **ETag/If-Match/If-None-Match:** SHA-256 of the model JSON. 304 on product and variant GET. 412 with the current ETag on PUT/PATCH.
- **Middleware order:** ARCHITECTURE.md:22-29 matches app/main.py registration (OTel → SecurityHeaders → RequestContext → InFlight → Forwarded → CORS → BodySize → Head). Security header values in SECURITY.md:86-95 match security_headers.py:48-70. `no-store` prefixes are users/orders/cart/admin, plus authenticated and guest-cookie requests.
- **State machine:** `ORDER_STATUS_TRANSITIONS` and `PAID_ONLY_STATUSES` (services/orders.py:20-36) match ARCHITECTURE.md:128-146.
- **Beat schedule:** the OPERATIONS.md:88-97 table matches app/tasks.py:652-702 for all 8 entries (names, task names, 02:30, 1 h, 4 h, 5 min, 03:15, 5 min, 02:45, 03:00 UTC). The Celery config values in OPERATIONS.md:79-81 (time limits 300/240, acks_late, reject_on_worker_lost, cancel-on-connection-loss, visibility 3600) and the compose `stop_grace_period: 330s` match.
- **Probes:** `/livez` returns `{"status":"ok"}`; `/readyz` uses a 2.0 s timeout, ok/fail checks and Retry-After 5. Both send `Cache-Control: no-store`, answer HEAD, and are exempt from the in-flight limiter and the rate limiter.
- **Logging:** JSON to stdout, `LOG_FILE` rotation at 100 MB x 5, root at WARNING outside development.
- **OTel:** extra `telemetry`, build arg `INSTALL_TELEMETRY`, http/protobuf only, probes excluded.
- **Postgres role script:** DML plus sequences only (initdb/10-app-role.sh:30-42). The `migrate` profile and `max_connections=200` match. Redis runs with AOF everysec and noeviction, `REDIS_MAXMEMORY` default 256mb, ports bound to loopback.
- **CI:** 16 jobs, 7 time zones as listed, PG matrix selections, test-redis on `real_redis`, pip-audit 2.10.1 with `--require-hashes`, CycloneDX 1.5 SBOM, gitleaks v8.30.1, Trivy 0.75.0 with `--ignore-unfixed` CRITICAL,HIGH. The payment-layer include list in TESTING.md:126 matches ci.yml:88 exactly. `fail_under=85`. 21 timing-sensitive tests. 26 gosu waivers in .trivyignore.yaml, consistent with "26 CVE alerts" (SECURITY.md:125). All CI actions are pinned by 40-character SHA.
- **Test infrastructure:** `REDIS_PORT=1`, WAL plus `synchronous=OFF`, `ecom_pgtests_<uuid>`, `before_cursor_execute` dirty-table hook, `_reset_database`, Hypothesis dev/ci/explore profiles as described in TESTING.md, snapshot test and `UPDATE_OPENAPI_SNAPSHOT`. All 4 live-Stripe test files named in TESTING.md:95 exist and load `.env.test`.
- **Test counts:** SQLite 5,214 passed plus 109 skipped = 5,323 non-timing tests, which matches the 01_baseline run.
- **Mutation numbers:** 3,113/3,165 = 98.4 %, 52 survivors, consistent across README, TESTING, CHANGELOG and T10.
- **CHANGELOG 4.0.1-4.0.3:** product-detail cache TTL of 5 minutes (redis_codec.py:40), 405 `Allow`, NUL/surrogate 422, `jti` required, HS-only `JWT_ALGORITHM`, DB 503 mapping (database.py:117-145) and the paid-only admin transitions all match the code.
- **Other:** password policy (min 8, 72-byte rule, SecLists 10k list, email-local-part and service-name check). Guest cart id from `secrets.token_urlsafe`, in a `__Host-` cookie. Search escapes `%`/`_` (`autoescape=True`). Email templates use `html.escape`. Webhook tolerance 300 s. Webhook duplicate returns `already_processed`.
