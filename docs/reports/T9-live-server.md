# Stage T9: Live Server Verification

Local benchmarks are comfortable; deploying onto a real server sharing memory with live production workloads requires precision and nerves of steel. Stage T9 validates container resource limits, loopback SSH tunnels, and clean teardown on a shared Linux VPS without disturbing neighboring services.

## Purpose and Exit Criterion

The purpose of Stage T9 was to deploy release candidate v4.0.1 on a live Linux host running real background workloads, execute smoke, E2E, and load test suites through an encrypted network tunnel, and verify strict resource isolation and complete clean teardown without impacting co-located services.

Exit criteria:
- Successful deployment in isolated containers with hard per-container memory and CPU limits.
- Zero open public ports: all communication conducted through loopback interfaces over an encrypted SSH tunnel.
- Baseline smoke suite: 22 of 22 checks passing.
- Black-box E2E suite: 100% pass rate on applicable headless scenarios (zero regressions).
- Remote load testing: zero 5xx server errors; rate limits enforce expected 429 responses.
- Safety threshold: minimum available host memory remains strictly above 700 MB at all times.
- Host integrity: neighbouring unrelated services (several containers and systemd units) operate continuously without restarts or resource starvation.
- 100% clean teardown: all test containers, networks, volumes, and temporary directories completely removed.

## Environment

- **Server Hardware:** A shared VPS (2 vCPU, 3.7 GB RAM, no swap) running unrelated production services.
- **Co-located Workloads:** Unrelated production services comprising several active Docker containers and systemd services.
- **Safety Watchdog:** Background monitor polling memory every 5 seconds (2113 samples recorded across 3 hours), configured with an emergency killswitch if free RAM dropped below 700 MB or if host services were interrupted.
- **Application Stack and Hard Resource Constraints:**
  - Container image: `ecom:4.0.1` (built locally and transferred via `docker save | docker load` to avoid consuming CPU on the shared host).
  - Deployed via an isolated compose project with explicit memory and CPU limits:
    - `api`: Memory 512 MB, MemSwap 512 MB, CPU limit 1.0
    - `db`: Memory 384 MB, MemSwap 384 MB, CPU limit 0.75 (`postgres:15-alpine`)
    - `redis`: Memory 96 MB, MemSwap 96 MB, CPU limit 0.25 (`redis:7-alpine`)
    - `celery-worker`: Memory 320 MB, MemSwap 320 MB, CPU limit 0.5
    - `celery-beat`: Memory 128 MB, MemSwap 128 MB, CPU limit 0.25
    - `mailpit`: Memory 64 MB, MemSwap 64 MB, CPU limit 0.1 (`axllent/mailpit:v1.31.4`)
  - Total hard memory limit: 1504 MB. Peak observed stack consumption: ~600 MB.
- **Configuration:** `ENVIRONMENT=test`, `PAYMENT_PROVIDER=mock`. Services bound strictly to loopback (`127.0.0.1`), accessible only via local SSH tunnel from the test client workstation. Host database and Redis ports were completely untouched.

## Method and Commands

1. **Local Image Preparation and Server Ingestion:**
   ```bash
   docker save ecom:4.0.1 | ssh <host> docker load
   ```
2. **Encrypted Tunnel Establishment:**
   ```bash
   ssh -N -L 18000:127.0.0.1:18000 -L 18025:127.0.0.1:18025 <host>
   ```
3. **Round-Trip Time (RTT) Measurement:**
   20 sequential HTTP GET requests to `/livez` over the tunnel.
4. **Smoke and E2E Suite Execution:**
   Executed remotely through the tunnel using `http://127.0.0.1:18000`.
5. **Remote Load Testing:**
   Locust running on the test client workstation sending 50 and 100 concurrent virtual users through the tunnel.
6. **Teardown and Host Verification:**
   ```bash
   docker compose -p ecom-test down -v
   ```

## Results

### Verification Summary Table

| Test Phase | Scope / Metric | Measured Value | Outcome |
|---|---|---|---|
| Tunnel Latency | 20 sequential `/livez` probes | min: 42.56 ms, median: 48.72 ms, max: 109.52 ms | PASS |
| Smoke Suite | 22 comprehensive probes | 22 passed, 0 failed (probes, catalog, problem details, headers, auth, admin) | PASS |
| E2E Test Suite | Black-box flows (T4 suite) | 10 passed, 1 xpassed (stock cache bug fixed in v4.0.1), 10 skipped (live Stripe CLI required), 0 failed | PASS |
| Load: 50 Users (5 min) | Remote load over tunnel | 10 rps, p50: 80 ms, p95: 148 ms, p99: 179 ms, 0 × 5xx / 503 | PASS |
| Load: 100 Users (5 min) | Remote load over tunnel | 20 rps, p50: 73 ms, p95: 135 ms, p99: 168 ms, 0 × 5xx / 503; 639 × 429 rate-limited | PASS |
| Minimum Free Host RAM | Continuous 5 s sampling | 1691 MB lowest point recorded (safety limit: 700 MB) | PASS |
| Host Service Integrity | Unrelated production services | Continuous uptime; 0 restarts across all systemd units and containers | PASS |
| Post-test Teardown | Complete artifact cleanup | All test containers, images, volumes, and work directories removed. Free RAM: 2540 MB (baseline: 2554 MB) | PASS |

### Rate Limiting Verification under Remote Load

During the 100-user load test, 639 requests correctly returned HTTP 429 Too Many Requests with `Retry-After` headers. Because all traffic arrived across the tunnel from loopback address `127.0.0.1`, the production IP rate limiter correctly aggregated virtual users into a single bucket and throttled excess traffic without degrading server responsiveness.

## Findings and Remediations

- **F-1 (Medium Severity / Test Environment): Missing TLS configuration on Mailpit container.**
  - *Detail:* Python's standard `smtplib` library unconditionally invokes `starttls()`. When Mailpit lacked certificates, it rejected connections with `SMTPNotSupportedError`, causing background order notification emails to fail.
  - *Remediation:* Mounted test certificates into Mailpit and enabled `MP_SMTP_REQUIRE_STARTTLS`.
- **F-2 (Low Severity / Validation): Order transition payment check.**
  - *Detail:* In v4.0.1, admin order transitions enforce `payment_status == paid` (returning 409 Conflict if unpaid). Test scripts were updated to complete mock payments prior to asserting transitions.
- **F-3 (Low Severity / Cookie Security): Guest cart cookie over HTTP.**
  - *Detail:* In plain HTTP test setups with `GUEST_CART_COOKIE_SECURE=false`, guest cart cookies are correctly issued without the `Secure` flag, validating environment adaptability.

## Limitations

- Measurements conducted through an SSH tunnel include ~49 ms median round-trip network transit latency between client and server.
- Live Stripe webhook delivery was skipped on the server in favor of mock payment provider execution to avoid deploying external development listeners in a production environment.

---

See also: [final-test-campaign.md](final-test-campaign.md), [T7-load.md](T7-load.md), [../PERFORMANCE.md](../PERFORMANCE.md), [../OPERATIONS.md](../OPERATIONS.md).
