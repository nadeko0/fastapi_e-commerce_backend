# Security Review and Vulnerability Remediation Record: Pre-3.0 Audit

This document records the sanitized findings, threat analysis, and remediation history of the comprehensive security review conducted prior to release v3.0.0 on 2026-10-16. The review combined manual white-box code auditing, black-box HTTP probing, and container infrastructure inspections covering the OWASP Top 10 (2021).

Every finding was assigned an identifier (F-01 through F-43) and remediated with an accompanying automated regression test written prior to code implementation.

---

## Audit Methodology and Security Posture

The assessment evaluated the application across three complementary modes:
- **White-box Source Audit:** Comprehensive review of application routing, dependency injection, authentication routines, Celery background tasks, and Alembic database migrations.
- **Black-box HTTP Probing:** Evaluated against dedicated local runtime instances with and without rate limiting, exercising multi-tenant isolation, IDOR boundaries, and race conditions.
- **Container and Environment Hardening:** Audited Docker configurations, unprivileged user execution, container network isolation, and dependency supply chains.

Areas verified as secure during the review with zero exploitable defects:
- **Object-Level Authorization (IDOR):** Order, address, and payment queries consistently filter by `user_id == current_user.id`. Administrative operations are strictly guarded by role dependencies.
- **Cart Isolation:** Session ownership is anchored in JWT claims or server-generated 256-bit cryptographically secure identifiers stored in `__Host-` prefixed cookies with `HttpOnly`, `SameSite=Lax`, and `Secure` attributes.
- **Timing Attacks on Authentication:** User lookup uses constant-time dummy password hashing for non-existent accounts, preventing timing-based email enumeration.
- **Stripe Webhook Verification:** Webhook signatures enforce HMAC-SHA256 verification via constant-time comparison with a configurable replay tolerance window.

---

## Consolidated Findings and Remediation Index

