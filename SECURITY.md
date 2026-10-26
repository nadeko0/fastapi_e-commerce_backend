# Security Policy and Architecture

Securing a transactional e-commerce platform demands defense in depth across authentication lifecycles, data validation, database concurrency, and dependency supply chains. Rather than trusting edge components implicitly, every internal service enforces strict boundary controls, sanitizes inputs, and fails closed when security dependencies degrade.

## Reporting a Vulnerability

If you find a vulnerability, please report it privately through GitHub's **Report a vulnerability** button on the repository's Security tab, not in a public issue or pull request. There is no security e-mail address.

A useful report includes:
- what the vulnerability is and what an attacker could do with it;
- how to reproduce it (an HTTP request or `curl` command is ideal);
- the affected endpoint, component or configuration, and the version you tested (the latest release is v4.0.3; earlier lines differ substantially).

This is a personal project, so there is no response-time commitment.

## Authentication and Token Architecture

Authentication implements JSON Web Tokens (PyJWT) with hardened cryptographic configurations:

### Token Claims and Verification

Tokens require explicit claims on decode via PyJWT `require` options. PyJWT validates expiration only when `exp` is present, so omitting claim validation would allow unsigned or stripped tokens to persist indefinitely.

| Token Type | Lifetime | Required Claims (`options={"require": ...}`) | Revocation Mechanism |
|---|---|---|---|
| Access Token | 15 minutes | `["exp", "sub", "type", "jti"]` | Blacklisted in Redis by unique `jti` upon logout. |
| Refresh Token | 7 days | `["exp", "sub"]` (`type`, `jti`, `family` checked during rotation) | Consumed atomically; family revocation on reuse. |
| Password Reset | 60 minutes | `["exp", "sub", "type", "pwd"]` | Bound to password fingerprint (`pwd`); single-use. |
| Email Verification | 24 hours | `["exp", "sub", "type"]` | Single-use; idempotent upon repeated verification. |

The signature algorithm is strictly constrained by a Pydantic literal to `HS256`, `HS384`, or `HS512` (`settings.JWT_ALGORITHM`). Algorithms such as `none` or asymmetric confusion attacks are rejected at schema validation time.

### Fail-Closed Token Revocation

When a client presents an access token, `get_current_user` checks whether its `jti` exists in the Redis blacklist and whether its issue timestamp (`iat`) precedes the user's global revocation watermark. If Redis is unreachable, token verification immediately fails closed with HTTP 503 Service Unavailable (`Retry-After: 30`) rather than returning 401 Unauthorized or accepting the token. A 401 response would indicate bad credentials and cause clients to discard their tokens, whereas 503 correctly reflects temporary infrastructure degradation.

### Password Hashing and Concurrency Controls

- **Direct Bcrypt Calls:** Passwords are hashed and verified directly with `bcrypt`, eliminating the unmaintained `passlib` shim.
- **Threadpool Offloading:** Bcrypt operations are CPU-bound and block execution for 200 to 300 ms at `BCRYPT_ROUNDS=12`. Route handlers run verification via `run_in_threadpool`, bounded by an event-loop semaphore (`settings.BCRYPT_MAX_CONCURRENCY` or `(os.cpu_count() or 1) * 4`), preventing unbounded concurrent logins from exhausting the threadpool.
- **The 72-Byte Rule:** Bcrypt considers only the first 72 bytes of an input string and raises a `ValueError` on longer inputs since bcrypt 5.0.0. Passwords longer than 72 bytes are rejected with 422 upon registration and password change. During login verification, passwords exceeding 72 bytes execute one dummy check of the first 72 bytes before returning False, ensuring constant execution time without leaking length limits.
- **Timing Attack Mitigation:** Authentication verifies against a precomputed dummy hash (`DUMMY_PASSWORD_HASH`) at full cost factor `settings.BCRYPT_ROUNDS` when an account does not exist, preventing timing-based email enumeration.

### NIST SP 800-63B-4 Password Policy

