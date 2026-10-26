# Research on Testing Practice and Standards

This document summarizes the research into software testing methodology, official library documentation, and industry standards conducted prior to the final testing campaign of release v4.0.0. The campaign structure, test profiles, exit criteria, and remediation packages (L1, L2, L3) were built directly upon these conclusions and their primary public sources.

## 1. Testing Strategy and Architecture

### Conclusions
- **The Practical Test Pyramid:** As articulated by Martin Fowler and Ham Vocke, automated testing should be structured with a broad base of fast, isolated unit tests, a substantial layer of integration and contract tests at service boundaries, and a minimal set of end-to-end black-box tests. End-to-end tests are expensive to maintain and prone to flakiness; coverage should not be duplicated unnecessarily across upper layers. The inverted "ice-cream cone" anti-pattern must be avoided.
- **Payment Gateway Testing Guidelines:** Official Stripe documentation recommends verifying webhook handling, idempotency, and failure recovery before going to production. Automated CI pipelines should run against dedicated sandboxes, while live webhook delivery should be decoupled from test synchronization using asynchronous queues.

### Public Sources
- Martin Fowler, Ham Vocke (2018), *The Practical Test Pyramid*: https://martinfowler.com/articles/practical-test-pyramid.html
- Stripe Billing Testing Documentation: https://docs.stripe.com/billing/testing
- Stripe Webhooks Integration Guide: https://docs.stripe.com/webhooks
- Stripe CLI Webhook Triggers: https://docs.stripe.com/stripe-cli/triggers
- Stripe Testing Cards and Simulation: https://docs.stripe.com/testing
- Stripe Billing Test Clocks: https://docs.stripe.com/billing/testing/test-clocks

## 2. API Contract and Property-Based Fuzz Testing

### Conclusions
- **Automated Contract Conformance:** Schemathesis generates property-based test cases directly from an OpenAPI 3.1.0 document. It validates that all responses conform strictly to declared response schemas, declared status codes, and media types, while ensuring that arbitrary or malicious input cannot trigger unhandled 5xx server errors.
- **Stateful API Fuzzing:** The stateful testing phase traverses multi-step user workflows using OpenAPI links (e.g. chaining resource creation POST responses to subsequent GET, PUT, and DELETE operations). When explicit links are absent from generated FastAPI schemas, explicit links must be injected or supplemented with deterministic end-to-end flows to ensure deep stateful coverage.

### Public Sources
- Schemathesis Documentation: https://schemathesis.readthedocs.io/en/stable/
- Schemathesis CLI Reference: https://schemathesis.readthedocs.io/en/stable/reference/cli/
- Schemathesis Stateful Testing Guide: https://schemathesis.readthedocs.io/en/stable/guides/stateful-testing/

## 3. Performance, Overload, and Soak Testing

### Conclusions
- **Workload Archetypes (k6 Methodology):** Non-functional performance testing should be partitioned into specific execution profiles:
  - *Smoke:* Minimal load to verify script correctness and basic responsiveness.
  - *Average-Load:* Expected production concurrency sustained over 10 to 60 minutes to establish baseline latencies.
  - *Stress:* Traffic above normal capacity to assess system behavior near operational limits.
  - *Spike:* Sudden massive bursts over seconds to verify stability under traffic surges.
  - *Breakpoint:* Gradually ramping load until system throughput degrades to identify hard capacity knees.
  - *Soak:* Sustained load over extended durations (1 to several hours) to detect slow resource degradation, connection pool exhaustion, or memory leaks.
- **Overload Handling Principles (Google SRE):** A service under extreme load must gracefully shed excess requests via rapid HTTP 503 responses with `Retry-After` headers rather than attempting to serve all requests and freezing the event loop or exhausting worker pools.
- **Resource Plateau Criteria:** In a soak test, a system is considered stable only when active database connection counts and process RSS memory flatten into a plateau following initial warm-up. Linear upward drift signifies a resource leak.

