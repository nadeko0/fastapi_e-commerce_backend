# Verification, Security, and Engineering Reports

This directory houses the comprehensive public records of all testing campaigns, security audits, architectural redesigns, and resilience evaluations for the FastAPI e-commerce backend. Every report reflects verified empirical measurements from release v4.0.3 and historical milestones.

## 📋 Final Test Campaign (v4.0.0 to v4.0.3)

- [Final Test Campaign Overview](final-test-campaign.md): Executive summary of the October 2026 test campaign, pre-agreed exit criteria verification, orchestrator review corrections, and final release metrics.
- [Remediated Defects across Releases 4.0.x](fixes-4.0.x.md): Detailed engineering record of all remediation and test packages (X1 to X10, L1 to L3) across releases v4.0.1, v4.0.2, and v4.0.3.
- [Daylight Saving Time Verification](daylight-saving-time.md): Verification of timezone transitions, UTC storage invariants, and Celery beat schedule execution across clock changes.
- [Testing Practice Research](research.md): Literature review of industry testing standards, test pyramid design, and testing methodology foundations.

## 🧪 Campaign Stage Reports (T1 to T10)

- [T1: Supply Chain and Static Security](T1-supply-chain.md): Supply chain auditing, container image minimization, pip elimination, gitleaks scanning, and govulncheck analysis.
- [T2: Test Polygon and Staging Environment](T2-test-environment.md): Test environment configuration, container network boundaries, database seeding, and fixture isolation.
- [T3: Contract and Fuzz Testing (Schemathesis)](T3-schemathesis.md): Property-based API contract fuzzing against the OpenAPI specification, error mapping, and schema alignment.
- [T4: End-to-End Black-Box Verification](T4-e2e.md): Full-journey customer purchasing, live Stripe test mode integration, Celery webhook processing, and reconciliation.
- [T5: Migrations and Disaster Recovery](T5-migrations-backup.md): Database migration round-trips (up/down/up), offline SQL rendering, and pg_dump backup and restore fidelity verification.
- [T6: Dynamic Security Testing (DAST & OWASP)](T6-dast-owasp.md): Automated OWASP ZAP scanning, authenticated API penetration testing, and input validation verification.
- [T7: Load, Stress, and Soak Testing](T7-load.md): Performance profiling across smoke, average load, stress, breakpoint, and 1-hour soak test runs.
- [T8: Chaos and Resilience Testing](T8-chaos.md): Fault injection experiments evaluating database cuts, Redis downtime, worker kills, and automatic recovery.
- [T9: Live Server Verification](T9-live-server.md): Verification on a shared VPS host confirming container resource constraints, network isolation, and performance.
- [T10: Mutation Testing](T10-mutation.md): Fault injection testing with mutmut 3.8.0 across payment, webhook, and error handling subsystems to eliminate testing gaps.

## 🏛️ Historical Reviews and Major Releases

- [Release 4.0.0 Architecture Redesign](release-4.0.0.md): Complete rationale for the v4.0.0 redesign, removal of deprecated routes, RFC 9110 and RFC 9457 alignment, and client migration checklist.
- [Pre-3.0 Security Review Record](security-review-3.0.md): Sanitized audit record of 43 security findings, vulnerability mechanisms, and verified fixes implemented in release v3.0.0.
- [3.x Load Testing and Reliability Findings](load-testing-3x.md): Empirical diagnosis of event-loop deadlocks under concurrency, off-loop payment architecture, and capacity planning guidelines.

---

See also: [Top-Level Project README](../../README.md), [Architecture Documentation](../ARCHITECTURE.md), [API Specification](../API.md), [Testing Guide](../TESTING.md), [Security Policy](../../SECURITY.md).