Password validation complies with NIST SP 800-63B-4 Section 3.1.1.2:
- No arbitrary composition rules (no mandatory special characters, digits, or mixed casing).
- Unicode characters are accepted; passwords undergo Unicode NFC normalization before hashing and verification.
- Passwords are checked against the SecLists 10,000 most common passwords blocklist.
- Context-specific passwords containing the user's email username or the service name are rejected with descriptive messages.

### Dual-Tier Login Lockout

Failed login attempts are counted atomically in Redis using Lua scripts:
- **Per-(Email, IP) Limit:** Configured via `settings.RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS` (default 10 per 15 minutes). An attacker guessing passwords from a single IP is locked out at that IP only, leaving the legitimate account owner unaffected from other networks.
- **Global Per-Email Backstop:** Configured via `settings.RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL` (default 100 per hour). Prevents distributed brute-force attacks across botnets.
- **Fail-Closed Boundary:** If Redis is down, login attempts return HTTP 503 (`Retry-After: 30`) to prevent unlimited password guessing during cache outages.

## Authorization and Access Boundaries

Access boundaries are enforced strictly at the database query layer:

| Resource Scope | Dependency | Enforcement Mechanism | Failure Response |
|---|---|---|---|
| Orders & Payments | `CurrentUser` | Explicit query filtering: `where(Order.user_id == current_user.id)` | 404 Not Found (or 403) |
| Addresses | `CurrentUser` | Scoped to authenticated user ID on reads and mutations | 404 Not Found |
| Admin Operations | `get_current_admin_user` | Requires authenticated user with `role == UserRole.ADMIN` | 401 (anonymous), 403 (client) |
| Active Account Gate | `get_current_active_user` | Checks `current_user.is_active` | 403 `account-inactive` |

- **Cart Session Isolation:** Guest shopping carts are anchored to server-generated 256-bit cryptographically secure identifiers stored in `__Host-` prefixed cookies with `HttpOnly`, `SameSite=Lax`, and `Secure` attributes. Upon customer login, guest cart lines are safely merged into the authenticated cart.

## Input Validation and Injection Defense

- **Forbidden Unknown Fields:** Request bodies extend `RequestBody` with Pydantic `extra="forbid"`, rejecting unexpected keys with 422 `extra_forbidden`.
- **Text Filtering:** Application-wide middleware rejects NUL bytes (`\x00`) and lone Unicode surrogates (`\ud800` through `\udfff`) with 422 across JSON bodies, query strings, URL paths, and form inputs before values reach PostgreSQL drivers or log formatters.
- **SQL Injection Defense:** All database queries utilize SQLAlchemy 2.0 parameterized expressions and ORM mapping exclusively. Raw string concatenation in queries is prohibited. Catalog search parameters escape SQL wildcard characters (`%`, `_`) to ensure literal matching.
- **Cross-Site Scripting (XSS):** The API serves raw JSON data with proper MIME types. Outbound transactional HTML emails escape product titles and customer names using `html.escape` to eliminate stored email XSS vectors.
- **Monetary Precision:** Prices and order totals are represented as exact decimal strings (never floating-point numbers) with ISO 4217 currency checks. Datetimes enforce explicit UTC timezone offsets (`UTCDatetime`).

## HTTP Security Headers and CORS Policy

The ASGI middleware stack injects OWASP Secure Headers on every response (including error envelopes):