### Public Sources
- Grafana k6 Test Types: https://grafana.com/docs/k6/latest/testing-guides/test-types/
- Grafana k6 Soak Testing Guide: https://grafana.com/docs/k6/latest/testing-guides/test-types/soak-testing/
- Grafana k6 Thresholds (SLOs): https://grafana.com/docs/k6/latest/using-k6/thresholds/
- Grafana k6 Scenario Executors: https://grafana.com/docs/k6/latest/using-k6/scenarios/executors/
- Locust Load Testing Framework: https://docs.locust.io/en/stable/quickstart.html
- Google SRE Book, *Handling Overload*: https://sre.google/sre-book/handling-overload/

## 4. Chaos Engineering and Dependency Fault Tolerance

### Conclusions
- **Principles of Chaos:** Systems must establish a steady-state hypothesis and actively verify that this hypothesis holds when external dependencies fail. Experiments must contain their blast radius to avoid impacting co-located systems.
- **Celery Task Delivery Guarantees:** As documented in Celery architecture guidelines, setting `acks_late=True` combined with `task_reject_on_worker_lost=True` ensures that messages are re-delivered to the queue if a worker process is terminated mid-execution. Because tasks may execute more than once following a crash, all background tasks (order e-mails, webhook processing) must be strictly idempotent.

### Public Sources
- Principles of Chaos Engineering: https://principlesofchaos.org/
- Shopify Toxiproxy Network Simulation: https://github.com/Shopify/toxiproxy
- Celery Task Configuration and Delivery Guarantees: https://docs.celeryq.dev/en/stable/userguide/tasks.html

## 5. Dynamic Application Security Testing (DAST) and Supply Chain

### Conclusions
- **OWASP API Security Top 10 2023:** Modern API security requires validating: Broken Object Level Authorization (BOLA), Broken Authentication, Broken Object Property Level Authorization (mass assignment), Unrestricted Resource Consumption, Broken Function Level Authorization (BFLA), Unrestricted Access to Sensitive Business Flows, Server Side Request Forgery (SSRF), Security Misconfiguration, Improper Inventory Management, and Unsafe Consumption of APIs.
- **Container and Dependency Hygiene:** Container base images must be scanned for vulnerabilities with known fixes. Secret scanners must inspect the entire commit history rather than just the working tree. Reachability tools (such as Go `govulncheck`) should be used to verify whether symbols flagged by vulnerability scanners are actually called by runtime binaries.

### Public Sources
- OWASP API Security Top 10 2023: https://api-security.owasp.org/editions/2023/en/0x11-t10
- OWASP ZAP API Scanner Documentation: https://www.zaproxy.org/docs/docker/api-scan/
- OWASP ZAP Container Overview: https://www.zaproxy.org/docs/docker/
- Trivy Container Vulnerability Scanner: https://trivy.dev/docs/latest/guide/scanner/vulnerability/
- Gitleaks Secret Scanner: https://github.com/gitleaks/gitleaks
- Pip-audit Python Package Auditor: https://pypi.org/project/pip-audit/

## 6. Database Migrations, Reversibility, and Disaster Recovery

### Conclusions
- **Disaster Recovery Rule:** A backup strategy is only proven when the recovery process has been validated by restoring the backup into a clean instance, verifying row counts and checksums, and executing application smoke tests against the restored data.
- **Logical Backups vs Continuous Archiving:** PostgreSQL documentation notes that logical dumps (`pg_dump -Fc`) create point-in-time snapshots but cannot provide Point-In-Time Recovery (PITR). PITR requires continuous WAL archiving (`archive_mode=on`, `archive_command`) combined with physical base backups (`pg_basebackup`).
- **Migration Reversibility:** Schema migrations must support zero-downtime execution, rollback cleanly (`downgrade`), and render valid SQL in offline generation mode (`alembic upgrade head --sql`).

### Public Sources
- PostgreSQL 18 Documentation, `pg_dump`: https://www.postgresql.org/docs/current/app-pgdump.html
- PostgreSQL 18 Documentation, `pg_restore`: https://www.postgresql.org/docs/current/app-pgrestore.html
- PostgreSQL 18 Documentation, Backup and Restore: https://www.postgresql.org/docs/current/backup.html
- PostgreSQL 18 Documentation, Continuous Archiving and PITR: https://www.postgresql.org/docs/current/continuous-archiving.html
- PostgreSQL 18 Documentation, Date/Time Types: https://www.postgresql.org/docs/current/datatype-datetime.html