| ID | Severity | OWASP Category | Finding Description | Remediated In | Remediation Summary |
|---|---|---|---|---|---|
| **F-01** | High | A04 Insecure Design | Payment currency chosen by client in request body rather than enforced from order | Fixed in 3.0.0 | Added `currency` column to `Order`; payments enforce order currency; webhooks verify matching amount and currency |
| **F-02** | High | A07 Auth Failures | Password-reset tokens remained valid for 24 hours and allowed repeated usage | Fixed in 3.0.0 | Single-use reset tokens validated against current password hash fingerprint; lifetime reduced to 60 minutes |
| **F-03** | High | A05 Misconfiguration | Mock payment provider and public webhook secret permitted in production | Fixed in 3.0.0 | Startup validation requires `PAYMENT_PROVIDER=stripe` and rejects placeholder secrets outside dev/test |
| **F-04** | Medium | A04 Insecure Design | Late payment failure webhooks could transition a paid order back to failed | Fixed in 3.0.0 | Webhook handler ignores failure events if the order has already achieved `PAID` status |
| **F-05** | Medium | A01 / A07 | Account deletion via legal data request permitted without password re-authentication | Fixed in 3.0.0 | Account deletion requires `current_password` (returning 422 if omitted, 401 if invalid) |
| **F-06** | Medium | A07 Auth Failures | Failed-login rate limiter failed open when Redis was unreachable | Fixed in 3.0.0 | Login fails closed, returning 503 with `Retry-After` if Redis is unavailable during authentication |
| **F-07** | Medium | A04 Privacy | GDPR account anonymization retained sensitive address and consent log entries | Fixed in 3.0.0 | Anonymization scrubs all customer addresses, consent IP logs, and data export request history |
| **F-08** | Medium | A02 / A05 | Missing startup validation for `JWT_SECRET_KEY` length and default placeholders | Fixed in 3.0.0 | Enforced startup validator ensuring secrets meet minimum length (>= 32 chars) and reject default values |
| **F-09** | Medium | A04 Insecure Design | Unpaid orders locked product inventory indefinitely | Fixed in 3.0.0 | Added Celery `expire_unpaid_orders` task to restock unpaid orders older than `UNPAID_ORDER_EXPIRE_MINUTES` |
| **F-10** | Medium | A05 Misconfiguration | Database and cache ports exposed on 0.0.0.0; app executed as Postgres superuser | Fixed in 3.0.0 | Ports bound to 127.0.0.1; application runs as unprivileged role `APP_DB_USER` on fresh volumes |
| **F-11** | Medium | A07 / A04 | Inconsistent error handling during Redis token revocation | Fixed in 3.0.0 | Revocation check fails closed on cache errors (refined to return 503 instead of 401 in v4.0.1) |
| **F-12** | Low | A01 Access Control | Administrative product variant list accessible without authentication | Fixed in 3.0.0 | Added `get_current_admin_user` dependency to administrative variant endpoints |
| **F-13** | Low | A07 Auth Failures | Email uniqueness was case-sensitive, permitting duplicate registered accounts | Fixed in 3.0.0 | Added migration creating unique index on `lower(email)`; email lookups normalized to lowercase |
| **F-14** | Low | A07 Auth Failures | Registration returned 409 Conflict on existing email, permitting user enumeration | Fixed in 3.0.0 | Registration returns uniform 202 Accepted; existing account holders receive notification email |
| **F-15** | Low | A07 Auth Failures | Logout without refresh token invalidated access token but left session active | Fixed in 3.0.0 | Logout without refresh token updates user password/revocation watermark, invalidating all issued tokens |
| **F-16** | Low | A09 Logging | Sensitive customer details and authentication credentials logged in plain text | Fixed in 3.0.0 | Log formatters sanitize authorization headers, passwords, and personally identifiable information |
| **F-17** | Low | A05 Misconfiguration | Health check endpoint revealed application version and deployment environment | Fixed in 3.0.0 | Removed version and environment strings from `/health`; returns only operational status |
| **F-18** | Low | A05 Misconfiguration | Swagger and OpenAPI documentation interfaces exposed in production | Fixed in 3.0.0 | Disabled interactive OpenAPI and Swagger endpoints when `ENVIRONMENT=production` |
| **F-19** | Low | A04 / A05 | Product search queries lacked length limits and wildcard character escaping | Fixed in 3.0.0 | Capped search input at 100 characters; escaped SQL wildcard characters (`%`, `_`) for literal matching |
| **F-20** | Low | A04 Insecure Design | Unbounded guest cart creation risked cache memory exhaustion | Fixed in 3.0.0 | Added IP-based guest cart rate limits; configured Redis memory bounds with `volatile-lru` eviction |
| **F-21** | Low | A04 / A08 | Missing database-level CHECK constraints on prices and inventory quantities | Fixed in 3.0.0 | Added Alembic migrations enforcing CHECK constraints (`price >= 0`, `stock >= 0`, `quantity > 0`) |
| **F-22** | Low | A01 Access Control | Arbitrary `success_url` and `cancel_url` in Checkout Session allowed open redirect | Fixed in 3.0.0 | Enforced validation requiring redirect URLs to match configured `FRONTEND_URL` |
| **F-23** | Low | A02 Cryptographic | Raw JWT strings stored directly as Redis cache keys | Fixed in 3.0.0 | Redis token blacklist keyed by JWT `jti` claim or cryptographic SHA-256 hash |
| **F-24** | Low | A02 / A07 | Verification links defaulted to localhost; reset-token validation echoed email | Fixed in 3.0.0 | Links generated from `FRONTEND_URL`; token validation endpoint confirms validity without echoing email |
| **F-25** | Info | A04 Insecure Design | Admin order listing accepted arbitrary model attributes for sorting | Fixed in 3.0.0 | Added strict allowlist for `sort_by` query parameter, returning 422 for unauthorized attributes |
| **F-26** | Info | A08 Data Integrity | Unused `validate_token` utility lacked explicit token type validation | Fixed in 3.0.0 | Removed unused helper function from codebase |
| **F-27** | Info | A06 Vulnerable Comp | `python-jose` dependency was unmaintained and pulled vulnerable dependencies | Fixed in 2.0.0 | Replaced `python-jose` with actively maintained `PyJWT` |
| **F-28** | Info | A05 Misconfiguration | Trust in `X-Forwarded-For` header varied depending on uvicorn startup arguments | Fixed in 3.0.0 | Client IP extraction standardized through custom middleware honoring `TRUSTED_PROXIES` |
| **F-29** | Medium | A05 Misconfiguration | Container stack failed to start when `REDIS_PASSWORD` was empty | Fixed in 3.0.0 | Added compose parameter validations requiring defined Redis password credentials |
| **F-30** | Low | A05 Misconfiguration | Redis container health check used `redis-cli ping` without authentication | Fixed in 3.0.0 | Updated container health check command to pass required AUTH credentials |
| **F-31** | Low | A06 Vulnerable Comp | Container base images were referenced by mutable tags rather than immutable digests | Fixed in 3.0.0 | Pinned all base image references in `Dockerfile` and `docker-compose.yml` to immutable sha256 digests |
| **F-32** | Low | A02 Cryptographic | Declared `DATA_ENCRYPTION_KEY` configuration setting was unused | Fixed in 3.0.0 | Removed obsolete setting to prevent misleading security assumptions |
| **F-33** | Low | A05 / A07 | Database configured with default superuser credentials | Fixed in 3.0.0 | Enforced dedicated non-superuser database role for runtime application access |
| **F-34** | Info | A09 Logging | Development SMTP credentials bundled in configuration template | Fixed in 3.0.0 | Replaced active credentials with generic placeholders in `.env.example` |
| **F-35** | Low | A04 Insecure Design | Order payment execution lacked row locking, permitting concurrent payment race | Fixed in 3.0.0 | Implemented pessimistic row-level locking (`SELECT ... FOR UPDATE`) during order payment workflows |
| **F-36** | Low | A03 Injection | Product names rendered unescaped in outgoing HTML order emails (Stored XSS) | Fixed in 2.0.0 | Applied HTML entity escaping to product titles and user-supplied fields in email templates |
| **F-37** | Medium | A04 Privacy | Periodic GDPR cleanup repeatedly re-processed previously anonymized records | Fixed in 3.0.0 | Updated cleanup query to filter for unanonymized records and process in bounded transactions |
| **F-38** | Medium | A07 Auth Failures | Global account lockout per email allowed denial-of-service against target accounts | Fixed in 3.0.0 | Split lockouts into strict per-email-and-IP limit and looser global per-email backstop |
| **F-39** | Medium | A04 Insecure Design | Unauthenticated guest cart volume risked Redis memory exhaustion | Fixed in 3.0.0 | Enforced guest cart creation rate limits and isolated eviction policies |
| **F-40** | Info | Compatibility | `Idempotency-Key` header on `POST /orders` was optional | Fixed in 2.0.0 | Enforced mandatory `Idempotency-Key` header (8-255 characters) for order creation |
| **F-41** | Info | Performance | Hourly cart sweep queried keys inefficiently without yielding results | Fixed in 3.0.0 | Removed redundant background cart cleanup task; delegated expiration to Redis key TTL |
| **F-42** | Low | A04 Insecure Design | GDPR export queried all order and address records in a single unconstrained query | Fixed in 3.0.0 | Added eager loading and bounded record limits for data export operations |
| **F-43** | Medium | A06 Vulnerable Comp | Static analysis reported CVE-2026-85394 in `python-jose` and CVE-2024-23342 in `ecdsa` | Fixed in 2.0.0 | Migrated token encoding to `PyJWT`, eliminating `python-jose` and `ecdsa` dependencies |
| **FP-1** | None | Not Applicable | Static review claimed `redis_async` and `overload` modules were missing | False Positive | Confirmed modules are present, tested, and active in the codebase |
| **FP-2** | None | Not Applicable | Static review claimed rate limiting keys failed due to `/api/v1` prefix | False Positive | Confirmed rate limiting middleware normalizes route templates before key generation |

