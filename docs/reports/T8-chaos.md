# Stage T8: Chaos Engineering and Resilience Testing

Things break in production; pretending otherwise is how minor infrastructure blips become multi-hour outages. Stage T8 subjects the running backend to active chaos engineering experiments: stopping Redis, pausing PostgreSQL, killing Celery workers mid-payment, and injecting network latency to prove autonomous recovery.

## Purpose and Exit Criterion

The purpose of Stage T8 was to test system resilience, graceful degradation, and autonomous recovery under active infrastructure failures during continuous background traffic against release candidate v4.0.1.

Exit criteria (established prior to testing):
- During an outage of Redis, PostgreSQL, or Celery, the API must reject impacted traffic with 503 and `Retry-After`; zero 500 errors or hangs.
- `/readyz` probe must drop to non-200 during outages and return to 200 autonomously after dependency recovery without API restarts.
- Zero lost Stripe webhook events: all events persisted during an outage must process after recovery.
- Zero duplicate financial charges or double fulfillment.

## Environment

- **Host:** WSL2 Ubuntu on a multi-core workstation (4 CPU cores, 8 GB RAM).
- **Core Isolation:** Application stack isolated on CPU cores 0 to 2; Locust background traffic (200 users) and monitoring probes pinned to CPU core 3.
- **Components:** Release v4.0.1, PostgreSQL 15.19 (`postgres:15-alpine`), Redis 7.4.11 (`redis:7-alpine`), Celery 5.6.3 (worker and beat), Caddy 2.11.6.
- **Chaos Tooling:**
  - Docker engine container manipulation: `docker stop`, `docker start`, `docker pause`, `docker unpause`.
  - Network fault proxy: Shopify Toxiproxy 2.11.0 (`ghcr.io/shopify/toxiproxy:2.11.0`).
  - Probing: 1-second interval HTTP polling of `/livez` and `/readyz` probes.
  - Baseline smoke suite: 24 checks executed after each individual experiment.

## Method and Commands

Each experiment applied continuous 200-user load across a 360-second window:
- **0 to 120 s:** Baseline steady-state operation.
- **120 s:** Fault injection timestamp recorded.
- **120 to 240 s:** Active failure window (2 minutes).
- **240 s:** Fault removal and recovery timestamp recorded.
- **240 to 360 s:** Post-recovery stabilization and self-healing evaluation.

```bash
# Fault injection examples:
docker compose -p ecomfinal stop redis
docker compose -p ecomfinal pause db
toxiproxy-cli toxic add -t latency -a latency=200 pg_proxy
```

## Results

### Summary Table of Chaos Experiments

| Exp # | Injected Fault | Time to 1st Error | Response Mix during Fault Window | `/readyz` Recovered | Throughput Recovered | Post Smoke | Outcome |
|---|---|---|---|---|---|---|---|
| E1 | Redis stopped (stop/start) | 5.8 s | 503: 2787 (100% with Retry-After), 200: 783, 500: 0, Dropped: 3 | 26.24 s | 69.01 s | 24/24 | PASS |
| E2 | Redis frozen (pause/unpause) | 2.0 s | 503: 3629 (100% with Retry-After), 200: 451, 500: 0, Dropped: 1 | 1.20 s | 19.00 s | 24/24 | PASS |
| E3 | PostgreSQL stopped (stop/start) | 4.3 s | 500: 2205 (No Retry-After), 503: 591, 200: 1122 | 8.30 s (Pool self-healed) | 20.00 s | 24/24 | **FAIL (Review Correction)** |
| E4 | PostgreSQL frozen (pause/unpause) | 1.6 s | 503: 5132 (100% with Retry-After), 200: 35, Socket timeouts: 60 | 0.70 s | 10.00 s | 24/24 | PASS |
| E5 | Worker killed mid-burst (50 webhooks) | Immediate | API unaffected: 100% 200 OK. Worker restarted after 60 s downtime | N/A | 6.00 s (queue drained) | 24/24 | PASS |
| E6 | Stripe webhooks during PostgreSQL down | Immediate | 10 of 10 returned non-2xx (9 × 500, 1 × 503), prompting Stripe retry | 8.30 s | Immediate | 24/24 | PASS |
| E7 | Celery beat stopped for 3 minutes | Immediate | API unaffected: 100% 200 OK. Resumed schedule upon restart | N/A | Immediate | 24/24 | PASS |
| E8 | Toxiproxy +200 ms latency to PostgreSQL | 73.0 s | 503: 26 (shedding), 200: 4092, 500: 0 | N/A | Immediate | 24/24 | PASS |

