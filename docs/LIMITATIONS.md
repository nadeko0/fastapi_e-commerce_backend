# Known Limitations and Deliberate Non-Goals

Every engineering design is a series of deliberate tradeoffs. This document outlines conscious design boundaries, known architectural limitations identified during verification campaigns, and notes on potential alternatives (because every system has trade-offs, and admitting them early prevents awkward surprises later).

## Product Limitations

- **Multi-Factor Authentication (MFA / TOTP / WebAuthn):**
  - *Why not:* MFA requires a dedicated authentication lifecycle, secret vaulting, and account recovery UX. NIST SP 800-63B-4 asks 15 characters for single-factor authentication without MFA, but the project maintains an 8-character minimum password length by deliberate project decision.
  - *Where to start:* Implement RFC 6238 TOTP as a mandatory second factor for administrative roles, followed by WebAuthn passkeys for customers.
- **Multi-Currency Store Operations:**
  - *Why not:* Supporting multiple store currencies introduces foreign exchange volatility, conversion rounding rules, cross-currency refund tracking, and complex accounting reports. The store operates in a single currency (`STORE_CURRENCY`), while orders record their currency at placement. Store analytics sum totals assuming `STORE_CURRENCY`; changing store currency mixes historical totals.
  - *Where to start:* Add exchange rate snapshot tables and compute sales analytics per presentment currency.
- **Full-Text Catalog Search:**
  - *Why not:* The catalog size is adequately served by case-insensitive substring matching (`lower(column) LIKE`, via SQLAlchemy `icontains`) on name and description; an older btree index on `(name, description)` was removed in v3.2.1 as ineffective for infix queries.
  - *Where to start:* Add PostgreSQL `tsvector` or `pg_trgm` indexes, or integrate an external search engine.
- **Administrative UI and GraphQL:**
  - *Why not:* This repository provides a backend REST API; an administrative dashboard is a frontend concern. REST with OpenAPI, ETag validation, and JSON merge-patch covers all client integration needs.
- **Data Retention for Inactive Live Accounts:**
  - *Why not:* GDPR storage limitations (`USER_DATA_RETENTION_DAYS`) currently apply only to deactivated accounts (`is_active=False`). Deleting live accounts without clear policy definitions or explicit warning notices risks deleting active customers.
  - *Where to start:* Establish formal retention terms and implement pre-erasure notification emails.

## Platform and Operations Limitations

- **Container Image Signing and Provenance:**
  - *Why not:* While CycloneDX SBOM generation and OCI labels are built into CI, Cosign/Sigstore signing and SLSA provenance require an external registry and deployment pipeline.
  - *Where to start:* Add a GitHub Actions release workflow (there is none; images are not published) that pushes to a registry and signs with Sigstore keyless signing.
- **Kubernetes and Helm Manifests:**
  - *Why not:* The application is Twelve-Factor compliant (environment configuration, `/livez` and `/readyz` probes, graceful SIGTERM shutdown, stdout logging), but deployment manifests belong in infrastructure repositories.
  - *Where to start:* Author Helm charts defining PodDisruptionBudgets, resource quotas, and horizontal pod autoscalers.
- **Prometheus Metrics (`/metrics`):**
  - *Why not:* OpenTelemetry distributed tracing and structured JSON logs fulfill current observability requirements; scrapeable Prometheus metrics are a subsequent enhancement.
  - *Where to start:* Add `prometheus-fastapi-instrumentator` to export request duration histograms.
- **PostgreSQL Image Gosu Vulnerability (Campaign Finding):**
  - *Why not:* The `postgres:18.6-alpine` base image (built on in `docker/postgres/Dockerfile`) includes `gosu` built with Go 1.24.6 and `moby/sys/user` v0.1.0, flagged in security scans as GO-2026-6686 (DoS parsing `/etc/passwd`). The vulnerability is practically unreachable in our environment because passwd files are root-owned.
  - *Where to start:* Rebase once Docker Library updates the official image, or build a custom PostgreSQL base image executing directly as the `postgres` user.
