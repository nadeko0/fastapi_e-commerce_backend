# Known Limitations and Deliberate Non-Goals

Every engineering design is a series of deliberate tradeoffs. This document outlines conscious design boundaries, known architectural limitations identified during verification campaigns, and notes on potential alternatives (because every system has trade-offs, and admitting them early prevents awkward surprises later).

## Product Limitations

- **Multi-Factor Authentication (MFA / TOTP / WebAuthn):**
  - *Why not:* MFA requires a dedicated authentication lifecycle, secret vaulting, and account recovery UX. As a documented tradeoff, minimum password length remains 8 characters (NIST 800-63B rev. 4 permits 8 only with MFA, recommending 15 without it).
  - *Where to start:* Implement RFC 6238 TOTP as a mandatory second factor for administrative roles, followed by WebAuthn passkeys for customers.
- **Multi-Currency Store Operations:**
  - *Why not:* Supporting multiple store currencies introduces foreign exchange volatility, conversion rounding rules, cross-currency refund tracking, and complex accounting reports. The store operates in a single currency (`STORE_CURRENCY`), while orders record their currency at placement. Store analytics sum totals assuming `STORE_CURRENCY`; changing store currency mixes historical totals.
  - *Where to start:* Add exchange rate snapshot tables and compute sales analytics per presentment currency.
- **Full-Text Catalog Search:**
  - *Why not:* The catalog size is adequately served by PostgreSQL `ILIKE` filters; an older btree index on `(name, description)` was removed in v3.2.1 as ineffective for infix queries.
  - *Where to start:* Add PostgreSQL `tsvector` or `pg_trgm` indexes, or integrate an external search engine.
- **Administrative UI and GraphQL:**
  - *Why not:* This repository provides a backend REST API; an administrative dashboard is a frontend concern. REST with OpenAPI, ETag validation, and JSON merge-patch covers all client integration needs.
- **Data Retention for Inactive Live Accounts:**
  - *Why not:* GDPR storage limitations (`USER_DATA_RETENTION_DAYS`) currently apply only to deactivated accounts (`is_active=False`). Deleting live accounts without clear policy definitions or explicit warning notices risks deleting active customers.
  - *Where to start:* Establish formal retention terms and implement pre-erasure notification emails.

## Platform and Operations Limitations

- **Container Image Signing and Provenance:**
  - *Why not:* While CycloneDX SBOM generation and OCI labels are built into CI, Cosign/Sigstore signing and SLSA provenance require an external registry and deployment pipeline.
  - *Where to start:* Integrate Sigstore keyless signing into the GitHub Actions release workflow.
- **Kubernetes and Helm Manifests:**
  - *Why not:* The application is Twelve-Factor compliant (environment configuration, `/livez` and `/readyz` probes, graceful SIGTERM shutdown, stdout logging), but deployment manifests belong in infrastructure repositories.
  - *Where to start:* Author Helm charts defining PodDisruptionBudgets, resource quotas, and horizontal pod autoscalers.
- **Prometheus Metrics (`/metrics`):**
  - *Why not:* OpenTelemetry distributed tracing and structured JSON logs fulfill current observability requirements; scrapeable Prometheus metrics are a subsequent enhancement.
  - *Where to start:* Add `prometheus-fastapi-instrumentator` to export request duration histograms.
- **PostgreSQL Image Gosu Vulnerability (Campaign Finding):**
  - *Why not:* The pinned `postgres:15-alpine` image includes `gosu` built with Go 1.24.6 and `moby/sys/user` v0.1.0, flagged in security scans as GO-2026-6686 (DoS parsing `/etc/passwd`). The vulnerability is practically unreachable in our environment because passwd files are root-owned.
  - *Where to start:* Rebase once Docker Library updates the official image, or build a custom PostgreSQL base image executing directly as the `postgres` user.
- **Dedicated Email Queue and Retries:**
  - *Why not:* Transactional emails currently rely on Celery tasks and BackgroundTasks without an isolated retry broker.
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
  - *Why not:* Offset pagination (`skip` / `limit`) is simple but scales poorly on deep sets; country fields currently accept 2-letter strings without validating full ISO 3166-1 tables.
  - *Where to start:* Adopt keyset/cursor pagination using opaque tokens, and enforce strict ISO country validation.

## Testing and Verification Limitations

- **Extended Soak Test Duration (Campaign Finding T7):**
  - *Why not:* The 1-hour load soak showed API memory growth of 6.4 MiB (~1%) with a decelerating rate consistent with Python allocator warmup. Confirming a permanent RSS plateau requires a 4 to 6 hour continuous soak on isolated hardware.
  - *Where to start:* Execute a 6-hour Locust load benchmark on a dedicated machine.
- **CI Trivy Database Caching (Campaign Finding T1):**
  - *Why not:* Continuous integration downloads the full Trivy vulnerability database on every run, risking registry rate limits during heavy CI usage.
  - *Where to start:* Cache `~/.cache/trivy` across workflow runs in GitHub Actions.

See also: [ARCHITECTURE.md](ARCHITECTURE.md), [OPERATIONS.md](OPERATIONS.md), [PERFORMANCE.md](PERFORMANCE.md), [RESILIENCE.md](RESILIENCE.md), [reports/final-test-campaign.md](reports/final-test-campaign.md).
