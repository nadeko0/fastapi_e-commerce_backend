# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [6.2.0] - 2026-11-14

### Upgrade notes
- The `sweep_stripe_events` beat task runs every minute instead of every 5. No new setting.
- Migration `0c86df1cbf98` adds five nullable columns to `payment_disputes` (evidence deadline and state). Run `docker compose --profile migrate run --rm migrate` before starting the new image.

### Added
- Each dispute stores what Stripe reports about its evidence (`Dispute.evidence_details`): the deadline (`due_by`), whether evidence was staged and submitted, whether it is past due, how many times it was submitted and how (`manual`, `smart_disputes`, `not_submitted`). Updated on every `charge.dispute.*` event; disputes stored before this release show nothing until their next event. The admin payments view (`GET /api/v1/orders/{order_id}/payments`) shows it per dispute as `evidence` (null until reported).
- OpenAPI: a dispute's `status` lists Stripe's appeal stage, `appeal_needs_response` and `appeal_under_review` (API 2026-09-30.endive and later). Such a dispute was already treated as open (order on hold, no refund); only the published values were missing.

### Fixed
- A Stripe webhook stored while the Celery broker (Redis) was unreachable was applied only about 20 minutes later: the sweeper re-queued an event only once it had been unchanged for `STRIPE_EVENT_SWEEP_AFTER_MINUTES`, a wait meant for messages lost in other ways and for events backing off between retries. In the November 2026 audit, a payment confirmed during a 75 s Redis freeze reached its order 21 minutes later (F-CHAOS-3). When the webhook route or an admin replay cannot publish an event, the event is now marked (`last_error`: "Not enqueued: ...", visible in the admin's webhook event list) and the sweeper publishes it on its next run, then clears the mark; a payment confirmed that way reaches its order within about a minute of the broker's return. Unmarked events keep the 20-minute wait, so a long queue gets no duplicate messages.

### Changed
- Dependency age rule for Python packages: one week instead of two (`[tool.uv] exclude-newer`, Dependabot's uv cooldown). Over the last year every yanked release of a locked package was replaced within 4 days and no release found to be bad was found within 14 days, while 30 security fixes waited a week longer. Images, GitHub Actions and tools keep two weeks.
- CI fails when a module under `app/` passes 800 lines. The longest ones were split by job: the order routes into `app/api/v1/orders/` and `app/api/v1/admin/` and `app/api/v1/users/` packages, the payment helpers the routes and the webhook processing share into `app/services/payment/`, and the GDPR redaction of stored Stripe events into `app/services/stripe_event_redaction.py`. No route, response or behaviour changed.
- Ruff now limits function size in `app/`: complexity 15, branches 15, statements 60 (C901, PLR0912, PLR0915). The one function past them, the Stripe webhook's `apply_event`, now looks up the payment attempt and hands the event to one handler per event type; what each event does is unchanged.
- Test tooling: pytest 9.1.1 -> 9.2.0, pytest-benchmark 5.3.0 -> 5.3.1 (held back in 6.0.0 and 6.1.0 by the two-week rule).

### Security
- dnspython 2.9.1 -> 2.9.2: CVE-2026-128089 (GHSA-vvf4-9x63-8xvv), excessive memory and CPU use when parsing a crafted WKS record from zone-file text. The app does not parse zone files (dnspython is loaded by email-validator, with the deliverability check off), so no exposure is known; upgraded all the same.
- OpenTelemetry 1.45.1 -> 1.45.2 (instrumentation 0.66b1 -> 0.66b2): CVE-2026-126360 (GHSA-mf5v-w3w3-f6cg), quadratic-time parsing of a crafted `traceparent` value in `TraceContextTextMapPropagator.extract()`. Conforming HTTP header values cannot carry the newline the attack needs, and the app's other carrier, Celery task headers, is written by the app itself, so no exposure is known; upgraded all the same. 1.45.2 was released on 2026-10-29.

### Documentation
- `docs/OPERATIONS.md`: a failed `/readyz` check means the dependency did not answer this instance within 2 s (a saturated instance can fail one probe while the database is healthy); remove an instance only after several consecutive failures.
- Load and chaos audit report: corrections from a re-verification of every numeric claim against the raw data.
- `docs/API.md` describes the dispute `evidence` and the appeal statuses; `SECURITY.md` the one-week age rule for Python packages; `docs/TESTING.md` the module-length and function-size checks.

## [6.1.0] - 2026-11-13

A MINOR release: the fixes from an external load, concurrency, chaos and perimeter audit of 6.0.0 on two 2-vCPU servers ([report](docs/reports/load-chaos-audit-2026-11.md)), two new settings (`REDIS_SOCKET_TIMEOUT`, `REDIS_SOCKET_CONNECT_TIMEOUT`), and an OpenAPI document that now names every header and shared cause a client can meet. No route, field or parameter was removed or changed in a way a client must follow. The fixes were tested in the test suite; the servers were not audited again after them.

### Upgrade notes
- New settings `REDIS_SOCKET_TIMEOUT` and `REDIS_SOCKET_CONNECT_TIMEOUT` (seconds, 1.0 each by default): a Redis command or connection that does not answer in time fails instead of waiting redis-py's default 5 s.
- The API no longer writes uvicorn's access log; one JSON line per request comes from the `app.access` logger instead (method, path, route, status, duration, client address, request id). Log pipelines that parsed uvicorn's text line need the new fields.
- Responses no longer carry a `Server` header.
- Stripe: stripe-python 16.1.0 pins API version `2026-10-28.endive` (only additions within Endive). Create a webhook endpoint with that version; see [docs/OPERATIONS.md](docs/OPERATIONS.md#going-live-with-stripe).

### Fixed
- `dispute_status` on orders: as soon as any dispute existed, every order of every customer showed the status of the newest dispute in the whole table (the subquery was not correlated to the order's payments). Each order now shows its own (F-BUG-1).
- With `WORKERS` above 1 every response on a reused connection waited about 40 ms: uvicorn's shared listener left Nagle's algorithm on for the connections the workers accepted. The workers now set `TCP_NODELAY` on every connection (F-PERF-1). Verified by a socket-level test; the latency through the proxy was not measured again.
- A frozen Redis held catalog requests for about 15 s at the median (redis-py's 5 s socket timeout per command, no timeout set by the app); both Redis clients now give up after 1 s by default (F-CHAOS-1). Not measured again on a server.
- An order's `shipping_address` copy took `is_default` and its timestamps from the live address book entry; the copy now has `is_default` false and the order's `created_at`.
- Two concurrent admin refunds of the same flagged payment: the loser got Stripe's refusal as an error; it is now a 409 "This payment has already been fully refunded".
- Celery's own lines (task received, succeeded, failed; beat's "Sending due task") were not written although the worker and beat ran with `--loglevel=info`, so periodic tasks could not be traced in the logs; they now go through the application's JSON logging at the worker's `--loglevel`.
- Emails had no `Date` or `Message-ID` header and no plain-text part; they now have both headers and a text/plain part generated from the HTML.
- `RateLimit` and `RateLimit-Policy` were sent only on responses a route returned itself; every counted response, 4xx included, now carries them, and the failed-login 429 names its own policy.
- `HEAD` on the health probes answered a `Content-Length` of 0; it now matches GET.
- Country normalisation of addresses was not a fixed point for a value that title-cases to two characters ("ß" to "Ss", then "SS" on the next read). Not reachable through the API (a country has at least two characters), found by the response round-trip property test.

### Changed
- A provider failure that leaves a payment attempt `unknown` logs one WARNING line with the error class, Stripe's request id, the HTTP status, the provider code and a hash of the idempotency key.
- OpenAPI: `RateLimit` and `RateLimit-Policy` are declared on every 429 and `WWW-Authenticate` on every 401; where a route describes its own 400, 401 or 503, the description also names the causes every route shares (a body that is not JSON, a bad token, the in-flight limit). A problem body carries `retry_after` whenever `Retry-After` is set.
- CORS exposes `Location` (the 201 of a POST) and allows the `X-Request-ID` request header.
- Documentation: the webhook API version, the worst case of the Stripe timeouts (about 16 s and 31 s observed against a theoretical 76.5 s), a table of Redis key TTLs, how to find PaymentIntents left unconfirmed after a lost provider answer, and the audit report with its charts.
- Dependencies: stripe 16.1.0, pydantic 2.14.2, hypothesis 6.170.14, dnspython 2.9.1, wrapt 2.5.2, python-dateutil 3.0.0 (used only by Celery and freezegun; the application keeps time in UTC). Held back: pytest 9.2 (pytest-benchmark 5.3.1 passes the two-week rule on 2026-11-15).

## [6.0.0] - 2026-11-11

A MAJOR release: three request bodies changed (API clients must update, see "Migrating API clients"), and the Docker Compose stack moves to PostgreSQL 18, whose data volume layout a 15 volume cannot be started with (see "Upgrading a deployment"). It also closes the remaining findings of the 2026-11-10 review, tightens the type and lint configuration, and documents every route's outcomes in the OpenAPI document.

### Migrating API clients
- `POST /api/v1/orders/{order_id}/pay`: the body is now
  ```json
  {"amount": "25.00", "currency": "USD", "payment_method_token": "pm_..."}
  ```
  `payment_method_token` is required (a body without it is a 422; Stripe refused a confirm without a payment method anyway, so such a request was never paid in production). `order_id` (the order is the one in the path) and `payment_method` (always Stripe) must no longer be sent: a body with them is a 422 `extra_forbidden`. `currency` stays optional. The response is unchanged and still carries `order_id` and `payment_method` ("stripe").
- `POST /api/v1/admin/categories`: `metadata` is no longer accepted (it was never stored); category responses no longer carry `metadata`, `product_count` or `children_count` (always `{}` and 0 before).
- `POST /api/v1/legal/data-request`: `request_type` other than `export` or `deletion` is a 422 from the schema (it was a 400 from the route).
- Sessions: a refresh token family ends `REFRESH_SESSION_MAX_DAYS` (default 30) after the login that started it, however often it is refreshed. After that `POST /api/v1/users/refresh` answers the same 401 as for an expired refresh token, and the client logs in again. Before, a session refreshed at least weekly never ended.

### Upgrading a deployment
- PostgreSQL 15 to 18: `docker-compose.yml` builds the database from `docker/postgres/Dockerfile` (`postgres:18.6-alpine` plus Alpine security updates) and mounts the volume on `/var/lib/postgresql`. An existing 15 volume is refused at start; move the data with a dump and restore, step by step in [docs/OPERATIONS.md](docs/OPERATIONS.md#postgresql-major-upgrades-postgresql-15-to-18). Supported servers are now PostgreSQL 16, 17 and 18.
- Redis 7 to 8.8, built from `docker/redis/Dockerfile` (`redis:8.8-alpine` plus Alpine security updates). 8.8 is the newest release redis-py 8.1 supports.
- Run the migrations (`docker compose --profile migrate run --rm migrate`): revision `f180919aaf3b` adds a CHECK on `payments.status`, a partial unique index allowing one active default address per user (older duplicate defaults are resolved first, the newest kept), `payments.reconciled_at`, and a partial index for the unpaid-order sweep. It stops without changing anything if a payment row has a status outside the known values.
- `docker compose up -d --build`: the db and redis images are built locally now.
- New setting `REFRESH_SESSION_MAX_DAYS` (default 30, at least `REFRESH_TOKEN_EXPIRE_DAYS`).

### Fixed
- An erasure request cancels the account's new, unpaid orders, returns their stock and closes their open payment attempts at Stripe. The deactivated owner could still pay one and get an order behind 403 on every route.
- An erasure request no longer deadlocks against an order being placed by the same account: it takes the user's row lock first, as `POST /orders` does.
- GDPR request mails are sent after the response: a mail failure can no longer turn a recorded request into a 500, and the client does not wait for SMTP.
- Changing the default address: two concurrent changes of one user's addresses could deadlock on PostgreSQL (a 500); they now run one after the other. Updating an address so that it equals another active one is a 409 (it was a 500), and the database allows one active default address per user.
- Reconciliation checks the window's payments least recently reconciled first (`payments.reconciled_at`), so above `RECONCILIATION_MAX_PAYMENTS` attempts in the window the older ones are no longer skipped for ever.
- `payments.status` is checked by the database against the values the code writes.
- `POST /admin/payments/{id}/refund` no longer returns Stripe's refusal text to the client; it is logged, as on the order refund route.
- The mock payment provider refuses to confirm a PaymentIntent without a payment method, as Stripe does; it used to pay with a default card.

### Changed
- The OpenAPI document states every status code each route answers with its cause, the limits and defaults a client meets (payment window, minimum charge, unpaid-order cap, rate limits, token lifetimes, cart limits, sorting values), and the 6.0.0 changes on the routes concerned. `docs/API.md` has a table of these limits.
- Images: the app image applies Debian security updates at build time, and the database and Redis images Alpine's, so a fix is not waited for until the official images are rebuilt; Docker Compose builds every image without layer cache (`no_cache`), so those updates run on each `--build` instead of being taken from a cached layer. CI scans the three built images with Trivy 0.76.1 and runs weekly on a schedule as well; uv 0.13.6.
- CI tests PostgreSQL 16, 17 and 18 (the whole suite on 18) and Redis 8.8. Dependabot watches the three Dockerfiles and holds Python to 3.13.
- Stricter mypy (`warn_return_any`, `warn_unreachable`, `disallow_any_generics`, extra error codes, fully typed payment layer and tasks) and ruff (logging, broad-except, comprehension, return, pytest-style and pylint rules, a ban on the sync Redis client outside the worker) settings; the code was fixed to pass them.
- Dependencies: FastAPI 0.162.0 (Starlette 1.10.0), SQLAlchemy 2.1.6, alembic 1.20.1, stripe 16.0.1, pydantic-settings 2.16.2, ruff 0.17.3, hypothesis 6.170.13, coverage 7.17.0, httpx2 2.16.0, tzdata 2026.6; setup-uv 10.3.3 in CI. Held back: pytest 9.2 (pytest-benchmark 5.3.0 does not run on it; 5.3.1 does, and is too new under the two-week rule) and python-dateutil 3.0 (a major release of a Celery dependency that changes time zone behaviour).
- Documentation: accepted limitations of the 2026-11-10 review, the GDPR scope, reconciliation and rate-limiter limits are recorded in `docs/LIMITATIONS.md`; `docs/OPERATIONS.md` has going-live notes for Stripe and the 6.0.0 upgrade.

### Removed
- The `update_product_stats` task and its schedule: it cached best sellers in Redis under a key nothing read.
- The unused synchronous Redis client the API process created at start.

## [5.1.0] - 2026-11-10

A MINOR release: fixes from a full review of payments, orders, configuration and the catalog, with new settings (`RATE_LIMIT_CONSENT`, `SMTP_TLS_VERIFY`, `DOCKER_API_BIND`) and two refund statuses newly published in the OpenAPI document. No route, field or parameter was removed.

Upgrade notes: `ENVIRONMENT=staging` and `production` now refuse to start with `EMAIL_PROVIDER=logging` or `SMTP_TLS_VERIFY=false`; `EMAIL_PROVIDER` accepts only `smtp` and `logging`; `CHECKOUT_SESSION_EXPIRE_MINUTES` must be at least 31. Development and test accept the logging provider and a mail catcher with a self-signed certificate (`SMTP_TLS_VERIFY=false`).

### Fixed
- A flagged second charge refunded through `POST /admin/payments/{id}/refund` keeps its manual-review flag in the database, which marks it as the charge that did not pay the order. Clearing it made the refund's webhook mark the paid order refunded, and the order's own `POST /orders/{id}/refund` then picked the already refunded charge, so the charge that paid the order could no longer be refunded through the API. The API still reports `requires_manual_review=false` once such a charge is fully refunded (admin payment list and its filter, order payments view), and the admin route records the refund under the order's lock, like every refund event.
- The expiry sweep no longer cancels and restocks an order that its customer paid for while the webhook was late or had failed: when Stripe refuses to expire a session or cancel a PaymentIntent, the attempt is read back, and one that is paid or being paid keeps the order for the webhook and for reconciliation. An attempt Stripe does not know at all still counts as closed.
- Cancelling an order (admin `PUT`/`PATCH /orders/{id}/status`, customer `POST /orders/{id}/cancel`) closes its open payment attempts at Stripe after the commit. A Checkout Session of a cancelled order used to stay payable for its whole lifetime while its stock was back on sale.
- A new payment attempt (`/pay` or `/checkout-session` with a new `Idempotency-Key`) on an order placed more than `UNPAID_ORDER_EXPIRE_MINUTES` ago is answered 409. A fresh attempt now and then kept an unpaid order, and its stock, out of the expiry sweep for good. A retry with the key of an earlier attempt is still served.
- A `/checkout-session` key whose request died before Stripe answered is sent to Stripe again once older than `PAYMENT_ATTEMPT_STALE_AFTER_MINUTES`, as on `/pay`. Every retry used to be answered 409 with `Retry-After` for ever.
- Two `POST /orders` of one account run one after the other (a `FOR NO KEY UPDATE` lock on the user's row). With different keys they used to read the same cart and the same count of unpaid orders: eight parallel requests placed eight orders from one cart and passed `MAX_PENDING_ORDERS_PER_USER`. A short window remains between the first request's commit and the emptying of the cart in Redis.
- An order whose total is below Stripe's minimum charge for the store currency (0.50 USD, 0.50 EUR, 0.30 GBP and the other currencies Stripe lists) is answered 422 and takes no stock. It used to be placed, hold its stock until it expired, and fail at every `/pay`. A currency without a published minimum is left to Stripe.
- A refund is recorded with Stripe's own status. The live provider folds `canceled` and `requires_action` into `pending`, so a refund canceled at once was counted as money on its way back. `RefundResponse.status` publishes the five values Stripe documents.
- Reconciliation goes on when another run (the daily task and an admin's run for one order) records the same new difference first: the payment counts as an error. The unique index used to stop the task with the rest of the window unchecked, or answer 500 on the admin route.
- SMTP STARTTLS verifies the server certificate and host name. `smtplib` used an unverified context by default, so anyone on the path to the mail server could read `SMTP_PASSWORD` and every reset link. A relay with a private CA is trusted through `SSL_CERT_FILE` / `SSL_CERT_DIR`.
- `EMAIL_PROVIDER` is `smtp` or `logging`; a misspelt value used to select the logging provider silently.
- `CHECKOUT_SESSION_EXPIRE_MINUTES` must be at least 31: the session's `expires_at` is counted from the attempt's creation, so 30 fell below Stripe's 30-minute minimum by the time of the call.
- The consent history stores at most 512 characters of the User-Agent, and consent changes are limited per user (`RATE_LIMIT_CONSENT`, default `10/hour`, one count shared by `POST /legal/consent` and `PUT`/`PATCH /users/me` when they change `marketing_consent`). Each change used to add an entry with the whole header (up to 16 KiB) without limit, to a row loaded on every authenticated request. The history itself is never trimmed: it is the evidence of consent.
- The privacy policy, terms of service and cookie policy carry a fixed version and effective date (2026-11-10) instead of the time of the request, and the terms state prices in `STORE_CURRENCY` instead of "EUR including VAT".
- `GET /products?in_stock=true` follows the variants of a product that has an active one (in stock when an active variant has stock), and a boolean characteristic filter (`true`/`false`) matches on PostgreSQL and SQLite. `str(True)` matched neither.
- A load-shed 503 carries the CORS headers: CORSMiddleware now sits outside the in-flight limit, so a browser sees the 503 and its `Retry-After` instead of an opaque network error.

### Added
- `SMTP_TLS_VERIFY` (default `true`): `false` skips the certificate check for a local mail catcher; refused in staging and production.
- `RATE_LIMIT_CONSENT` (default `10/hour`).
- `DOCKER_API_BIND` in `docker-compose.yml`: the host address the API port is published on (default every interface; `127.0.0.1` when the reverse proxy runs on the same host).

### Changed
- On `docker stop` the API finishes requests in flight for up to 90 s (`timeout_graceful_shutdown` in `main.py`), and Docker waits 100 s (`stop_grace_period`) before SIGKILL. A `/pay` call can wait on Stripe about 76 s in the worst case; Docker's default of 10 s cut it off.
- Unused code removed: `PasswordUpdate`, both `TokenPayload` schemas, `CategoryUpdate`, and the unused Redis session helpers.

## [5.0.0] - 2026-11-03

A MAJOR release: the operationIds of the OpenAPI document were shortened (69 renamed), so a client generated from the spec gets new method names and has to be regenerated. No route, parameter or payload was removed or changed on the wire. The additions to the HTTP API below are backward compatible. There was no 4.2.0 release: that number was chosen first, corrected before anything was tagged, and its changes are listed here.

### Changed
- **Breaking for generated clients:** operationIds are the handler names (`get_product`) instead of FastAPI's default built from name and path (`get_product_api_v1_products__product_id__get`); a handler serving a PUT and a PATCH gets the method as a suffix.
- The OpenAPI document is a complete, typed contract: enums are published without runtime validation, required fields are stated, the `guest_cart` cookie, `ETag` / `If-Match`, `X-Request-ID` and `Retry-After` are documented, and routes carry tags and an admin marker. `tests/unit/test_openapi_contract.py` (27 tests) checks it and the snapshot test pins the whole document. The live spec is at `/api/v1/openapi.json`.
- Provider error text is no longer echoed to clients on `POST /orders/{id}/pay`, `/checkout-session` and `/refund`: the response carries a fixed message (for a refund above what is left, the payment's own refundable amount) and the provider's text goes to the log.
- One log line per dispute status change (it used to be one per event), and a `Stripe event <id> (<type>) stored` line per stored webhook delivery.
- The `.gitleaks.toml` allowlists the placeholder token of the `Token` response example, and only that value in `app/schemas/user.py` and the OpenAPI snapshot.

### Added
- `GET /products/{id}` returns `has_variants` and the active `variants`; `GET /products` and `GET /products/search` carry `has_variants`. For a product with variants the parent's price and stock are not the purchasable values: adding it to the cart without a variant is answered 409.
- Orders carry a nullable `dispute_status` (customer order detail, order history, admin order list), and `GET /admin/orders` takes `has_dispute=true|false`.

### Fixed
- The admin `PUT` and `PATCH` of a product check `If-Match` against the same ETag as `GET /products/{id}`; for a product with variants the two differed.
- The PaymentIntent of a Checkout Session carries `order_id` and `payment_id` in its metadata, so `payment_intent.succeeded` alone settles a Checkout order (`checkout.session.completed` is then a no-op), even when the session event is late or lost.
- A decline on Stripe's hosted Checkout page (`payment_intent.payment_failed`) is only logged. It used to fail the attempt and the order although the customer could retry on the same session.
- A refund made with `POST /orders/{id}/refund` is recorded with source `admin` even when its webhook is processed first.
- A lost dispute only flags the order (payment status `refunded`); it does not cancel it or return stock, and the manager corrects stock by hand with `PUT` or `PATCH /admin/products/{id}` or `/admin/products/{id}/variants/{variant_id}`.
- CI: the product detail cache hit stays free of the variants query and the payment guards are covered by tests.

## [4.1.2] - 2026-11-02

A PATCH release: it fixes the Celery worker's healthcheck in `docker-compose.yml` and the benchmark check in CI. No change to the application code or the API.

### Fixed
- The `celery-worker` healthcheck in `docker-compose.yml` runs under `timeout 9`. Each check starts a whole Celery process (70-120 MB); one that outlived the 10 s healthcheck timeout was counted as failed but left running, so on a CPU-starved host the checks piled up, about 30 MB more every 10 s, until the kernel killed the worker for memory. Found by a load test on a 2 vCPU server.

### Changed
- The `benchmark` CI job times the base commit and HEAD one after the other on the same runner (`.github/scripts/benchmark_ab.sh`) and fails when a median on HEAD is more than 35% slower. It used to compare with a baseline saved by an earlier run on `main`, which came from another runner: six reruns of the 4.1.1 commit landed on three CPU models whose medians differed by up to 2.26 times, and three of the six failed the 80% gate with no change to the code. On one runner, six runs of unchanged code put HEAD within 6% of the base on the CPU benchmarks and within 22% on the request benchmarks.

## [4.1.1] - 2026-11-02

A PATCH release: it fixes the entity-tag header parser and two faults of the migration environment, and adds checks to CI. No API change, no new endpoint or setting.

### Fixed
- `app/core/etag.py` parses `If-Match` and `If-None-Match` in one pass: it finds the quoted tags with a simple pattern and checks only the text between them. CodeQL alert 8 flagged the expression that validated the whole header as polynomial ReDoS; timing showed linear growth, so it was not exploitable, but that expression is gone. Hypothesis tests check that the parser accepts and rejects exactly what it did.
- `migrations/env.py` disposes the database engine it creates, avoiding leaked connections when running Alembic in-process.
- `migrations/env.py` no longer disables the application's loggers. `fileConfig` defaulted to `disable_existing_loggers=True`, so a process that ran migrations itself lost its own logs from then on; random test order exposed it. A caller with its own logging can skip `alembic.ini`'s logging with `config.attributes["configure_logger"] = False`.
- Throwaway PostgreSQL databases from migration tests use the session naming convention (`ecom_pgtests_<epoch>_<hex>`), allowing the stale database sweep to clean them up.

### Added
- Static security analysis CI job `sast` running Bandit 1.9.4 offline on `app/`.
- Migration compatibility CI job `migration-compat`: the newest tagged release's database tests of orders, payments, checkout, carts, refunds, users and addresses, run against the current migrations on PostgreSQL 15; it ends at once when `migrations/` is unchanged since that tag. Also a PostgreSQL migration round-trip test (`tests/integration/test_postgres_migration_roundtrip.py`) in `test-postgres`.
- Benchmark CI job `benchmark` comparing hot path performance against the newest baseline from `main`, failing when a median is more than 80% slower.
- Dev dependencies `mypy`, `pytest-socket`, `pytest-randomly`, and `pytest-benchmark`.
- Mutation testing rerun on this release: 3,434 mutants, 98.7% detected. 31 of the 74 first-run survivors were gaps in the tests and are killed by new tests; the other 43 are equivalent.

### Changed
- The `lint` CI job now includes type checking with `mypy` (0 errors across 80 files); `app/` was annotated for it without a behaviour change.
- The one Bandit finding, `HOST = "0.0.0.0"` in `app/core/config.py`, is marked `# nosec B104` with the reason: inside a container the port mapping decides what is exposed.
- Test suite execution runs in randomized order by default via `pytest-randomly`, printing the seed used for reproducibility.
- Network access in the test suite is blocked by default via `pytest-socket`, allowing only local and loopback connections.

## [4.1.0] - 2026-11-02

Three independent audits of 4.0.3 reviewed the code, ran it under load and fuzzing, and checked it against written specifications. This release closes what they found. MINOR, because it adds endpoints and settings; no response field was removed.

### Added
- `GET /api/v1/admin/payments` and `POST /api/v1/admin/payments/{payment_id}/refund`: a charge flagged for manual review, such as a late payment on a cancelled order, can be listed and refunded. A retry after success answers 409 without calling the provider.
- `CHECKOUT_SESSION_EXPIRE_MINUTES` (default 60), sent to Stripe as `expires_at`; `MAX_PENDING_ORDERS_PER_USER` (default 10).
- Orders keep what they were placed with: product name, variant SKU and attributes on lines, the shipping address on the order. One migration, backfilling existing orders.
- The data export is assembled and mailed to the account's own address as JSON. Marketing consent changes through `PATCH /users/me` are recorded in the consent history.
- Regression suites `tests/torture_suite`, `tests/strict_domain_audit` and `tests/audit_verification`.

### Changed
- An order with a payment attempt that can still be paid is neither expired nor cancellable by its customer (409 with Retry-After). Older attempts are closed at Stripe before the order is cancelled, and after a success the order's other attempts are closed.
- A product with active variants is bought through a variant: a line without one answers 409, in the cart and at checkout.
- Erasure is accepted while an order or dispute is open. The account's data goes on schedule; the order's address snapshot stays until the order is finished and no dispute is open, at most 30 days for an unfinished order. The last active administrator cannot request erasure (409).
- Reconciliation also compares Checkout attempts whose webhook was lost; `skipped` stays 0.
- The deletion mail says the deletion is scheduled, and a second mail confirms it after the purge.
- An order total above 21,474,836.47, what a payment can record, answers 422.

### Fixed
- Payments:
  - A stale refund event no longer rewinds a settled refund and returns a refunded order to paid.
  - `/refund` no longer answers 500 after the provider refunded, when an earlier failure was not yet processed.
  - A refund of a disputed charge answers 409. A dispute landing during the call is recorded and flagged.
  - `/pay` answers 409 when the success could not be applied.
  - A crashed attempt no longer answers 409 for good.
- Checkout and stock:
  - A variant checkout no longer deadlocks against a plain line of the same product, answered 503 before.
  - A restock or cart merge past the integer range no longer fails.
  - A retry racing its first request gets its order, not "Cart is empty".
  - A product delete racing a checkout falls back to a soft delete.
  - One failing order no longer stops the expiry sweep.
- Accounts:
  - A password reset evicts logins and refresh rotations in flight, and logout evicts a racing rotation.
  - A logged-out token is no longer accepted again up to a second before it expires.
  - Registration without a name now sends the verification mail.
  - Login normalises the email as registration does.
- GDPR:
  - Purges at volume no longer die on the idle-in-transaction timeout.
  - Webhook events about an erased customer are redacted when processed.
  - No mail goes to an anonymised address.
  - The retention status compares exact age.
- Protocol:
  - JSON nested deeper than 64 levels or holding NaN or Infinity is 422, where deep JSON could break the public catalog with 500s.
  - Price filters are bounded, and money takes ASCII digits only.
  - `*` in `If-Match` counts only alone.
  - PATCH with a foreign `+json` type is 415.
  - A client waiting exactly Retry-After is admitted.
  - A product's ETag follows its variants.
- Operations:
  - A Redis outage on a cart read is 503, not an empty cart, and a guest merge past the line cap keeps the remainder.
  - Passwords with URL characters work in the database URI.
  - `/health` and database connects have time limits.
  - SQL parameters stay out of logs and errors.
  - Celery logs as JSON.

### Removed
- Order-email Celery tasks nothing enqueued. Those mails go through BackgroundTasks, as they did.

### Documentation and tests
- Documents corrected against the code, including the Compose quick start and the backup commands.
- Stale code comments corrected.
- Lock tests now fail when the lock is removed.
- Test runs killed hard no longer leave their temp database behind.
- Dependabot waits 14 days and proposes no major PostgreSQL or Python image.

## [4.0.3] - 2026-10-26

### Added
- Regression tests killing all 50 surviving non-equivalent mutants from mutmut 3.8.0 mutation testing (mutation score raised to 98.4%, 3113 of 3165 mutants killed).
- Test coverage for rare branches of the live Stripe provider (refund listing, webhook tolerance, and card error details).
- Test coverage for unusual webhook payloads, GDPR redaction of stored events, payment reconciliation counters and paging, 405 and 415 problem details, and rate-limit keys for non-Bearer or unresolvable tokens.

## [4.0.2] - 2026-10-26

### Added
- Tests that run Celery tasks through a real embedded worker and pin `acks_late`, `reject_on_worker_lost` and the retry bound of Stripe event processing.
- Webhook signature tests built with the Stripe SDK's own helper (tampered payload, stale timestamp, rotated secrets) and a test mapping Stripe's 429 to 503 with Retry-After.
- Tests pinning the OAuth2 password `grant_type` handling and checking that every beat schedule entry names a registered task.

### Fixed
- Unreachable database (connection refused or lost, pool checkout timeout, invalidated connection) returns 503 with Retry-After instead of 500, enabling Stripe webhook delivery retries.
- Celery broker outage during Stripe event queueing logs a single-line warning instead of a full traceback, leaving retry to the periodic sweeper.

### Security
- Startup validation restricts `JWT_ALGORITHM` to symmetric HMAC algorithms (`HS256`, `HS384`, `HS512`), preventing algorithm confusion attacks.

## [4.0.1] - 2026-10-25

### Added
- CI test matrix with PostgreSQL 15, 16, and 18, and real Redis integration covering Lua scripts and WATCH transactions.
- Automated daylight-saving transition replay tests across 2026 transitions and multiple time zones.
- N+1 query prevention tests on all listing endpoints, response round-trip tests, and live migration tests over seeded data.

### Fixed
- String inputs with NUL bytes or lone surrogates now return 422 validation problems instead of 500 errors; non-Unicode tokens return 401.
- Redis outage during authentication returns 503 with Retry-After without revoking the token family or issuing unregistered tokens.
- Product detail cache invalidates immediately on stock adjustments (orders, cancellations, expirations) and TTL reduced to 5 minutes.
- Admin order status transitions to confirmed, processing, sent, or delivered require paid status, returning 409 Conflict otherwise.
- Product creation without images returns 422 instead of creating orphan database rows on 500 errors.
- Admin catalog writes (products, categories, variants) commit only after response serialization succeeds.
- Input-only rules (lengths, patterns, minimums) apply to request models only, so reading a stored row that predates a rule no longer fails with a 500.
- 405 Method Not Allowed responses include an Allow header listing all valid methods for the requested path.
- Country code validation normalizes Unicode before length validation, returning 422 if post-normalization length exceeds bounds.
- Order confirmation emails format order timestamps in UTC with an explicit timezone marker.
- Offline migration rendering (`alembic upgrade head --sql`) renders the complete migration chain cleanly.
- OpenAPI schema reflects all enforced input validation constraints.

### Security
- Access tokens lacking a `jti` claim are rejected immediately (401 Unauthorized).
- Docker runtime image excludes `pip` to eliminate five CVE findings in vendored tooling.
- Redis base image pinned to patched 7.4.11 release; verified Postgres image gosu CVEs are uncallable via govulncheck.
- CI automated scans fail on any fixable CRITICAL or HIGH vulnerabilities (Trivy) or committed credentials (gitleaks).

## [4.0.0] - 2026-10-24

### Added
- Added `GET /users/addresses/{id}` to fetch an individual address by ID, enabling standard 201 `Location` headers on creation.
- Documented 403 `account-inactive` responses across all authenticated OpenAPI operation definitions.

### Changed
- **Breaking:** Removed outer response envelope `{success, data, error, timestamp, request_id}` (Google AIP-131); single resources return directly, paginated lists return `{items, total, page, size, has_more}`, and `X-Request-ID` is in the response header. Clients must read resource fields at the root of the JSON body.
- **Breaking:** `GET /users/orders` returns a standard page object `{items, total, page, size, has_more}`; `GET /orders/{id}/payments` returns `{"items": [...]}`; confirmation endpoints return `{"message": ...}`. Clients must adjust response deserialization.
- **Breaking:** Standardized all error responses on RFC 9457 Problem Details (`application/problem+json`) with stable URIs under `https://github.com/nadeko0/fastapi_e-commerce_backend/problems/<slug>`. Clients must parse `detail`, `type`, and `errors[]` instead of legacy error structures.
- **Breaking:** Changed resource creation status codes to 201 Created with `Location` header (`POST /orders`, `/users/addresses`, `/admin/categories`, `/admin/products`, `/admin/products/{id}/variants`), and 202 Accepted for `POST /users/register`. Clients must expect 201/202 instead of 200.
- **Breaking:** Changed deletion status codes to 204 No Content for address and product deletions (subsequent delete returns 404). Clients must handle empty 204 responses.
- **Breaking:** Replaced 400 with 409 Conflict for business state conflicts (invalid order transitions, cancellations or refunds in invalid states, insufficient stock, inactive address, already verified email). Clients must handle 409 for state conflicts.
- **Breaking:** Deactivated accounts return 403 Forbidden with `account-inactive` instead of 400. Clients must inspect 403 on authenticated endpoints.
- **Breaking:** Card payment declination returns 422 Unprocessable Content with `payment-declined` instead of 402. Clients must update payment failure error handling.
- **Breaking:** Malformed JSON bodies return 400 Bad Request with `malformed-request` (was 422), and unsupported Content-Type returns 415 Unsupported Media Type with `Accept` header (was 422). Clients must handle 400 and 415.
- **Breaking:** Missing or malformed `Idempotency-Key` returns 400 Bad Request with `idempotency-key-missing` (was 422); an in-flight payment attempt returns 409 Conflict with `Retry-After` (was 200). Clients must retry 409 after the indicated delay.
- **Breaking:** Strict request body validation enabled (`extra="forbid"`). Clients must not send undeclared fields (e.g. `current_password` on password reset) or requests will be rejected with 422.
- **Breaking:** Monetary amounts in request bodies must be formatted as decimal strings (e.g. `"10.50"`). Clients must not send numeric JSON floats.
- **Breaking:** Date filters (`start_date`, `end_date`) must include an explicit timezone offset. Clients must send RFC 3339 formatted timestamps.
- **Breaking:** `GET /admin/stats` amounts are formatted as decimal strings rather than numbers. Clients must parse amounts as strings.
- **Breaking:** Email verification and password reset emails link to frontend routes (`FRONTEND_URL/verify-email?token=...`, `FRONTEND_URL/reset-password?token=...`) that forward tokens to the POST API endpoints. Frontends must implement these landing pages.
- **Breaking:** `ENVIRONMENT` configuration setting is required and must be one of `development`, `test`, `staging`, or `production`. Deployments must provide this environment variable.
- **Breaking:** Application logs stream to `stdout` by default; file logging is disabled unless `LOG_FILE` is explicitly configured. Deployments must capture container stdout logs.

### Removed
- **Breaking:** Removed `GET /users/verify-email/{token}`, `GET /users/password/reset/{token}`, `GET /users/data/export`, `POST /users/data/delete`, and `POST /users/consent`. Clients must use `POST /users/verify-email`, `POST /users/password/reset-token/validate`, `POST /legal/data-request`, and `POST /legal/consent`.
- **Breaking:** Removed `POST /users/password/reset/{token}` (token in URL path). Clients must use `POST /users/password/reset/confirm` with `{token, new_password}` in the request body.
- **Breaking:** Removed query parameters for `POST /orders`, `PUT /orders/{id}/status`, `POST /cart/items`, and `PUT /cart/items/{id}`. Clients must provide all request payloads as JSON bodies.
- **Breaking:** Removed `Deprecation`, `Sunset`, and `Link` response headers. Clients must not rely on legacy deprecation headers.
- **Breaking:** Removed `PUBLIC_BASE_URL` and `GDPR_EXPORT_MAX_RECORDS` configuration settings. Deployments must use `FRONTEND_URL` for email links.

### Fixed
- Resolved dynamic OpenAPI generation bug where error responses reverted to `application/json` when FastAPI rebuilt documentation.
- `GET /users/addresses/default` returns 405 Method Not Allowed (`Allow: POST`) instead of matching the numeric ID route and failing with 422.

## [3.4.0] - 2026-10-24

### Added
- HEAD method support on all GET routes (RFC 9110 Section 9.1), counted under GET rate limits and included in 405 Allow headers.
- `GET /admin/products/{id}/variants/{variant_id}` with ETag and `If-None-Match` (304), enabling conditional updates with `If-Match`.
- Optional OpenTelemetry distributed tracing (FastAPI, SQLAlchemy, Redis, Celery, requests) via `OTEL_*` settings and optional `telemetry` package extra.
- Propagation of `request_id` and `trace_id` into Celery task context and execution log lines.
- Enforced 24-hour expiration (`IDEMPOTENCY_KEY_TTL_HOURS`) for `POST /orders` idempotency keys, while payment and checkout keys remain permanent.

### Changed
- Application JSON logs stream to stdout at `LOG_LEVEL` by default (Twelve-Factor App); rotating file logging is optional via `LOG_FILE`.
- Standardized database constraint naming conventions across 13 foreign keys and 2 unique constraints on PostgreSQL (migration `c5e2a8f4b196`).

### Fixed
- Added PATCH to CORS allowed methods to support cross-origin browser requests.

## [3.3.1] - 2026-10-24

### Changed
- Test suite performance optimized with pytest-xdist parallelization and single SQLite schema initialization per process, cutting test execution time from ~13 minutes to ~3.5 minutes while keeping 99% coverage and 100% payment gate.

### Security
- Enforced bounds for `BCRYPT_ROUNDS` (4-31), rejecting values below 12 outside development and test environments during startup checks.

## [3.3.0] - 2026-10-23

### Added
- PATCH support using JSON Merge Patch (RFC 7396) for `/users/me`, `/users/addresses/{id}`, and admin product and variant endpoints.
- HTTP `ETag` and conditional validation (`If-None-Match` returns 304, `If-Match` returns 412) on product details and admin updates.
- ISO 4217 `currency` field returned alongside all monetary amounts across products, variants, carts, and statistics.
- Idempotency-Key validation following the IETF draft: reusing a key with a mismatched request payload returns 422.
- `X-Request-ID` and W3C `traceparent` propagation on all responses and structured log records.
- Standard IETF `RateLimit` and `RateLimit-Policy` response headers, and `Retry-After` on all 503 responses.
- Kubernetes-compatible `/livez` and `/readyz` health probe endpoints supporting HEAD and returning `Cache-Control: no-store`.
- Query sorting support via `sort_by` and `sort_order` across paginated endpoints against allowed field lists.
- Replacement endpoints: `POST /users/verify-email` and `POST /users/password/reset-token/validate`.
- Daily retention period enforcement for deactivated accounts and GDPR redaction of customer data in stored Stripe webhook payloads.
- CI pipeline artifact generation for CycloneDX SBOM and standard OCI container labels.

### Deprecated
- Marked legacy endpoints with RFC 9745/8594 `Deprecation`, `Sunset`, and `Link` headers: `/users/*` GDPR routes (use `/legal/*`), GET email verification and password reset token inspection, and query parameters on order/cart mutations.

### Changed
- Upgraded FastAPI to 0.153.0.
- Modernized password policy per NIST SP 800-63B-4: removed arbitrary character rules, added NFC normalization and a 10,000-entry common password blocklist.
- Email addresses normalized to Unicode NFC prior to lookup and authentication attempt rate limiting.
- `GET /products` accepts characteristics filter via JSON query parameter rather than an invalid request body.
- Password reset confirmation endpoint accepts `PasswordResetConfirm` (`new_password` only), ignoring extraneous `current_password`.
- Removed eight unused configuration settings.

### Fixed
- Password reset via token no longer incorrectly requires `current_password`.
- Stripe Checkout Session creation timeouts are marked unknown, preventing duplicate sessions when retrying with new idempotency keys.

## [3.2.5] - 2026-10-23

### Changed
- Upgraded FastAPI to 0.152.0, pydantic-settings to 2.16.1, and httpx to 2.15.1.

### Fixed
- Application loggers configured to write at `LOG_LEVEL` (default INFO) in production with third-party loggers at WARNING, preserving GDPR audits and security events; timestamps formatted as RFC 3339 in UTC.
- Phone number input validation enforces ITU-T E.164 standard formatting (1-9 country code, max 15 digits).

### Security
- Hardened OWASP HTTP response security headers: `Referrer-Policy: no-referrer`, `Cross-Origin-Resource-Policy: same-origin`, `Cross-Origin-Opener-Policy: same-origin`, and strict CSP `default-src 'none'; frame-ancestors 'none'`.
- Tightened CORS configuration to allow only explicit HTTP methods and headers while exposing `Retry-After`.
- CI pipeline integrates `pip-audit` scanning against all locked dependencies.

## [3.2.4] - 2026-10-23

### Changed
- Modernized FastAPI route dependencies to `typing.Annotated` form with shared type aliases.
- Migrated SQLAlchemy ORM models to 2.0 `Mapped[...]` and `mapped_column` declarations with explicit 2.0-style queries.
- Refactored Stripe integration to instantiate dedicated `StripeClient` objects with explicit timeouts and retries.
- Consolidated monetary validation across models into a reusable Pydantic `Money` type.
- Updated codebase to Python 3.13 union typing syntax and expanded Ruff linting rulesets (`UP`, `B`, `ASYNC`, `DTZ`, `FAST`, `SIM`).
- Pinned GitHub Actions dependencies by commit SHA, upgraded uv to 0.13.5, and adopted native pyproject.toml pytest configuration.

## [3.2.3] - 2026-10-23

### Changed
- OpenAPI schemas updated from deprecated `example` to `examples` across legal and user schemas.
- Celery configured to cancel long-running tasks upon broker disconnect with late task acknowledgments to prevent duplicate processing.
- Test suite treats all Pydantic and FastAPI deprecation warnings as fatal errors.

### Fixed
- Database session commit errors during request termination now properly return 500 rather than 200, returning connections to the pool before response dispatch.
- Address validation errors sanitize reflected input values when country strings exceed length constraints during normalization.
- JWT decoding explicitly enforces required claims (`exp`, `sub`, `type`), rejecting tokens lacking expiration or subject claims.

## [3.2.2] - 2026-10-23

### Added
- Architectural AST guards and concurrency heartbeat tests preventing blocking synchronous calls on the event loop.
- Parity test suite verifying state consistency between synchronous and asynchronous Redis implementations.

### Changed
- Converted all 43 FastAPI route handlers from synchronous blocking database/Redis operations to `AsyncSession` and `AsyncRedisService`, eliminating event loop stalls under concurrent load.
- Reused Celery synchronous payment, stock, and event logic within async routes via `AsyncSession.run_sync` and threadpools.
- Reduced web worker database pool footprint to a single connection pool.
- Dispatched CPU-intensive bcrypt hashing to threadpools, releasing database connections prior to hash computation.

## [3.2.1] - 2026-10-23

### Changed
- Stripe HTTP client configured with bounded timeouts and retries (`STRIPE_CONNECT_TIMEOUT_SECONDS`, `STRIPE_READ_TIMEOUT_SECONDS`, `STRIPE_MAX_NETWORK_RETRIES`), reducing maximum worst-case call latency from 4 minutes to 75 seconds.
- Added concurrent PostgreSQL indexes on unindexed foreign keys (`order_items.order_id`, `product_id`, `variant_id`, `orders.shipping_address_id`) and removed redundant indexes (migration `821e97899aca`).
- Configured Celery worker graceful shutdown timeout (330 seconds) and Redis socket timeouts.
- Sensitive configuration settings typed as `SecretStr` to prevent credential leakage in string representations and dumps.

### Fixed
- Stripe call timeouts in `/pay` transition to an "unknown" status: same-key retries replay with identical idempotency keys, while new keys receive 409 Conflict.
- Stripe upstream errors (rate limits, network timeouts, API errors) return 503 Service Unavailable instead of 500.
- Passwords exceeding 72 bytes return standard invalid credentials errors rather than crashing bcrypt with 500 errors.
- Integer inputs exceeding 32-bit bounds and decimal inputs exceeding `Numeric(10,2)` return 422 instead of 500.
- Deterministic locking order by product ID prevents deadlocks during concurrent multi-item checkout and cancellation transactions.
- Added tie-breaker primary key ordering to all paginated list queries to prevent skipped or duplicated rows across pages.
- Email dispatch performed outside database transactions, preventing database idle locks during SMTP transport.
- Resolved `UnboundLocalError` in session teardown when database connection acquisition failed.

### Security
- Enforced 1 MiB payload ceiling via `MAX_REQUEST_BODY_BYTES`, rejecting oversized bodies with 413 Payload Too Large before reading data.
- Added `Cache-Control: no-store` headers to all responses containing tokens, client secrets, or personal data.
- 401 Unauthorized responses include standard `WWW-Authenticate` headers; invalid bearer tokens specify `error="invalid_token"`.
- 422 validation response bodies omit raw input values to prevent credential exposure.
- Enabled Redis Append-Only File (AOF) persistence to ensure revoked token blocklists survive restarts.

## [3.2.0] - 2026-10-22

### Added
- Asynchronous Stripe webhook ingestion: events are acknowledged immediately with 200 and processed asynchronously by Celery with retry backoff and scheduled sweeping.
- Admin webhook audit endpoints: `GET /admin/webhook-events` and `POST /admin/webhook-events/{id}/replay`.
- Automated daily payment reconciliation against the Stripe API, storing discrepancies for audit (`GET /admin/reconciliation/discrepancies`, `POST /admin/reconciliation/run`).
- PostgreSQL integration test suite and five-time-zone matrix testing in CI.

### Changed
- Converted all database timestamp columns to `TIMESTAMP WITH TIME ZONE` (migration `ada27dd39537`) and emitted RFC 3339 timezone-aware timestamps across all API responses.
- Modernized core dependencies to use latest idioms across Pydantic V2, SQLAlchemy 2, and FastAPI lifespan handlers.

### Fixed
- Normalized CORS configured origins by stripping trailing slashes, fixing cross-origin browser requests.
- `POST /admin/reconciliation/run` returns 503 rather than 500 when Celery task brokers are unavailable.
- Resolved timezone offset shift bugs in GDPR export, deletion request IDs, and session IDs caused by naive UTC timestamps.
- Fixed product image URL serialization warnings.

## [3.1.0] - 2026-10-22

### Added
- Admin payment audit endpoint: `GET /orders/{id}/payments` listing attempts, refunds, disputes, and review flags.
- Webhook processors for `refund.created`, `refund.updated`, `refund.failed`, `charge.refunded`, `payment_intent.canceled`, and `charge.dispute.*` (putting disputed orders on hold).
- Hypothesis state machine test suite and multi-currency buyer journey tests covering live Stripe test mode.
- 100% line and branch test coverage requirement enforced in CI for payment processing modules.

### Changed
- Refunds are rejected if the underlying charge is actively disputed, preventing double reimbursement.
- Restricted `STORE_CURRENCY` validation to 139 currencies supported by Stripe.
- Order notification emails format amounts with the order's active currency rather than hardcoded "$" signs.

### Fixed
- Prevented refund association with incorrect secondary charges, race conditions between concurrent refunds, and duplicate refund processing during Redis outages.
- Fixed webhook handler to ensure only successful `PaymentIntent` events mark orders as paid.
- Pending refunds no longer marked as completed until confirmed; failed refunds no longer deducted from refundable balances.
- Ensured out-of-order webhook events that arrive before local payment attempt records are preserved.
- Sub-cent fractional amounts are rejected rather than silently rounded; oversized amounts and malformed signatures return client errors rather than 500.
- Resolved race conditions on concurrent identical idempotency keys in `/pay` and `/checkout-session`.

## [3.0.0] - 2026-10-21

### Added
- Automatic expiration and restocking of unpaid orders after `UNPAID_ORDER_EXPIRE_MINUTES`.
- Customer order cancellation endpoint: `POST /orders/{id}/cancel` for unpaid orders.
- Configuration settings: `PAYMENT_PROVIDER`, `STORE_CURRENCY`, `FRONTEND_URL`, `PUBLIC_BASE_URL`, `RATE_LIMIT_GUEST_CART`, and `GDPR_EXPORT_MAX_RECORDS`.

### Changed
- User registration returns generic 200 response without revealing whether the email already exists, sending an account notice to existing users.
- Account deletion via `/legal/data-request` requires current password confirmation.
- Password reset token inspection (`GET /users/password/reset/{token}`) no longer returns account email.
- Authentication fails closed with 503 Service Unavailable when Redis is unreachable.
- Global logout without a refresh token revokes all existing sessions across devices.
- Password reset link expiration reduced to 60 minutes (`PASSWORD_RESET_TOKEN_EXPIRE_MINUTES`).
- Required `Idempotency-Key` header (8-255 characters) on `/pay`, `/checkout-session`, and `/refund`.
- Concurrent payment attempts on the same order rejected with 409 Conflict.
- Strict currency validation: payments must match order currency (`STORE_CURRENCY`), disallowing zero-decimal or three-decimal currencies.
- Checkout session redirect URLs restricted strictly to `FRONTEND_URL`.
- Search query text capped at 100 characters and matched literally without wildcard evaluation.
- Health endpoint (`/health`) reports service status without exposing system environment details.
- Docker deployment runs as non-root, non-superuser Postgres role with base images pinned by SHA digest.

### Removed
- Removed unused `DATA_ENCRYPTION_KEY` configuration setting.
- Removed deprecated `RATE_LIMIT_LOGIN_FAILURES` setting in favor of split IP and email limits.

### Fixed
- Celery email retry tasks properly process the intended target recipient on redelivery.
- `POST /users/consent` records privacy policy consents accurately.
- Resolved 500 errors on invalid query parameters, price filter inversions, and missing category assignments.
- Differentiated variant database integrity errors from SKU uniqueness conflicts.
- Removed ineffective hourly cart cleanup background task.

### Security
- Fixed payment security: strict amount and currency validation in webhooks, row-level locks on orders during payment transitions, and prohibition of mock provider in production.
- Single-use password reset tokens, disabled login for deactivated accounts, and case-insensitive unique email index (`lower(email)`).
- Disabled OpenAPI/Swagger documentation endpoints in production mode.
- Rate limiting on guest cart creation per IP address.
- Comprehensive GDPR erasure scrubbing addresses, consent IP logs, and export requests in bounded batches.

## [2.0.0] - 2026-10-20

### Added
- Guest shopping carts with automated merging into customer account carts upon authentication.
- Worker overload protection via `MAX_IN_FLIGHT_REQUESTS`, returning 503 with `Retry-After`.
- Cart price validation endpoint (`/cart/validate`) and dynamic live pricing in cart responses.

### Changed
- Required `Idempotency-Key` header (8-255 characters) on `POST /orders`, returning 422 if omitted.
- Active rate limiting across operations: 300/min per caller, 60/min search, 20/min auth, 10/min checkout, and failed login throttling (requires `TRUSTED_PROXIES` behind proxies).
- Explicit null values on database NOT NULL columns return 422 rather than causing database errors.
- Asynchronous database and Redis access implemented for catalog and authentication endpoints.

### Fixed
- Resolved persistent 409 Conflict bug when processing payments against live Stripe test mode.
- Offloaded synchronous Stripe API calls from the main event loop to worker threads.
- Celery worker tasks manage database sessions properly, resolving order confirmation email delivery failures.
- Preserved newly added items during active checkout transactions.
- Disallowed ordering of inactive catalog items.
- Optimized GDPR data export queries, reducing database query count from 165 to single batch queries.

### Security
- Token revocation blocklist keyed by JWT `jti` claim, preventing blocklist bypass via padding characters.
- Replaced unmaintained `python-jose` library with `PyJWT` to resolve critical CVEs in dependencies.
- HTML-escaped product titles in email templates to prevent HTML injection.

## [1.1.0] - 2026-10-20

### Changed
- Upgraded SQLAlchemy to 2.1 on psycopg 3 driver (`postgresql+psycopg://`).
- Refreshed core dependencies: FastAPI 0.151, Starlette 1.9, Pydantic 2.14, Stripe SDK 16, uv 0.13.
- Multi-stage Docker image without build tools and container health checks without curl.
- Docker Compose log rotation capped at 3 x 10 MB per container; Celery beat schedule stored in `/tmp`.
- Centralized project version management strictly in `pyproject.toml`.

### Removed
- Unmaintained local helper scripts removed from repository distribution.

### Fixed
- Celery beat tasks, routes, and rate limits registered by explicit names rather than import paths, restoring scheduled background jobs and rate limits.
- Alembic database downgrades past migration `b6d7b96c6d17` fixed by naming foreign key constraints.
- Resolved flaky cryptographic signature verification tests caused by base64url padding slack bits.

## [1.0.0] - 2026-10-20

### Added
- Initial complete release of FastAPI e-commerce backend managed by `uv`.
- Product catalog with categories and product variants (size, color, SKU, inventory tracking).
- Shopping cart with session management and inventory reservation.
- Stripe payment integration supporting mock and live test modes, Stripe Checkout Sessions, webhooks, and idempotent payment processing.
- JWT authentication with refresh token rotation and token revocation blocklist.
- User management with role-based access control (customer and admin roles) and address book.
- GDPR compliance endpoints for data export and erasure with background batch processing.
- Background asynchronous tasks using Celery and Redis broker for transactional emails and scheduled jobs.
- Structured JSON logging across all application components.
- Automated CI pipeline with linting, unit/integration testing (85% coverage gate), and Docker container builds.

[6.0.0]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v5.1.0...v6.0.0
[5.1.0]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v5.0.0...v5.1.0
[5.0.0]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v4.1.2...v5.0.0
[4.1.2]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v4.1.1...v4.1.2
[4.1.1]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v4.1.0...v4.1.1
[4.1.0]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v4.0.3...v4.1.0
[4.0.3]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v4.0.2...v4.0.3
[4.0.2]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v4.0.1...v4.0.2
[4.0.1]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v4.0.0...v4.0.1
[4.0.0]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v3.4.0...v4.0.0
[3.4.0]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v3.3.1...v3.4.0
[3.3.1]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v3.3.0...v3.3.1
[3.3.0]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v3.2.5...v3.3.0
[3.2.5]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v3.2.4...v3.2.5
[3.2.4]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v3.2.3...v3.2.4
[3.2.3]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v3.2.2...v3.2.3
[3.2.2]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v3.2.1...v3.2.2
[3.2.1]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v3.2.0...v3.2.1
[3.2.0]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v3.1.0...v3.2.0
[3.1.0]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v2.0.0...v3.0.0
[2.0.0]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v1.1.0...v2.0.0
[1.1.0]: https://github.com/nadeko0/fastapi_e-commerce_backend/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/nadeko0/fastapi_e-commerce_backend/releases/tag/v1.0.0

---

See also: [SETUP.md](SETUP.md), [docs/reports/release-4.0.0.md](docs/reports/release-4.0.0.md), [docs/reports/fixes-4.0.x.md](docs/reports/fixes-4.0.x.md).
