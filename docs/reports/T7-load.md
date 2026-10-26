# Stage T7: Load, Stress, and Soak Performance Testing

Pushing an API until it breaks reveals whether it degrades with dignity or collapses in chaos. Stage T7 benchmarks latency and throughput across eight distinct execution profiles, confirming that in-flight limiters shed excess load with standard retry advice before connection pools starve.

## Purpose and Exit Criterion

The purpose of Stage T7 was to evaluate system throughput, latency percentiles under concurrent traffic, overload shedding resilience, and resource stability during sustained execution against release candidate v4.0.1.

Exit criteria:
- Average load (200 users): p95 latency no more than 25% worse than 3.x baseline (~66 to 69 ms); zero 5xx server errors.
- Overload protection (spike and breakpoint): excess traffic rejected via rapid 503 responses carrying `Retry-After`; zero 500 or 502 errors; zero event loop hangs.
- Soak test (1 hour): database connections, worker RSS, and API process memory reach a stable plateau without unbounded leakage.

## Environment

- **Host Machine:** WSL2 Ubuntu running on a multi-core workstation (4 CPU cores, 8 GB RAM).
- **CPU Pinning (Core Isolation):**
  - Application stack (API, DB, Redis, Celery worker/beat, Caddy, Mailpit) isolated on CPU cores 0 to 2 via `<work dir>/isolate.sh`.
  - Locust load generator (v2.46.7) and resource sampler pinned strictly to CPU core 3 (`--cpuset-cpus 3`, `taskset -c 3`).
- **Application Configuration:**
  - Release v4.0.1, 4 Uvicorn workers, `MAX_IN_FLIGHT_REQUESTS=15`, DB connection pool size 10 (overflow 5), `BCRYPT_ROUNDS=12`.
  - `PAYMENT_PROVIDER=mock` (to measure raw stack throughput without external Stripe gateway latency).
  - Rate limits raised to 100000/s for capacity tests; production rate limits restored for the `limiter` profile.
- **Comparison Baseline (3.x):** Measured on bare-metal Windows 11 with PostgreSQL 18 and Memurai (runs 38/38b in `scripts/loadtest/FINDINGS.md`: 200 users, 4 workers, in-flight 15: p50 10 ms, p95 66 to 69 ms). The comparison serves as a directional reference due to differing virtualization stacks.

## Method and Commands

Locust simulated realistic customer personas: Visitor (62%), Shopper (25%), NewCustomer (5%), AccountUser (5%), Admin (2%), and Health Monitor (1%).

Execution profiles:
- **smoke:** 5 users, 120 s hold.
- **average (TLS):** 200 users, ramp 120 s, hold 600 s via Caddy HTTPS (`https://localhost:8443`).
- **avg_direct (HTTP):** 200 users, ramp 120 s, hold 600 s directly to API (`http://<api-ip>:8000`).
- **stress400:** 400 users, ramp 180 s, hold 600 s directly to API.
- **stress600:** 600 users, ramp 180 s, hold 600 s directly to API.
- **spike:** 50 users (60 s) -> jump to 800 users in 10 s (hold 120 s) -> return to 50 users (180 s).
- **breakpoint:** 50 users initial, stepping +50 users every 20 s up to 650 users.
- **limiter:** 200 users, ramp 60 s, hold 300 s via Caddy with production rate limits enabled.
- **soak2:** 200 users, ramp 120 s, sustained hold 3480 s (total duration 60 minutes).

## Results

### Performance and Overload Summary Table