- **Base Image Versions (Python 3.13, PostgreSQL 18, Redis 8.8):**
  - *Why not:* Every base image is pinned by digest, and a major or minor version is adopted by hand, with a test run, never by a bot (the Dependabot configuration ignores major Python and PostgreSQL updates, see `SECURITY.md`). Python stays on 3.13 on purpose. Redis stays on 8.8, the newest release the redis-py client lists as supported (8.10 exists); PostgreSQL 18 is the newest psycopg 3.3 supports. PostgreSQL 16 and 17 are supported and tested in CI, 18 is what Compose deploys.
  - *Where to start:* Move Redis when redis-py lists a newer release; change the pins in `docker/postgres/Dockerfile`, `docker/redis/Dockerfile` and `.github/workflows/ci.yml` and the expected tags in `tests/unit/test_docker_compose.py` together. A PostgreSQL major upgrade follows "PostgreSQL Major Upgrades" in `docs/OPERATIONS.md`.
- **Database Owner Role Is the Superuser:**
  - *Why not:* The API and the Celery worker connect as `APP_DB_USER`, a role that can read and write rows and nothing else (no superuser, no DDL, no new databases or roles). Migrations run through the `migrate` profile service as `POSTGRES_USER`, which is the superuser the PostgreSQL image creates and the owner of the schema. That superuser is not used by the running application and the port is published on loopback only, but it is still the account that owns the data.
  - *Where to start:* Create a second, non-superuser owner role in `docker/postgres/initdb/`, give it `CREATE` on the schema, and run the `migrate` service as that role. Until then, keep the `migrate` service run on demand only and use a long random `POSTGRES_PASSWORD` (the application refuses to start outside development with a shorter one).
- **Dedicated Email Queue and Retries:**
  - *Why not:* Emails are sent after commit without retries: from API routes in-process via BackgroundTasks, and from webhook processing and the expiry task inside the Celery task that ran them. A crash after commit loses the email.
  - *Where to start:* Route email tasks to a dedicated Celery queue with exponential backoff and dead-letter handling.

## API Contract Boundaries

- **Concurrent Refresh-Token Reuse Window (Campaign Finding):**
  - *Why not:* The auth service enforces strict token family revocation. Two concurrent requests using the same refresh token (e.g. multi-tab browser races) trigger theft detection and revoke the entire token family.
  - *Where to start:* Introduce a brief grace period (e.g. 5 seconds) during which a reused refresh token returns the newly issued pair instead of revoking the family.
- **Delayed Payment Methods Returning 409 Until Webhook (Campaign Finding):**
  - *Why not:* Retrying `POST /orders/{id}/pay` with the same `Idempotency-Key` while an asynchronous payment method is pending returns HTTP 409 with `Retry-After` until resolved by a webhook. Differentiating "request currently in progress" from "payment pending at provider" requires an additional database state column.
  - *Where to start:* Add a `provider_pending` state and migration to distinguish in-flight requests from provider processing.
- **Provider-Enforced Over-Refunds (Campaign Finding):**
  - *Why not:* The API delegates balance checking for refunds exceeding the remaining amount to Stripe (which returns HTTP 400). Pre-validating with an internal HTTP 409 requires an explicit database link between idempotency keys and refund records to avoid misinterpreting valid retries.
  - *Where to start:* Add an `idempotency_key` column to `payment_refunds` with a unique index.
- **Lax Boolean and Number Coercion (Campaign Finding T3 F5):**
  - *Why not:* Pydantic's non-strict mode accepts `0`/`1` as booleans and numeric strings as timestamps. Enforcing `StrictBool` and strict dates changes acceptable input contracts, which would represent a breaking API change.
  - *Where to start:* Enable strict schema validation globally.
- **Pagination and Country Validation:**
  - *Why not:* Offset pagination (`page` / `size`) is simple but scales poorly on deep sets; country fields accept any string of 2 to 100 characters (a two-letter value is upper-cased, anything else title-cased) without checking it against ISO 3166-1.
  - *Where to start:* Adopt keyset/cursor pagination using opaque tokens, and enforce strict ISO country validation.
- **Single-Level JSON Merge Patch:**
  - *Why not:* Partial resource updates implement RFC 7396 semantics one level deep. Nested object values (`characteristics`, `attributes`) replace the stored object as a whole rather than merging keys recursively. This is a deliberate design difference from RFC 7396.
  - *Where to start:* Implement recursive dictionary merging for nested JSON attributes.
- **Admin Flagged Payment Refund Retry (409):**
  - *Why not:* Retrying `POST /api/v1/admin/payments/{payment_id}/refund` after a successful refund returns HTTP 409 without calling the payment provider because nothing of the charge is left to refund. The payment keeps its manual-review flag (it marks the charge that did not pay the order); the API reports `requires_manual_review=false` once the charge is fully refunded.
  - *Where to start:* Persist a dedicated refund idempotency record to permit idempotent replays of admin refund operations.