---

## Detailed Analysis of Key Vulnerability Classes

### 1. Payment Concurrency, Currency Arbitrage, and State Inversion (F-01, F-04, F-35)
- **Vulnerability Mechanism:**
  - In F-01, the `Order` model lacked a currency column. Endpoints `/pay` and `/checkout-session` inspected only the decimal amount and adopted the currency submitted in the request body. A customer could place an order totaling 100 USD and submit payment for 100 JPY.
  - In F-04, asynchronous Stripe webhook handling processed failure notifications without evaluating prior order success, allowing a delayed failed charge event to mark an already paid order as failed.
  - In F-35, payment initiation inspected order state without locking the row. Parallel requests could both observe `status = PENDING`, initiate two charges against Stripe, and create duplicate ledger entries.
- **Remediation Implemented:**
  - Added a `currency` column to the `orders` table, populated from `STORE_CURRENCY` during order creation.
  - Payment endpoints reject any request specifying a currency different from `order.currency` with 400 Bad Request.
  - Payment initiation executes within an atomic transaction acquiring a pessimistic row lock via `SELECT ... FOR UPDATE`.
  - Webhook processing in `_apply_payment_success` strictly compares received amounts and currencies against database records before updating status.
  - Webhook handlers explicitly ignore charge failure events if the order has already reached `PAID` status.

