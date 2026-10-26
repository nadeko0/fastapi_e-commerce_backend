# Performance and Overload Protection

An e-commerce backend must remain responsive under standard traffic and degrade gracefully when overwhelmed. Rather than allowing unbounded concurrency to exhaust database connections and stall the event loop, this architecture enforces strict in-flight limits, atomic rate limits, and connection pooling.

## Load and Stress Test Results (v4.0.1)

Performance was evaluated in Stage T7 using Locust 2.46.7 against release v4.0.1.

- **Benchmark Environment:** WSL2 Ubuntu (4 CPU cores, 8 GB RAM). Core isolation pinned the application stack to CPU cores 0 to 2 and Locust to CPU core 3.
- **Runtime Configuration:** 4 Uvicorn workers, `MAX_IN_FLIGHT_REQUESTS=15`, PostgreSQL connection pool size 10 (overflow 5), `BCRYPT_ROUNDS=12`, `PAYMENT_PROVIDER=mock`.
- **User Personas:** Visitor (62%), Shopper (25%), NewCustomer (5%), AccountUser (5%), Admin (2%), Health Monitor (1%).

### Summary of Benchmark Runs

| Profile | Target Users | Throughput (RPS) | Latency p50 | Latency p95 | Latency p99 | 500 / 502 Errors | Overload 503 / 429 |
|---|---|---|---|---|---|---|---|
| smoke | 5 | 1.2 | 19 ms | 300 ms | 554 ms | 0 | 0 |
| average (Caddy TLS) | 200 | 41.4 | 24 ms | 68 ms | 331 ms | 0 | 0 |
| avg_direct (Direct API) | 200 | 41.6 | 11 ms | 57 ms | 314 ms | 0 | 0 |
| stress400 | 400 | 82.5 | 15 ms | 87 ms | 338 ms | 0 | 0 |
| stress600 | 600 | 114.9 | 124 ms | 1522 ms | 4267 ms | 0 | 7,476 x 503 (13.6% shed) |
| spike (50 to 800) | 800 | 55.4 | 340 ms | 2281 ms | 6020 ms | 0 | 10,992 x 503 (55.0% shed) |
| breakpoint (50 to 650) | 650 | 69.7 | 23 ms | 465 ms | 1052 ms | 0 | 299 x 503 (shedding at ~450u) |
| limiter (Prod Limits) | 200 | 41.8 | 27 ms | 65 ms | 352 ms | 0 | 3,532 x 429 Too Many Requests |
| soak2 (1-Hour Soak) | 200 | 41.3 | 15 ms | 139 ms | 534 ms | 0 | 15 x 503 (0.01% transient shed) |

### Throughput Knee and Capacity Limits
- **Sub-saturation (up to 400 users, ~82.5 rps):** Zero dropped requests, p95 latency under 90 ms.
- **Capacity Knee (450 to 500 users, 85 to 100 rps):** Worker concurrency saturates; the in-flight middleware begins shedding requests with rapid 503 responses.
- **Heavy Overload (550 to 600 users, 110 to 115 rps):** Excess requests are immediately rejected (median cutoff 104 ms) to protect active transactions.

### 1-Hour Soak Test Stability and Caveat
During the 1-hour sustained soak test (`soak2`, 200 users, ~41.3 rps):
- **Database Connections:** Remained perfectly constant at 43 active connections (zero connection pool leakage).
- **Celery Worker Memory:** Remained stable at ~536 MiB RSS (+6.5 MiB transient fluctuation).
- **API Process Memory:** Process RSS increased by 6.4 MiB (+1.05%, from 607.7 to 614.1 MiB across the final 40 minutes). Because the rate of increase decelerated (+4.4 MiB then +2.0 MiB), the orchestrator reviewed this as a PASS with caveat: the growth is consistent with Python memory allocator warm-up, but a 4 to 6 hour extended soak is required to confirm a permanent plateau.

## Live Server Verification (Masked)

Stage T9 evaluated release v4.0.1 deployed in isolated containers on a shared VPS (2 vCPU, 3.7 GB RAM, no swap) running unrelated production services (several containers and systemd units).
- **Isolation and Safety:** Containers were constrained to a total ceiling of 1504 MB RAM (peak stack usage ~600 MB). Minimum free host RAM never dipped below 1691 MB (safety threshold: 700 MB), and neighboring services operated with zero restarts.
- **Encrypted SSH Tunnel:** Tests ran over loopback via an SSH tunnel with ~48.7 ms median network round-trip time.
- **Results:** 50 virtual users yielded 10 rps with p95 latency of 148 ms; 100 users yielded 20 rps with p95 latency of 135 ms and zero 5xx errors. The rate limiter correctly throttled 639 requests to HTTP 429 when traffic arrived from the single tunnel loopback address.

