# Stage T2: Staging Test Polygon and Baseline Verification

## Purpose and Exit Criterion

The purpose of Stage T2 was to stand up an isolated staging polygon matching production configuration constraints, terminate TLS via reverse proxy, wire live dependencies, and execute a comprehensive 24-check baseline smoke test before commencing automated testing phases.

Exit criteria:
- Complete compose stack boots with `ENVIRONMENT=staging` and passes `check_deployment_settings` without warnings or bypasses.
- Caddy reverse proxy terminates TLS on localhost and forwards client IP addresses correctly.
- Background worker and beat scheduler register tasks and active schedules.
- Baseline smoke test suite executes with 24 of 24 checks passing (0 failures).

## Environment

- **Host Machine:** WSL2 Ubuntu on a multi-core workstation (4 CPU cores and 8 GB RAM allocated to the VM).
- **Target Repository State:** Git clone checked out at tag `v4.0.0` (commit `3af3e688`).
- **Configuration:** `ENVIRONMENT=staging` (enforcing strict production deployment checks via `app/core/config.py`).
- **Components and Container Images:**
  - `api`: FastAPI application running under Python 3.13.16 and Uvicorn with 4 workers, `MAX_IN_FLIGHT_REQUESTS=15`, internal port 8000 (not exposed to host).
  - `caddy`: `caddy:2.11.6-alpine` (digest `sha256:d44355d3...`), terminating TLS on host port `127.0.0.1:8443` via an internal certificate authority (`caddy-root.crt`).
  - `db`: `postgres:15-alpine` (PostgreSQL 15.19, digest `sha256:f7d23353...`), accessible on `127.0.0.1:15432`. Roles: `ecom_owner` (DDL owner) and `ecommerce_app` (DML only, unprivileged runtime user).
  - `redis`: `redis:7-alpine` (Redis 7.4.11, digest `sha256:858f009f...`), accessible on `127.0.0.1:16379` with `REDIS_PASSWORD` enforced.
  - `celery-worker`: Celery 5.6.3 executing background processing tasks.
  - `celery-beat`: Celery beat scheduler managing recurring jobs.
  - `mailpit`: `axllent/mailpit:v1.31.4` catching outgoing SMTP messages on internal port 1025 and providing REST search API on `127.0.0.1:8025`.
- **Network and Proxy Security:** `TRUSTED_PROXIES=<proxy-ip>` (fixed IP of Caddy container). Caddy replaces `X-Forwarded-For`; spoofed client headers do not manipulate rate limit partition keys.

## Method and Commands

All operations were executed via generic orchestration scripts within the environment:

1. **Stack Initialization and Health Verification:**
   ```bash
   <work dir>/stack.sh up
   ```
2. **Export Caddy Local Root Certificate for Test Clients:**
   ```bash
   <work dir>/stack.sh ca
   ```
3. **Admin User Bootstrap via Direct Database Role Promotion:**
   ```bash
   <work dir>/stack.sh make-admin
   # Executes: UPDATE users SET role='admin', is_email_verified=true WHERE email='...';
   ```
4. **Baseline Smoke Suite Execution:**
   ```bash
   python3 <work dir>/smoke.py <work dir>
   ```

## Results

The smoke verification suite executed 24 distinct probes against the running staging stack:

