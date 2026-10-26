# Stage T6: Dynamic Security Testing (OWASP ZAP & API Top 10)

An automated vulnerability scanner has the enthusiasm of an eager intern and the subtlety of a sledgehammer. Stage T6 subjects the staging application to OWASP ZAP active scanning and 231 manual security probes covering the OWASP API Security Top 10.

## Purpose and Exit Criterion

The purpose of Stage T6 was to perform automated Dynamic Application Security Testing (DAST) using OWASP ZAP and conduct an exhaustive manual verification across all ten categories of the OWASP API Security Top 10 2023.

Exit criteria:
- OWASP ZAP: Zero High and zero Medium alerts without documented and verified justification.
- OWASP API Security Top 10 2023: 100% pass across all 10 categories.
- Zero server crashes or unhandled exceptions under malicious security payloads.

## Environment

- **Target:** Staging stack running release candidate v4.0.0 (commit `0d794fc3`).
- **Scanner Image:** `zaproxy/zap-stable:20261008` (digest `sha256:7aaa659b0d...`, containing ZAP 2.17.0).
- **Scanner Network Placement:** ZAP was executed inside the Docker network targeting `http://api:8000` directly. This bypassed container-internal TLS hostname mismatches (Caddy certificate was issued only for `localhost:8443`), testing the application's native security boundaries directly. TLS and Caddy proxy headers were validated separately.
- **Exclusions:** Endpoints executing live financial transactions (`/pay`, `/checkout-session`, `/refund`) were excluded from automated fuzzer scans to avoid uncontrolled external calls to Stripe; these endpoints were audited manually.

## Method and Commands

1. **Passive Baseline Spider Scan:**
   ```bash
   zap-baseline.py -t http://api:8000/ -c <work dir>/api-scan.conf -I -a
   ```
   Evaluated public endpoints: `/`, `/api/v1/docs`, `/redoc`, `/api/v1/categories`, `/api/v1/products`, `/api/v1/legal/privacy-policy`, and `/readyz`.
2. **Active OpenAPI API Scan:**
   ```bash
   zap-api-scan.py -t http://api:8000/api/v1/openapi.json -f openapi -c <work dir>/api-scan.conf -I -a --hook <work dir>/hook_zap.py
   ```
   Executed across three distinct authentication contexts:
   - Unauthenticated (anonymous)
   - Authenticated customer (using Replacer rule injecting `Authorization: Bearer <customer_token>`)
   - Authenticated administrator (injecting `Authorization: Bearer <admin_token>`)
3. **Manual OWASP API Top 10 Probes:**
   231 targeted test probes covering access control, token manipulation, business flow logic, and injection vectors.

## Results

### OWASP ZAP Alert Audit and Justifications

The automated scan generated one High and several Medium alerts, all of which were audited and determined to be false positives or by-design behavior:

| Alert | Risk Level | Target Route | Finding Detail & Verification | Justification |
|---|---|---|---|---|
| PII Disclosure | High | `GET /api/v1/users/me` | ZAP flagged customer test telephone number `+4915112345678`. | **False Positive.** Synthetic phone number coincidentally satisfies the Luhn credit card checksum algorithm. No payment card data is stored or returned. |
| Content Security Policy | Medium | `/api/v1/docs`, `/redoc` | CSP contains `unsafe-inline`, wildcards, and lacks SRI hashes. | **Justified.** Required by third-party Swagger UI / Redoc bundles loaded from jsdelivr. In production (`ENVIRONMENT=production`), interactive docs are completely disabled. |
| Full Path Disclosure | Low | `/api/v1/products/search` | ZAP flagged Problem Details JSON body as leaking local filesystem paths. | **False Positive.** The RFC 9457 `instance` field returns the HTTP request URI path (e.g. `/api/v1/products/search`), which ZAP misinterpreted as a disk path. |
| Private IP Disclosure | Low | `/api/v1/legal/consent-status` | Response body contained private IPv4 addresses. | **By Design.** Endpoint returns the authenticated user's own consent audit log, which lawfully records their client registration IP. |
| Injection & XSS | - | All endpoints | Zero SQL injection, zero command injection, zero cross-site scripting vulnerabilities. | PASS |

### OWASP API Security Top 10 2023 Manual Audit (231 Probes)

