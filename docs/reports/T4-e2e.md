# Stage T4: End-to-End Black-Box Verification (Live Stripe)

Unit tests prove that individual functions work in theory; end-to-end tests prove that real transactions complete without dropping funds into the ether. Stage T4 drives black-box user journeys against Stripe test mode, delivering webhooks via the Stripe CLI to confirm payment, refund, and reconciliation integrity.

## Purpose and Exit Criterion

The purpose of Stage T4 was to perform comprehensive black-box end-to-end verification over HTTPS against real Stripe test mode with live webhooks delivered through the official Stripe CLI, verifying full business flows across the stack.

Exit criteria:
- Complete pass rate across all 14 core business scenarios (catalog, auth, cart, checkout, payments, declines, 3DS, refunds, disputes, webhooks, reconciliation, admin, GDPR).
- Zero 5xx responses from the application.
- 100% webhook delivery and processing with zero error logs in API or Celery worker.
- Complete financial reconciliation with Stripe API showing zero discrepancies.

## Environment

- **Host:** WSL2 Ubuntu staging environment (commit `0d794fc3`, tag `v4.0.0`).
- **Entry point:** `https://localhost:8443` via Caddy 2.11.6 (`caddy-root.crt`).
- **Email capture:** Mailpit v1.31.4 on `127.0.0.1:8025`.
- **Payment provider:** Real Stripe test mode.
- **Stripe CLI:** `stripe/stripe-cli:v1.52.2` (digest `sha256:ca5598cb...`, API version `2024-11-20.acacia`), containerized as `t4-stripe-listen` on docker network `ecomfinal`. Command:
  ```bash
  stripe listen --skip-update --all-snapshot --forward-to http://api:8000/api/v1/orders/webhooks/stripe
  ```
- **Signing secret verification:** SHA256 of `stripe listen --print-secret` matched `STRIPE_WEBHOOK_SECRET` in `.env`.
- **Test tooling:** pytest 8.4.2, httpx 0.28.1 running in Python 3.13 without application imports.
- **Test payment instruments:** `pm_card_visa` (successful charge), `pm_card_visa_chargeDeclined` (generic decline), `pm_card_visa_chargeDeclinedInsufficientFunds` (insufficient funds), `pm_card_threeDSecure2Required` (3DS challenge), `pm_card_createDispute` (chargeback).

## Method and Commands

Two consecutive independent test runs (`run1` and `run2`) were executed with unique entity identifiers:

```bash
# Terminal 1: Live webhook forwarder
<work dir>/listener.sh start

# Terminal 2: Test runner execution
<work dir>/run.sh run1
<work dir>/run.sh run2

# Reconciliation and log capture
<work dir>/collect.sh run1
<work dir>/collect.sh run2
```

## Results

Both runs yielded identical results: 20 passed, 1 xfailed (known bug F-1), 0 failed. Zero 5xx errors occurred in either run.

| Scenario | Scope and Operations Verified | Outcome |
|---|---|---|
| a | Catalog: pagination, filtering, sorting, search, category tree, ETag, 304 If-None-Match, HEAD requests | PASS |
| b | Auth lifecycle: registration (202), Mailpit verification email, login, refresh rotation, revocation on reuse, logout | PASS |
| c | Password reset: reset request, email token validation, password update, invalidation of active sessions | PASS |
| d | Guest cart: anonymous cookie (`__Host-` prefix) merged into customer cart upon login, cookie cleared | PASS |
| e | Address book: 201 Location, GET by ID, merge-patch, delete returning 204, repeat delete returning 404 | PASS |
| f | Order and payment: 201 Location, Idempotency-Key validation, `pm_card_visa` payment, stock decrement, Celery webhook processing, order status email | PASS |
| g | Declined payments: `chargeDeclined` and `chargeDeclinedInsufficientFunds` leave order unpaid; subsequent payment succeeds | PASS |
| h | 3DS challenge: `requires_action` redirect URL; order remains payable after PaymentIntent cancellation | PASS |
| i | Refunds: partial refund, remainder refund, over-refund rejection with 400 | PASS |
| j | Disputes: `pm_card_createDispute` places order on hold (409 on status change/refund); closed lost dispute triggers refund | PASS |
| k | Stripe Checkout: checkout session creation; expiration handling; unlinked events safely ignored | PASS |
| l | Admin operations: concurrency check via If-Match (412 on mismatch), variant management, webhook replay, reconciliation | PASS |
| m | GDPR data export: email export payload; deletion request revokes tokens and prevents subsequent login | PASS |
| n | Security boundaries: 401 unauthenticated, 403 unauthorized, BOLA resource isolation, 413 body size, 415 media type, 429 rate limit | PASS |