### Data Preservation and Webhook Guarantees

- **Worker Killed Mid-Burst (E5):** Celery worker was killed with SIGKILL after acknowledging 31 of 50 incoming webhook tasks. The remaining 19 unacknowledged tasks were requeued by Redis (`acks_late`, `reject_on_worker_lost`). Upon worker restart 60 seconds later, all 19 tasks completed in 6 seconds. Final database audit confirmed exactly 50 of 50 events processed, 0 duplicates, and 1228 of 1228 unique PaymentIntents in the payment ledger.
- **Webhooks during Database Outage (E6):** 10 incoming webhooks received non-2xx status codes during the outage, guaranteeing delivery retries by Stripe. Post-recovery, all 10 events were received, persisted, and processed exactly once. Replaying the payloads returned `200 already_processed` with zero state changes.

## Orchestrator Review and Corrections

The testing agent initially marked experiment **E3 (PostgreSQL stopped)** as PASS on the grounds that the SQLAlchemy connection pool recovered without manual intervention.

In review, the orchestrator overruled the verdict to **FAIL**. The exit criterion explicitly mandated: "API responds with 503 and Retry-After during dependency outages; zero 500 errors". In E3, 56% of outage responses (2205 requests) returned 500 Internal Server Error without a `Retry-After` header. This constituted a critical compliance failure and was logged as finding 1 below.

## Findings and Remediations

- **Finding 1 (Medium Severity / App Bug): Unreachable database returned 500 instead of 503.**
  - *Cause:* In `app/core/database.py`, `get_async_db` caught `SQLAlchemyError` and indiscriminately re-raised an HTTP 500 "A database error occurred" problem detail.
  - *Reproducer:* Stop PostgreSQL while sending traffic: `docker compose stop db`. Endpoints return 500 without `Retry-After`.
  - *Remediation:* Differentiated database exceptions: connection refusal, socket drops, pool checkout timeouts, and connection invalidations return HTTP 503 with `Retry-After: 5` and problem type `service-unavailable`. Logical database errors (e.g. statement cancelled by `statement_timeout`) remain 500. Shipped in v4.0.2 (fix X9).
- **Finding 2 (Low Severity / Logging Gap): Multi-page traceback on Redis publication failure.**
  - *Cause:* When Redis was stopped, `enqueue_event` logged an 80-line traceback at WARNING level, despite the event being safely committed in PostgreSQL for the background sweeper to process.
  - *Remediation:* Reduced log output to a single concise line. Shipped in v4.0.2 (fix X9).
- **Finding 3 (Info / Socket Semantics): Socket timeouts during container pause.**
  - *Cause:* During E4 (PostgreSQL pause), 60 requests that had already sent TCP packets before cgroup freezing blocked until client read timeout. Subsequent requests were cleanly shed via in-flight 503.

## Limitations

- Fault injection windows were limited to 2 to 3 minutes per experiment; long-duration multi-hour degradation was evaluated under the T7 soak test.

---

See also: [fixes-4.0.x.md](fixes-4.0.x.md), [final-test-campaign.md](final-test-campaign.md), [T7-load.md](T7-load.md), [../RESILIENCE.md](../RESILIENCE.md).