### 2. Authentication Integrity and Token Security (F-02, F-08, F-14, F-15, F-38)
- **Vulnerability Mechanism:**
  - In F-02, password-reset tokens were valid for 24 hours and were not invalidated upon use, allowing multiple resets with the same token if intercepted.
  - In F-14, user registration returned 409 Conflict when an email address existed, enabling adversaries to map registered user accounts.
  - In F-38, account lockout logic tracked failed attempts globally per email address (10 failures in 15 minutes). An attacker could deliberately lock out arbitrary victims by issuing failed login attempts from any IP address.
  - In F-15, logging out without presenting a refresh token revoked the access token in Redis but left all associated refresh tokens valid indefinitely.
- **Remediation Implemented:**
  - Password reset tokens encode a cryptographic fingerprint derived from the user's current password hash. Updating the password immediately invalidates the token against future checks. Default token lifetime was reduced to 60 minutes.
  - Registration returns a uniform 202 Accepted response for all valid email submissions. If the email is registered, the owner receives an email notification alerting them of the registration attempt.
  - Lockout protection was split into two tiers: a strict limit per email and IP address (`RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS`), and a generous global backstop per email (`RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL`).
  - Logout without a refresh token sets a user-level revocation timestamp watermark in Redis, invalidating all access and refresh tokens issued prior to the logout timestamp.

### 3. Production Deployment Hardening (F-03, F-10, F-18, F-31, F-33)
- **Vulnerability Mechanism:**
  - In F-03, the application allowed execution with `PAYMENT_PROVIDER=mock` and default placeholder webhook secrets even in production configurations.
  - In F-10 and F-33, Docker Compose exposed database and cache ports across all network interfaces (`0.0.0.0`), and the application connected to PostgreSQL with superuser privileges.
  - In F-18, OpenAPI schemas and interactive Swagger UI documentation were accessible in production.
- **Remediation Implemented:**
  - Startup validation refuses to launch the application outside development and test environments unless `PAYMENT_PROVIDER=stripe`, secrets meet minimum 32-character lengths without default placeholder substrings, and `FRONTEND_URL` uses HTTPS.
  - Database and cache container ports in `docker-compose.yml` are bound exclusively to localhost (`127.0.0.1`).
  - Runtime application access connects via an unprivileged role (`APP_DB_USER`) created on fresh volumes; schema migrations run under a dedicated administrative Docker profile.
  - Interactive documentation routes are disabled automatically when `ENVIRONMENT=production`.
  - All container base images in `Dockerfile` and `docker-compose.yml` are pinned to immutable SHA-256 digests.