Webhook Delivery and Processing Metrics:
- Total webhooks delivered via Stripe CLI: 209 events.
- HTTP status: 209 answered with 200 OK (0 non-200).
- Celery processing: 209 processed successfully (0 failed).
- Reconciliation: 0 discrepancies between application database and Stripe API; 0 payments flagged for manual review.
- Application error logs: 0 ERROR entries in API and Celery worker logs.

## Findings and Remediations

- **F-1 (Medium Severity / App Bug): Product detail view displays stale stock.**
  - *Cause:* `GET /api/v1/products/{id}` was cached in Redis for 1 hour. Stock decrements from completed orders and stock increments from cancellations did not invalidate this cache key. After 3 completed orders, catalog list showed stock 997, while the product detail endpoint still showed 1000 with an outdated ETag.
  - *Reproducer:* Place an order for a product and query `GET /api/v1/products/{id}`.
  - *Remediation:* Explicitly invalidate the product detail cache key upon database commit across all write paths (order creation, order cancellation, and inventory expiry tasks). Reduced default product detail TTL to 5 minutes as a fallback. Shipped in v4.0.1 (fix X5).
- **F-2 (Medium Severity / Infrastructure): Caddy reverse proxy panics on large HTTP/2 payloads.**
  - *Cause:* When a client transmits a request body exceeding 1 MiB (`MAX_REQUEST_BODY_BYTES`) over HTTP/2, Uvicorn responds with 413 and immediately closes the connection. Caddy 2.11.6 encountered a nil pointer panic in its HTTP/2 `responseWriter.SetReadDeadline` handler, causing container restarts.
  - *Remediation:* Recommended configuring `request_body max_size 1MB` at the reverse proxy layer to reject oversized payloads at the network boundary.
- **N-3 (Medium Severity / Business Logic): Unpaid orders could be transitioned to shipped status.**
  - *Cause:* Admin order status transitions (NEW -> CONFIRMED -> PROCESSING -> SENT -> DELIVERED) did not check `payment_status`. An admin could dispatch unpaid orders.
  - *Reproducer:* `PUT /api/v1/admin/orders/{id}/status` with `status: "confirmed"` on an unpaid order returned 200.
  - *Remediation:* Added guard requiring `payment_status == paid` for transitions to confirmed, processing, sent, or delivered; returns 409 Conflict otherwise. Shipped in v4.0.1 (fix X5).
- **N-1 (Low Severity / Observation): GDPR data anonymization timing.**
  - *Cause:* GDPR erasure immediately marks accounts inactive, but personal data anonymization occurs after a 24-hour grace period via Celery beat (`DATA_DELETION_GRACE_PERIOD_DAYS`). Immediate verification within the test run confirmed account deactivation; anonymization logic was verified via unit tests.

## Limitations

- Hosted Checkout and interactive 3DS challenge web pages cannot be completed headless without browser interaction; tests validated redirect generation, webhook processing, and CLI cancellation simulation.
- Replay testing utilized a direct database helper (`markfailed.sh`) to mark a processed webhook as failed in order to test the admin replay endpoint.

---

See also: [fixes-4.0.x.md](fixes-4.0.x.md), [final-test-campaign.md](final-test-campaign.md), [T8-chaos.md](T8-chaos.md), [../ARCHITECTURE.md](../ARCHITECTURE.md).