## 7. Mutation Testing and Test Suite Adequacy

### Conclusions
- **Code Coverage vs Test Adequacy:** 100% line or branch coverage does not prove that tests assert correct behavior; it only proves that lines were executed. Mutation testing introduces synthetic syntactic faults (e.g. modifying arithmetic operators, boundary conditions, or exception handling) to evaluate whether existing tests fail (kill the mutant).
- **Targeted Application:** Full-codebase mutation testing is computationally intensive. The recommended practice is to focus mutation testing on high-risk domains: financial ledgers, payment processing, transaction reconciliation, rate limiting, and access control. Mutants that survive must be investigated to distinguish between equivalent mutations and genuine assertion gaps.

### Public Sources
- Mutmut Documentation: https://mutmut.readthedocs.io/en/latest/
- Mutmut Configuration Guide: https://mutmut.readthedocs.io/en/latest/configuration.html
- Mutmut on PyPI: https://pypi.org/project/mutmut/
- Mutmut Repository: https://github.com/boxed/mutmut

## 8. Timezone Disambiguation and Temporal Resilience

### Conclusions
- **Local Time Disambiguation (PEP 495):** During Daylight Saving Time transitions where clocks roll back, local time becomes ambiguous. Python's `datetime` implements PEP 495 using the `fold` attribute (`fold=0` for the first occurrence of a repeated hour, `fold=1` for the second). Systems should store and compute timestamps exclusively in UTC (`timestamptz`), serializing to ISO-8601 with `Z` indicators, and perform timezone conversion only at presentation boundaries.
- **Periodic Job Scheduling:** Crontab-style schedules configured in local time risk running twice or skipping during DST shifts. In Celery, configuring `timezone="UTC"` and `enable_utc=True` guarantees that periodic intervals remain strictly fixed at 24 hours (86400 seconds).

### Public Sources
- Python PEP 495, *Local Time Disambiguation*: https://peps.python.org/pep-0495/
- Python Standard Library, `zoneinfo`: https://docs.python.org/3/library/zoneinfo.html
- Python Standard Library, `datetime`: https://docs.python.org/3/library/datetime.html
- Linux Programmer's Manual, `crontab(5)`: https://man7.org/linux/man-pages/man5/crontab.5.html
- Celery Periodic Tasks User Guide: https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html
- Celery Configuration Guide: https://docs.celeryq.dev/en/stable/userguide/configuration.html
- Celery Schedules Reference: https://docs.celeryq.dev/en/stable/reference/celery.schedules.html
- Hypothesis Strategy Reference: https://hypothesis.readthedocs.io/en/latest/reference/strategies.html
- Time Machine (Time Freezing for Python): https://time-machine.readthedocs.io/en/latest/usage.html
- Freezegun Library: https://github.com/spulec/freezegun

## 9. Framework Best Practices (FastAPI, Pydantic, Pytest)

### Conclusions
- **Asynchronous Testing:** The FastAPI testing guide advises testing asynchronous endpoints using `httpx.AsyncClient` paired with `ASGITransport` to run against a real event loop without spawning external HTTP threads.
- **Property-Based Testing Integration:** Hypothesis integration documentation recommends creating dedicated CI profiles (`HYPOTHESIS_PROFILE=ci`) with fixed deterministic budgets to avoid random build failures in automated release pipelines, while reserving exploratory unseeded runs for development.
- **Order Dependency Testing:** Pytest documentation recommends running test suites in randomized execution order (e.g. via `pytest-randomly`) to uncover hidden test inter-dependencies and teardown leaks.

### Public Sources
- FastAPI Advanced User Guide, *Async Tests*: https://fastapi.tiangolo.com/advanced/async-tests/
- FastAPI Reference, *TestClient*: https://fastapi.tiangolo.com/reference/testclient/
- Pytest Documentation, *Good Practices*: https://docs.pytest.org/en/stable/explanation/goodpractices.html
- Hypothesis Settings and Profiles Reference: https://hypothesis.readthedocs.io/en/latest/reference/api.html

---

See also: [final-test-campaign.md](final-test-campaign.md), [fixes-4.0.x.md](fixes-4.0.x.md), [../TESTING.md](../TESTING.md).