| Check # | Probe Description | Target Endpoint | Expected Status | Result |
|---|---|---|---|---|
| 1 | Liveness probe | GET `/livez` | 200 OK (`{"status":"ok"}`) | PASS |
| 2 | Readiness probe | GET `/readyz` | 200 OK (DB and Redis status "ok") | PASS |
| 3 | Health status | GET `/health` | 200 OK (all services healthy) | PASS |
| 4 | Product catalog list | GET `/api/v1/products` | 200 OK (page object format) | PASS |
| 5 | RFC 9457 Problem Details | GET `/nonexistent-route` | 404 Problem (`application/problem+json`) | PASS |
| 6 | Security header: MIME sniffing | GET `/api/v1/products` | `X-Content-Type-Options: nosniff` | PASS |
| 7 | Security header: Clickjacking | GET `/api/v1/products` | `X-Frame-Options: DENY` | PASS |
| 8 | Security header: CSP | GET `/api/v1/products` | `Content-Security-Policy: default-src 'none'` | PASS |
| 9 | Request tracing header | GET `/api/v1/products` | `X-Request-ID` returned in response | PASS |
| 10 | Customer registration | POST `/api/v1/users/register` | 202 Accepted | PASS |
| 11 | Outgoing email interception | GET `mailpit:8025/api/v1/messages` | Verification email captured in Mailpit | PASS |
| 12 | Email verification | POST `/api/v1/users/verify-email` | 200 OK (valid token from email) | PASS |
| 13 | Customer login | POST `/api/v1/users/login` | 200 OK (access and refresh tokens) | PASS |
| 14 | Authenticated profile retrieval | GET `/api/v1/users/me` | 200 OK (role: client, verified: true) | PASS |
| 15 | Profile update | PATCH `/api/v1/users/me` | 200 OK (merge-patch semantics) | PASS |
| 16 | Admin user creation | Database SQL update | Role updated to 'admin' in database | PASS |
| 17 | Admin login | POST `/api/v1/users/login` | 200 OK | PASS |
| 18 | Admin profile retrieval | GET `/api/v1/users/me` | 200 OK (role: admin) | PASS |
| 19 | Admin dashboard stats | GET `/api/v1/admin/stats` | 200 OK (monetary values as strings) | PASS |
| 20 | Celery worker task execution | Background task dispatch | Task completed in Celery worker logs | PASS |
| 21 | Celery beat schedule | Beat log check | Recurring schedule loaded without error | PASS |
| 22 | HSTS header | GET `/api/v1/products` | `Strict-Transport-Security` header set | PASS |
| 23 | Referrer policy header | GET `/api/v1/products` | `Referrer-Policy: no-referrer` | PASS |
| 24 | Secure cookie flags | POST `/api/v1/users/login` | `Secure`, `HttpOnly`, `SameSite=Lax` | PASS |

Summary: 24 of 24 checks passed. 0 failures.

## Findings and Operational Notes

- **Finding 1 (Low Severity / Security Observation): Interactive API documentation exposed in staging.**
  - *Detail:* `/api/v1/docs`, `/api/v1/openapi.json`, and `/redoc` were publicly reachable in staging.
  - *Analysis:* FastAPI settings disable interactive documentation strictly when `ENVIRONMENT == "production"`. In `staging`, access is preserved by design to allow integration testing.
- **Finding 2 (Low Severity / Documentation Gap): Initial administrator bootstrap.**
  - *Detail:* The codebase did not ship a dedicated CLI command to bootstrap the first administrator user; older documentation referenced an ignored development script.
  - *Remediation:* Staging used direct SQL execution (`UPDATE users SET role='admin'`). Documented in repository setup guides.
- **Finding 3 (Info / Architectural Note): Email dispatch mechanics.**
  - *Detail:* Registration, verification, and password reset emails are dispatched via FastAPI `BackgroundTasks` within the API process, whereas order updates and periodic tasks run asynchronously via Celery.
- **Finding 4 (Info / Test Environment): Shared client IP from host.**
  - *Detail:* All test traffic originating from the host machine is routed via Caddy under a single IP (`<host-ip>`), causing all test processes to share rate limit buckets unless specific bypasses or elevated thresholds are configured.
- **Finding 5 (Low Severity / Documentation Gap): Database migration permissions.**
  - *Detail:* The `api` service runs under the unprivileged `ecommerce_app` role, which lacks DDL privileges. Executing migrations via `docker compose exec api alembic upgrade head` fails with permission errors.
  - *Remediation:* Migrations must be executed via the dedicated runner profile: `docker compose --profile migrate run --rm migrate`.

## Limitations

- The internal Caddy CA certificate (`caddy-root.crt`) is self-signed and must be injected into client trust stores (e.g. `requests`, `httpx`, `curl`).
- Testing traffic through the loopback proxy shares a single IP address bucket for IP-based rate limiting.

---

See also: [final-test-campaign.md](final-test-campaign.md), [T4-e2e.md](T4-e2e.md), [T7-load.md](T7-load.md), [../OPERATIONS.md](../OPERATIONS.md).