| OWASP Category | Evaluated Vectors and Controls | Outcome |
|---|---|---|
| API1: Broken Object Level Authorization (BOLA) | Customer B probed against Customer A's orders, items, and address IDs. All requests returned 404 Not Found or 403 Forbidden. | PASS |
| API2: Broken Authentication | Forged JWT signatures, `alg=none`, weak signing keys, expired timestamps, and future `nbf` claims rejected with 401. Refresh token reuse triggers family revocation. Login and password reset return identical timing/responses for existent and non-existent emails. | PASS |
| API3: Broken Object Property Level Authorization | Mass assignment attempts sending extra fields rejected with 422 `extra_forbidden`. Sensitive fields (passwords, salts, internal IDs) never returned in response models. | PASS |
| API4: Unrestricted Resource Consumption | Pagination parameters clamped (`size` bounded). Rate limits enforce 429 with `Retry-After`. Maximum concurrent in-flight requests limited to 15. | PASS |
| API5: Broken Function Level Authorization (BFLA) | 18 admin management endpoints probed by anonymous and customer roles: anonymous received 401, customer received 403. | PASS |
| API6: Unrestricted Access to Sensitive Business Flows | Idempotency key mechanics prevent double charges. Concurrency race test on single remaining inventory unit resulted in exactly 1 × 201 order and 1 × 409 conflict, ending at 0 stock. | PASS |
| API7: Server Side Request Forgery (SSRF) | 30 malicious callback URL variations in checkout/webhook parameters rejected with 422. | PASS |
| API8: Security Misconfiguration | HSTS, CSP, and framing headers present. CORS rejects unauthorized origins. TRACE method returns 405. Error bodies contain no stack traces. Host header spoofing rejected in email generation. | PASS |
| API9: Improper Inventory Management | Legacy/unversioned paths (e.g. `/v0`, `/v2`, `/metrics`, `/admin`) return 404. Production environment completely disables Swagger UI and OpenAPI documentation routes. | PASS |
| API10: Unsafe Consumption of APIs | Stripe webhooks reject missing, forged, or stale signatures (>300 s age); timestamp at 290 s accepted; event replays handled idempotently. | PASS |

## Findings and Remediations

- **F-T6-1 (Medium Severity / App Bug): Product creation without images corrupts catalog reads.**
  - *Cause:* `POST /api/v1/admin/products` omitting the `images` field defaulted to `[]` in `ProductCreate`. Pydantic does not validate default values, so the min-length rule was bypassed and the row was saved to PostgreSQL with `images = '{}'`. However, `ProductInDB` response serialization required at least one image, raising an unhandled 500 error after committing the transaction. Subsequent public catalog queries filtering by category or searching by name crashed with 500.
  - *Reproducer:* Admin creates product without `images`: returns 500 and saves row. Public `GET /api/v1/products?category_id=<id>` returns 500 for all users.
  - *Remediation:* Made `images` required in `ProductCreate` (rejects missing field or `[]` with 422). Added flush-before-commit to build response before persisting. Moved `min_length` constraints from response models to request models so database rows with empty images return 200 without crashing. Shipped in v4.0.1 (fixes X7, X6).
- **F-T6-2 (Low Severity): Connection reset on 2 MiB request body via Caddy.**
  - *Cause:* Transmitting a 2 MiB body through Caddy returned 502 Bad Gateway in ~30% of attempts because Uvicorn issued an early 413 and closed the connection while Caddy was still writing.
  - *Remediation:* Documented reverse proxy configuration to enforce body size limits at the edge.
- **F-T6-3 (Info / Security Enhancement): Tokens without `jti` claim accepted.**
  - *Cause:* Access tokens lacking a `jti` (JWT ID) claim were validated if signed with the correct secret, but could not be individually revoked.
  - *Remediation:* Enforced mandatory `jti` claim in all access tokens (missing `jti` returns 401). Shipped in v4.0.1 (fix X7).

## Limitations

- ZAP active scanning targeted internal HTTP port 8000 to avoid container TLS certificate trust complexities; reverse proxy TLS configuration was validated via independent manual handshakes.

---

See also: [fixes-4.0.x.md](fixes-4.0.x.md), [final-test-campaign.md](final-test-campaign.md), [T1-supply-chain.md](T1-supply-chain.md), [../../SECURITY.md](../../SECURITY.md).