## Comparison with 3.x Baseline

The 3.x baseline was measured on bare-metal Windows 11 with PostgreSQL 18 and Memurai (200 users, 4 workers, in-flight cap 15: p50 10 ms, p95 66 to 69 ms; detailed in [reports/load-testing-3x.md](reports/load-testing-3x.md)). In release v4.0.1, direct API execution reached p50 11 ms and p95 57 ms (68 ms via TLS), matching baseline responsiveness despite the overhead of container virtualization.

## Overload Handling and Shedding

Without concurrency control, overloading this application does not degrade it gracefully: it stops it entirely while the event loop waits on database connections only the loop itself can free (an exquisite, self-inflicted deadlock).

### In-Flight Request Limiting
`InFlightLimitMiddleware` (`app/core/overload.py`) wraps the ASGI application outside Starlette routing:
- It tracks active HTTP requests per worker process.
- When `in_flight >= MAX_IN_FLIGHT_REQUESTS`, it immediately rejects incoming requests with HTTP 503 Service Unavailable, problem details type `service-overloaded`, detail "Server is at capacity, please retry shortly", and header `Retry-After: 1`.
- Safe ceiling formula: `(DB_POOL_SIZE + DB_MAX_OVERFLOW) / 2` because an authenticated request can hold two database connections simultaneously (auth session and route session).
- Probe endpoints (`/health`, `/livez`, `/readyz`) are exempt so load shedding never causes orchestrators to restart healthy containers.

### Rate Limiting and Headers
`RateLimiter` (`app/core/rate_limit.py`) applies atomic fixed-window counting via Redis Lua scripts:
- Keyed by user ID for authenticated requests, or client IP address (accounting for `TRUSTED_PROXIES` and `/64` IPv6 subnets).
- Responses include IETF draft headers: `RateLimit-Policy` (e.g. `"default";q=300;w=60`) and `RateLimit` (e.g. `"default";r=285;t=42`).
- Rejections return HTTP 429 Too Many Requests with `Retry-After` set to seconds remaining in the window.
- Fail-open policy: if Redis is unreachable, standard routes permit requests rather than causing an outage (auth failed-login counting fails closed).

## Tuning Settings

Configuration parameters in `app/core/config.py`:

| Setting | Default Value | Description |
|---|---|---|
| `WORKERS` | `4` | Number of Uvicorn worker processes |
| `DB_POOL_SIZE` | `10` | SQLAlchemy connection pool size per engine (sync and async) |
| `DB_MAX_OVERFLOW` | `5` | Maximum overflow connections above pool size |
| `DB_POOL_TIMEOUT` | `30` | Seconds to wait before failing connection checkout |
| `DB_POOL_PRE_PING` | `True` | Validates connection liveness before checkout |
| `DB_POOL_RECYCLE` | `3600` | Connection recycling timeout in seconds |
| `REDIS_POOL_SIZE` | `50` | Redis client connection pool size |
| `REDIS_POOL_TIMEOUT` | `20` | Redis connection timeout in seconds |
| `MAX_IN_FLIGHT_REQUESTS` | `None` | Active request ceiling per worker (set to 15 in benchmarks) |
| `RATE_LIMIT_ENABLED` | `True` | Global toggle for rate limiting |
| `RATE_LIMIT_DEFAULT` | `"300/minute"` | Default route rate limit |
| `RATE_LIMIT_SEARCH` | `"60/minute"` | Product search endpoint limit |
| `RATE_LIMIT_AUTH` | `"20/minute"` | Authentication route limit per IP |
| `RATE_LIMIT_CHECKOUT` | `"10/minute"` | Order creation, payment, and checkout session limit |
| `RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS` | `"10/15 minutes"` | Hard lockout for a specific IP targeting an account |
| `RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL` | `"100/hour"` | Distributed guessing backstop across all IPs |
| `RATE_LIMIT_GUEST_CART` | `"100/hour"` | Limit on new guest cart creation per IP |
| `TRUSTED_PROXIES` | `[]` | CIDRs and IPs permitted to set X-Forwarded-For headers |

See also: [reports/T7-load.md](reports/T7-load.md), [reports/T9-live-server.md](reports/T9-live-server.md), [reports/load-testing-3x.md](reports/load-testing-3x.md), [TESTING.md](TESTING.md), [RESILIENCE.md](RESILIENCE.md).