| Header | Configured Value | Purpose and Scope |
|---|---|---|
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` | Enforces browser TLS encryption for 1 year. |
| `X-Content-Type-Options` | `nosniff` | Prevents browser MIME-type sniffing. |
| `X-Frame-Options` | `DENY` | Prevents clickjacking and framing attacks. |
| `Referrer-Policy` | `no-referrer` | Eliminates referrer leakage across outbound links. |
| `Cross-Origin-Resource-Policy` | `same-origin` | Blocks cross-origin no-cors resource embedding. |
| `Cross-Origin-Opener-Policy` | `same-origin` | Isolates browsing context from external tabs. |
| `Permissions-Policy` | `geolocation=(), microphone=()` | Disables unneeded client-side hardware APIs. |
| `Cache-Control` | `no-store` | Enforced on `/users`, `/orders`, `/cart`, `/admin`, and authenticated calls. |

- **Content Security Policy (CSP):** API endpoints enforce `default-src 'none'; frame-ancestors 'none'`. Interactive documentation routes (`/api/v1/docs`, `/redoc`) apply a CDN-scoped policy outside production; documentation endpoints are disabled entirely in production (`ENVIRONMENT=production`).
- **Deprecated Headers Omitted:** `X-XSS-Protection` is deliberately omitted; modern browsers deprecate it, and its heuristic audit mode can introduce client-side security vulnerabilities.
- **CORS Policy:** Origins are strictly restricted to `settings.BACKEND_CORS_ORIGINS`. Because credentials are enabled (`allow_credentials=True`), wildcard `*` origins are rejected by CORS middleware.

## Concurrency, Rate Limiting, and Overload Protection

- **Request Body Limits:** `BodySizeLimitMiddleware` terminates requests exceeding `settings.MAX_REQUEST_BODY_BYTES` (default 1 MiB) with HTTP 413 `body-too-large`.
- **In-Flight Concurrency Caps:** `InFlightLimitMiddleware` caps active requests per Uvicorn worker to `settings.MAX_IN_FLIGHT_REQUESTS` (configured to 15 under benchmarks), shedding excess traffic with HTTP 503 `service-overloaded` (`Retry-After: 1`).
- **Safe Worker Capacity Formula:** The safe in-flight ceiling is derived as `(DB_POOL_SIZE + DB_MAX_OVERFLOW) / 2` because an authenticated request can briefly checkout two database connections simultaneously during dependency resolution and route execution.
- **Rate Limits:** Evaluated via atomic Redis Lua scripts per route template and caller. Keyed by authenticated user ID, or client IP accounting for `TRUSTED_PROXIES` (evaluated right-to-left) and `/64` IPv6 subnet grouping. Responses carry draft IETF `RateLimit` and `RateLimit-Policy` headers.
- **Fail-Open vs. Fail-Closed Boundaries:** General rate limiting fails open on Redis outages to maintain store availability; authentication and failed-login limits fail closed (503) to prevent brute-force attacks during cache degradation.

## Secrets Handling and Deployment Configuration

- **Secret Handling:** All credentials (`JWT_SECRET_KEY`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `SMTP_PASSWORD`) are typed as Pydantic `SecretStr` to prevent leakage in debug logs or exception traces.
- **Deployment Guards:** In staging and production environments, startup validators verify that secrets meet minimum byte lengths (JWT secret >= 32/48/64 bytes, webhook secret >= 32 bytes, database passwords >= 16 bytes), reject placeholder defaults, require `PAYMENT_PROVIDER=stripe`, enforce HTTPS for `FRONTEND_URL`, and fail startup if `LOG_LEVEL=DEBUG`.

## GDPR Privacy Controls

- **Article 17 Erasure and Anonymization:** Deletion requests via `POST /api/v1/legal/data-request` require verification of `current_password`. The service anonymizes personal details, purges physical addresses, scrubs IP logs, and revokes all active tokens, while retaining immutable financial transaction rows required by statutory tax laws.
- **Article 15 and 20 Portability:** Bounded, eager-loaded queries export user account records, addresses, order histories, and consent audits in JSON format.
- **Consent Tracking:** Granular user consent state is recorded and audited across terms of service, privacy policies, and optional marketing preferences.

## Supply Chain Security and CI Gates

- **Lockfile Enforcement:** Dependencies are managed via `uv.lock` with deterministic package hashes across production and dev dependency groups.
- **Automated Dependency Audits:** Dependabot scans weekly for minor and patch updates across GitHub Actions, Python packages, Dockerfiles, and compose files.
- **CI Vulnerability Scanning:** Every commit triggers `uvx pip-audit==2.10.1 --require-hashes` against PyPI and OSV databases, and generates a CycloneDX 1.5 Software Bill of Materials (SBOM).
- **Secret Scanning:** `gitleaks v8.30.1` scans the full Git history on every push using `.gitleaks.toml` rules.
- **Container Vulnerability Gating (Trivy):** The CI pipeline scans application and compose images with Trivy v0.75.0, gating builds on zero fixable CRITICAL or HIGH vulnerabilities.
- **The Gosu Decision:** Official `postgres:15-alpine` images bundle `/usr/local/bin/gosu` built with Go 1.24.6, triggering 26 standard-library CVE alerts. Binary call-graph inspection with `govulncheck v1.9.0` confirmed zero vulnerable symbols are reachable (gosu only drops root privileges at startup). Waivers were documented in `.trivyignore.yaml` strictly scoped to that binary path.
- **The Two-Week Age Rule:** External tools, base images, and GitHub Actions must be older than two weeks when pinned by immutable digests to protect against supply-chain poisoning. A documented exemption process allows immediate pinning for critical security patches, which was utilized to pin `redis:7.4.11-alpine` for CVE-2026-84782.
- **SHA Pinning:** All GitHub Actions workflows pin actions by full 40-character commit SHAs.

## Verification and Security Testing Results

Security controls were validated across independent testing stages prior to release:

| Verification Mode | Scope and Methodology | Key Findings and Remediations | Status |
|---|---|---|---|
| **Pre-3.0 Security Review** | White-box code audit and black-box probing across OWASP Top 10 (2021). | Remediated 43 specific findings (F-01 to F-43), including payment row locking (`SELECT FOR UPDATE`), case-insensitive email indexes, and single-use reset tokens. | PASS |
| **Stage T1: Supply Chain** | Gitleaks commit history audit, pip-audit dependency graphs, and Trivy container inspection. | Uninstalled bundled pip from Python base image; validated gosu reachability with govulncheck; granted security exemption for Redis CVE-2026-84782. | PASS |
| **Stage T3: Contract Fuzzing** | Schemathesis 4.30.1 property-based fuzzing across 54 paths (30,000+ examples). | Identified PostgreSQL crashes on NUL bytes (`\x00`) and surrogate Unicode tokens; implemented unified text sanitization middleware in v4.0.1. | PASS |
| **Stage T6: DAST & OWASP Top 10** | OWASP ZAP 2.17.0 passive/active scans and 231 manual OWASP API Top 10 (2023) probes. | Fixed empty images validation in product creation; enforced mandatory access token `jti` claims; 100% pass across all 10 API security categories. | PASS |

### OWASP API Security Top 10 (2023) Coverage

The 231 manual test probes in Stage T6 confirmed complete compliance across the 10 API security categories:
1. **API1 (BOLA):** Cross-tenant resource access rejected with 404 or 403.
2. **API2 (Broken Authentication):** Forged JWT signatures, `alg=none`, expired timestamps, and reused refresh tokens rejected.
3. **API3 (Property Level Authorization):** Mass assignment rejected via `extra="forbid"`; sensitive fields omitted from response models.
4. **API4 (Resource Consumption):** Pagination bounded (`size <= 100`), in-flight requests capped, and atomic rate limits active.
5. **API5 (BFLA):** Administrative management endpoints return 401 for anonymous callers and 403 for customer roles.
6. **API6 (Business Flows):** Concurrency tests on inventory races prevent overselling; idempotency keys prevent duplicate payments.
7. **API7 (SSRF):** Malicious callback URL variants in checkout and webhook parameters rejected with 422.
8. **API8 (Security Misconfiguration):** HSTS, framing, and CSP headers present; CORS strictly enforced; TRACE method disabled.
9. **API9 (Improper Inventory Management):** Legacy API paths return 404; production environment disables OpenAPI/docs endpoints.
10. **API10 (Unsafe Consumption of APIs):** Stripe webhooks verify HMAC signatures and reject timestamps older than 300 seconds.

See also: [docs/API.md](docs/API.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/RESILIENCE.md](docs/RESILIENCE.md), [docs/reports/T1-supply-chain.md](docs/reports/T1-supply-chain.md), [docs/reports/T6-dast-owasp.md](docs/reports/T6-dast-owasp.md), [docs/reports/security-review-3.0.md](docs/reports/security-review-3.0.md).