### 4. Data Privacy, Injection, and GDPR Compliance (F-05, F-07, F-19, F-36, F-37)
- **Vulnerability Mechanism:**
  - In F-05, an authenticated user could submit an account deletion request via `POST /legal/data-request` without verifying their current password.
  - In F-07 and F-37, account anonymization left addresses and consent IP logs intact, and the background job repeatedly scanned previously anonymized records.
  - In F-36, product titles were rendered directly into HTML confirmation emails without character escaping, creating a stored cross-site scripting (XSS) path.
  - In F-19, product search queries executed unconstrained `ILIKE` operations with unescaped SQL wildcard characters (`%`, `_`), allowing excessive database resource consumption.
- **Remediation Implemented:**
  - Account deletion requests require the user's `current_password`, returning 422 if omitted and 401 if incorrect.
  - The GDPR anonymization job scrubs all associated physical addresses, consent IP logs, and historical data export archives, filtering strictly for records where `anonymized_at IS NULL`.
  - Product titles and user-supplied strings in email templates are HTML-entity escaped using Jinja2 autoescaping.
  - Product search strings are capped at 100 characters and wildcard characters are escaped for literal substring matching.

### 5. Supply Chain and Cryptographic Migration (F-27, F-43, F-23, F-32)
- **Vulnerability Mechanism:**
  - In F-27 and F-43, the legacy codebase relied on `python-jose`, which ceased maintenance and carried published security advisories (CVE-2026-85394 and transitive dependency `ecdsa` CVE-2024-23342).
  - In F-23, raw JWT strings were used as cache keys in Redis, needlessly storing signed user tokens in Redis memory.
  - In F-32, the configuration schema declared a `DATA_ENCRYPTION_KEY` setting that was never referenced in encryption routines, providing a false impression of field-level encryption.
- **Remediation Implemented:**
  - In release v2.0.0, `python-jose` was completely replaced with the actively maintained `PyJWT` library, removing the vulnerable `ecdsa` package from the dependency graph.
  - In v3.0.0, the Redis token blacklist and revocation trackers were refactored to index exclusively on the JWT `jti` (JWT ID) claim or its SHA-256 digest.
  - Removed `DATA_ENCRYPTION_KEY` from configuration schemas and documentation.

### 6. Reverse Proxy Trust and Client IP Resolution (F-28, F-20, F-39)
- **Vulnerability Mechanism:**
  - In F-28, relying on uvicorn's native proxy forwarding resulted in inconsistent client IP resolution depending on startup flags. If an application trusted `X-Forwarded-For` blindly, an attacker could spoof their IP to bypass per-IP rate limiters.
  - In F-20 and F-39, guest shopping carts could be created anonymously without volume bounds, allowing an attacker to exhaust Redis memory.
- **Remediation Implemented:**
  - Standardized client IP extraction into a dedicated core middleware that inspects `X-Forwarded-For` only when the immediate peer IP address matches an entry in `TRUSTED_PROXIES`.
  - Introduced explicit rate limits on guest cart creation (`RATE_LIMIT_GUEST_CART`), restricting unauthenticated cart generation per IP address.
  - Bound Redis memory allocation in Docker Compose with a `volatile-lru` eviction policy, safeguarding authentication keys and message queues against cart key growth.

---

## Residual Risks and Testing Boundaries

The pre-3.0 security audit established a robust baseline, elevating test coverage from 737 to 1796 automated tests with 98% branch coverage. However, the review identified several boundaries that were explicitly deferred to subsequent testing campaign stages:
1. **Authenticated Dynamic Fuzzing (DAST):** The review did not include contract-based fuzzing of all endpoints (subsequently executed in Stage T3 using Schemathesis).
2. **Automated Supply Chain and Secret Scanning:** Static SCA and secret scanning were manual point-in-time checks (subsequently integrated into CI in Stage T1 using Trivy and gitleaks).
3. **Multi-Worker Concurrency and Load:** Race conditions were verified on local threads; validation under distributed multi-worker traffic was deferred to Stage T7 (Load) and Stage T8 (Chaos).

---

See also: [Final Test Campaign Overview](final-test-campaign.md), [Remediated Defects across Releases 4.0.x](fixes-4.0.x.md), [Release 4.0.0 Architecture Redesign](release-4.0.0.md), [Dynamic Security Testing (DAST & OWASP)](T6-dast-owasp.md).