- **Live Product Image References:**
  - *Why not:* Orders snapshot product names, variant SKUs, and variant attributes at checkout time, but line item image URLs are read dynamically from current catalog records. Modifying or deleting product images changes or clears displayed thumbnails on past order views.
  - *Where to start:* Snapshot image URLs onto order line item records at checkout.

## Accepted Findings of the 2026-11-10 Audit

A full code review found these. Each was judged low impact and is accepted as it is.

- **Stale Product Detail in the Cache:**
  - *Why not:* A reader can load a product row, a writer can commit and delete the cache key, and the reader then stores the old row under that key. `GET /products/{id}` serves it until the entry expires, at most 5 minutes later (`PRODUCT_CACHE_TTL` in `app/services/redis_codec.py`; `REDIS_PRODUCT_CACHE_TTL_HOURS` is the category tree's lifetime). This is display only: carts and checkout price from the database.
  - *Where to start:* Store a version (for example `updated_at`) with the entry and refuse to overwrite a newer one, or shorten the TTL.
- **In-Flight Limit Counts Requests Still Sending Their Body:**
  - *Why not:* `InFlightLimitMiddleware` counts a request for its whole ASGI call, which includes receiving the body, so `MAX_IN_FLIGHT_REQUESTS` slow uploads can hold every slot of a worker and the rest get 503. A reverse proxy that buffers request bodies (the nginx default) hands the application only complete requests, so it is not reachable there; Compose has no proxy, so a deployment without one is exposed.
  - *Where to start:* Run behind a buffering proxy, or count a request only from the moment its body has been read.
- **Token `iat` and the Password-Change Watermark Use Different Clocks:**
  - *Why not:* An access token is revoked after a password change when its `iat` is older than the user's watermark in Redis. The `iat` is written by the host that issued the token and the watermark by the host that handled the change, so with hosts whose clocks differ by more than the time between a login and the change, a token issued before the change can look newer and live on until it expires (`ACCESS_TOKEN_EXPIRE_MINUTES`, 15). Refresh tokens are revoked by family, not by clock.
  - *Where to start:* Keep every host on NTP, or take both timestamps from the Redis server's clock.
- **Downgrade of Revision `821e97899aca` Can Fail:**
  - *Why not:* The downgrade recreates the B-tree index on product `(name, description)`. A description long enough to exceed the index's row size limit makes it fail. Upgrades are not affected, and a downgrade is preceded by a backup anyway (see `docs/OPERATIONS.md`).
  - *Where to start:* Do not recreate that index in the downgrade, or index `md5(description)`.
- **Settings Without Bounds:**
  - *Why not:* `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` and `STRIPE_WEBHOOK_TOLERANCE_SECONDS` accept any integer, so a zero, a negative or an absurd value starts the application and breaks it later (a webhook tolerance of 0 turns the timestamp check off in Stripe's libraries). The settings that guard money and security have bounds or deployment checks; these are operator choices.
  - *Where to start:* Add `Field(ge=..., le=...)` to each, as the newer settings have.
- **The Whole User Row on Every Authenticated Request:**
  - *Why not:* `get_current_user` loads the full `users` row with each authenticated request, although a route usually needs only a few of its columns. It is one primary-key query per request.
  - *Where to start:* Load only the needed columns, or cache them briefly with the revocation checks.
- **Redis Socket Timeouts Are the Client's Defaults:**
  - *Why not:* The application sets only `REDIS_POOL_TIMEOUT` (the wait for a free pooled connection). Socket connect and read timeouts are redis-py's own defaults (5 seconds in 8.1), which the chaos run on a frozen Redis showed to be short enough (503 after about 2 s), but they are not written down in the settings.
  - *Where to start:* Add explicit `REDIS_SOCKET_TIMEOUT` and connect-timeout settings and pass them to both clients.
- **Celery Worker Concurrency Is Not Pinned:**
  - *Why not:* The worker command has no `--concurrency`, so Celery starts one prefork child per CPU of the host. Each child builds its own database pool (`DB_POOL_SIZE` plus `DB_MAX_OVERFLOW`, opened lazily), so the number of connections the worker can use depends on the host, and the `max_connections=200` of Compose is sized for the default four-worker stack.
  - *Where to start:* Set `--concurrency` in the worker command and count it in the `max_connections` formula in `docs/OPERATIONS.md`.
- **Rotating Log File with Several Processes:**
  - *Why not:* With `LOG_FILE` set, every API worker process opens the same `RotatingFileHandler`; processes rotating one file can lose or interleave records (a limitation the Python logging cookbook documents). By default `LOG_FILE` is unset and logs go to stdout only, which is how Compose runs.
  - *Where to start:* Leave `LOG_FILE` unset and collect stdout, or log to a file per process.

## GDPR Scope

Implemented: access and portability (Art. 15 and 20, `POST /legal/data-request` with `export`), erasure (Art. 17) with order and dispute protections, consent records, and retention of deactivated accounts. Not implemented:

- **Restriction of Processing (Art. 18):**
  - *Why not:* There is no "freeze but do not delete" account state. It would need a new user state handled in every query path (orders, cart, admin), and it is rarely used in a small shop.
  - *Where to start:* Add an account flag that blocks logins and processing jobs while keeping the data, and handle requests manually until then.
- **Records of Processing (Art. 30):**
  - *Why not:* A record of processing activities is a document kept by whoever operates the shop (what is processed, why, for how long, who receives it), not application behaviour.
  - *Where to start:* Write it for the deployment, using the data model and `docs/OPERATIONS.md` as inputs.
- **Breach Procedure (Art. 33 and 34):**
  - *Why not:* Detecting a breach, assessing the risk and notifying the supervisory authority and users within 72 hours is an incident-response process of the operator, not something to encode as an endpoint.
  - *Where to start:* Write the procedure for the deployment; the structured logs with request ids are the main evidence source.
- **Crypto-Shredding of Backups:**
  - *Why not:* Erasure scrubs the live database, but a database backup taken before it still holds the data until that backup is deleted. Per-user encryption keys, destroyed on erasure, would make old backups unreadable, but they need a key-management service and a migration of every personal-data column.
  - *Where to start:* Keep backups for a short, documented time so that erased data ages out, and say so in the privacy policy; then evaluate field-level encryption with a KMS.
- **Signed-Link Export Delivery:**
  - *Why not:* The export is assembled in the request and mailed inline as JSON to the account's verified address. Delivery through a file in object storage behind an expiring signed link needs storage the project does not have.
  - *Where to start:* Write the export to object storage and mail a signed URL that expires after `GDPR_EXPORT_EXPIRY_HOURS`.

## Payment Reconciliation and Rate Limiting

- **Reconciliation Starts from Local Rows:**
  - *Why not:* The daily reconciliation takes payments recorded in the database and compares each with Stripe. A charge that exists at Stripe but has no local payment row (for example one created by hand in the Dashboard, or by another integration on the same account) is never discovered.
  - *Where to start:* List recent PaymentIntents from Stripe and report those without a local row as a new discrepancy kind.
- **Fixed-Window Rate Limit Residuals:**
  - *Why not:* The limiter counts in fixed windows, so a caller can spend one window's budget at its end and the next one's at its start: up to twice the limit within a short span. Password-reset and verification mail routes are limited per requesting address (`RATE_LIMIT_AUTH`), not per target mailbox, so many addresses can each send mail to one mailbox. When Redis cannot be reached the limiter lets every request through (fail open; only the failed-login count fails closed), so there is no ceiling during an outage.
  - *Where to start:* Smooth bursts with `limit_req` at the proxy; add a per-mailbox counter on the reset and verification routes; for the outage case, a per-process in-memory fallback limit.

## Testing and Verification Limitations

- **Extended Soak Test Duration (Campaign Finding T7):**
  - *Why not:* The 1-hour load soak showed API memory growth of 6.4 MiB (~1%) with a decelerating rate consistent with Python allocator warmup. Confirming a permanent RSS plateau requires a 4 to 6 hour continuous soak on isolated hardware.
  - *Where to start:* Execute a 6-hour Locust load benchmark on a dedicated machine.
- **CI Trivy Database Caching (Campaign Finding T1):**
  - *Why not:* Continuous integration downloads the full Trivy vulnerability database on every run, risking registry rate limits during heavy CI usage.
  - *Where to start:* Cache `~/.cache/trivy` across workflow runs in GitHub Actions.

See also: [ARCHITECTURE.md](ARCHITECTURE.md), [OPERATIONS.md](OPERATIONS.md), [PERFORMANCE.md](PERFORMANCE.md), [RESILIENCE.md](RESILIENCE.md), [reports/final-test-campaign.md](reports/final-test-campaign.md).