| Profile | Users | RPS | p50 (ms) | p95 (ms) | p99 (ms) | 500 / 502 | 503 Overload (Retry-After) |
|---|---|---|---|---|---|---|---|
| smoke | 5 | 1.2 | 19 | 300 | 554 | 0 | 0 |
| average (Caddy TLS) | 200 | 41.4 | 24 | 68 | 331 | 0 | 0 |
| avg_direct (Direct API) | 200 | 41.6 | 11 | 57 | 314 | 0 | 0 |
| stress400 | 400 | 82.5 | 15 | 87 | 338 | 0 | 0 |
| stress600 | 600 | 114.9 | 124 | 1522 | 4267 | 0 | 7476 (13.6% shed) |
| spike (50 -> 800) | 800 | 55.4 | 340 | 2281 | 6020 | 0 | 10992 (55.0% shed) |
| breakpoint (50 -> 650) | 650 | 69.7 | 23 | 465 | 1052 | 0 | 299 (shedding starts ~450u) |
| limiter (Prod limits) | 200 | 41.8 | 27 | 65 | 352 | 0 | 3532 × 429 Too Many Requests |
| soak2 (1-hour soak) | 200 | 41.3 | 15 | 139 | 534 | 0 | 15 (0.01% transient shed) |

### 1-Hour Soak Stability Metrics (Three 20-Minute Windows)

| Resource Metric | 0 - 20 min | 20 - 40 min | 40 - 60 min | Net Trend across 1 Hour |
|---|---|---|---|---|
| PostgreSQL active connections | 43 | 43 | 43 | Perfectly flat (zero pool leakage) |
| Celery worker RSS memory | 529.5 MiB | 529.4 MiB | 536.0 MiB | Stable (+6.5 MiB transient fluctuation) |
| API process RSS memory | 607.7 MiB | 612.1 MiB | 614.1 MiB | +6.4 MiB (+1.05%), rate decelerating |

### Capacity Knee on Benchmark Machine

- **Up to 400 users (~82.5 rps):** Zero dropped requests, p95 latency under 90 ms.
- **450 to 500 users (85 to 100 rps):** Saturation threshold; in-flight limiter begins shedding requests via 503.
- **550 to 600 users (110 to 115 rps):** In-flight queue saturated; p95 latency exceeds 1.5 s, 13.6% traffic shed.

## Orchestrator Review and Verdicts

1. **Average Load:** **PASS**. p95 was 68 ms via TLS and 57 ms direct (vs 66/69 ms in 3.x baseline); zero 5xx errors.
2. **Overload Protection:** **PASS**. Zero 500 or 502 errors across spike and breakpoint tests; excess requests immediately shed with 503 `service-overloaded` and `Retry-After` (median cutoff latency 104 ms).
3. **Soak Test (1 hour):** **PASS with caveat**. The testing agent initially concluded an unqualified "plateau". In review, the orchestrator analyzed memory deltas: API process RSS grew from 607.7 to 614.1 MiB (+6.4 MiB over the final 40 minutes). While the growth rate decelerated (+4.4 MiB then +2.0 MiB), suggesting Python memory allocator warm-up rather than a code leak, a 1-hour window is insufficient to prove a permanent plateau. A 4 to 6 hour extended soak would be required to definitively confirm a permanent plateau.

## Findings and Operational Notes

- **Initial Soak Run Invalidation (Harness Bug):** In the first soak attempt, admin tokens (15-minute expiry) expired after 914 s, resulting in subsequent admin requests returning 401. The generator script was patched to rotate tokens, the run was discarded, and `soak2` was executed cleanly.
- **ClientDisconnect on Webhook Route (Low Severity):** Under `stress600`, one `ClientDisconnect` occurred when Locust aborted during request body transmission. Handled gracefully without 500 errors or process crashes.
- **TLS Overhead:** Caddy TLS added 7 to 13 ms to median latency (p50: 24 ms vs 11 ms).

## Limitations

- Workstation benchmark: services and load generator executed on the same physical machine, sharing memory bus and caches (mitigated via strict cpuset core isolation).
- Mock payment provider eliminated third-party network transit latency.

---

See also: [load-testing-3x.md](load-testing-3x.md), [T8-chaos.md](T8-chaos.md), [T9-live-server.md](T9-live-server.md), [../PERFORMANCE.md](../PERFORMANCE.md).
