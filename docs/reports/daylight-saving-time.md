# Daylight Saving Time (DST) Resilience Verification

Few things in computing are as reliably disruptive as humans deciding that 2:00 a.m. should happen twice in one night. This audit and verification campaign proves that timestamps, scheduled jobs, and financial deadlines remain strictly monotonic across daylight saving time shifts.

## Purpose and Scope

In the early morning of 25 October 2026, European timezones underwent the autumn Daylight Saving Time transition: wall clocks rolled back from 03:00 to 02:00, resulting in the hour between 02:00 and 03:00 occurring twice.

This audit and testing campaign was conducted to verify that:
1. The e-commerce backend stores, serializes, and queries timestamps unambiguously without data corruption or duplicate job execution.
2. Celery beat schedules fire periodic jobs exactly once per 24 hours across both autumn and spring clock shifts.
3. Token lifetimes, idempotency key expirations, rate limit windows, and order cancellation deadlines maintain strict elapsed time intervals regardless of local clock adjustments.

## Application Architecture Analysis

The application codebase was audited against Python PEP 495 and PostgreSQL temporal documentation:
- **Database Storage:** All timestamp columns in PostgreSQL use `TIMESTAMP WITH TIME ZONE` (`timestamptz`). PostgreSQL stores these values as UTC internally.
- **API Serialization:** All datetime response fields serialize to ISO-8601 strings ending with the explicit `Z` UTC indicator (e.g. `2026-10-25T02:30:00Z`).
- **Date Filters:** Query parameters accepting dates (such as admin order filters) require an explicit timezone offset; naive dates without an offset are rejected with 422 Unprocessable Entity.
- **Celery Scheduler:** Celery is configured with `timezone="UTC"` and `enable_utc=True`. The four daily crontab jobs (`cleanup_inactive_accounts` 02:30, `cleanup_expired_retention` 02:45, `cleanup_expired_idempotency_keys` 03:00, `check_payment_reconciliation` 03:15) run in UTC; the rest are fixed intervals (every 5 minutes to every 4 hours), which DST cannot affect at all. Because the schedule runs in UTC, the tasks cannot duplicate or skip during European local transitions.
- **Deadlines and Expirations:** All business deadlines (guest cart expiration, access and refresh token lifetimes, order payment windows, GDPR grace periods) calculate relative time via Python `datetime.now(timezone.utc) + timedelta(...)`, which measures monotonic physical elapsed time.
- **Rate Limiting:** Redis sliding windows are managed using `PEXPIRE` with millisecond precision, completely decoupled from server wall-clock time.

## Identified Gaps and Latent Risks

Prior to the test campaign, the audit identified three risks:
1. **Latent Celery Misconfiguration Risk:** If an operator altered Celery configuration from `UTC` to a local European timezone, beat tasks scheduled between 02:00 and 03:00 would execute twice during the autumn transition and skip entirely during the spring transition.
2. **Transactional Email Timezone Ambiguity (Minor Bug):** Order confirmation emails displayed dates without an explicit UTC label (e.g. `2026-10-25 02:30:00`), which could mislead customers into assuming the timestamp represented local wall-clock time.
3. **CI Timezone Matrix Gaps:** Continuous integration evaluated five timezones, but none underwent active DST shifts during test execution, leaving PEP 495 `fold` disambiguation unasserted.

## Implemented Tests and Verification Suite (Task X6)

To permanently guard against regressions, a comprehensive DST test suite was implemented in release v4.0.1:

### 1. Celery Beat Schedule Replay Across Clock Transitions
- The complete Celery beat schedule was replayed minute-by-minute across both annual transitions:
  - Autumn transition: 24 to 27 October 2026 (repeated 02:00 to 03:00 hour).
  - Spring transition: 28 to 31 March 2026 (skipped 02:00 to 03:00 hour).
- Using Celery's `crontab.is_due` evaluation with frozen clocks, all four periodic tasks were proven to trigger exactly once per 24 hours (interval: 86400 seconds).
- **Negative Control:** When Celery timezone was intentionally overridden to `Europe/Bratislava`, interval lengths shifted to 25 hours (autumn) and 23 hours (spring), causing the test to fail. This confirmed the sensitivity of the assertion.

### 2. Timezone Offset Filter Disambiguation
- Admin date range filters were verified with ambiguous local timestamps:
  - `2026-10-25T02:30:00+02:00` correctly parsed to `2026-10-25T00:30:00Z` (first pass of the repeated hour).
  - `2026-10-25T02:30:00+01:00` correctly parsed to `2026-10-25T01:30:00Z` (second pass of the repeated hour).
  - Naive timestamps without offset return HTTP 422.

### 3. Hypothesis Property Tests with Real Timezones
- Property tests generated randomized times across `Europe/Bratislava`, `America/New_York`, and `Asia/Kolkata` with both `fold=0` and `fold=1` (PEP 495).
- Conversion to UTC and JSON serialization round-trips were proven unambiguous across all generated edge cases.

### 4. Invariant Tests with Frozen Clocks
- System time was frozen at `00:30Z`, `01:00Z`, and `01:30Z` on 25 October 2026.
- Tests asserted that:
  - Rolling "7d" statistics windows encompass exactly 168 hours.
  - "1-day" GDPR grace periods encompass exactly 24 hours.
  - Order cancellation and idempotency cleanup windows shift by their exact physical durations.
  - Deliberately injecting a 1-hour shift into the test harness caused 9 tests to fail, validating test fidelity.

### 5. CI Matrix Expansion
- Added `Europe/Bratislava` and `America/New_York` to the GitHub Actions timezone matrix.
- Added `tzdata` as a dev dependency to ensure consistent `zoneinfo` database resolution across Windows and Linux runners.

## Shipped Remediations (Release v4.0.1)

- **Email Timestamp Labeling:** Updated order confirmation templates in `app/services/email/` to format order timestamps explicitly in UTC: `YYYY-MM-DD HH:MM:SS UTC`. Datetimes received with non-zero offsets are normalized to UTC before rendering. Shipped in v4.0.1 (fix X6).
- **Hardened Invariants:** Added tests enforcing `settings.CELERY_TIMEZONE == "UTC"` and `settings.CELERY_ENABLE_UTC is True`. Shipped in v4.0.1 (fix X6).

---

See also: [fixes-4.0.x.md](fixes-4.0.x.md), [final-test-campaign.md](final-test-campaign.md), [../OPERATIONS.md](../OPERATIONS.md).
