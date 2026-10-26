# Final Runtime Torture & Stateful Fuzzing Report — fastapi_e-commerce_backend v4.0.3

Date: 2026-10-26 · Branch: `claude/elegant-johnson-b5n6yr`

> **Status: Phase 3 (validation) was interrupted on request because of usage limits.**
> Phases 1–2 are complete. Validators had started adding `xfail` markers (partially:
> concurrency 9, stateful 9, protocol 5 occurrences of `xfail`; boundary 0). **The suite is therefore NOT yet green in CI:**
> the `test_bug_*` reproducers below still fail by design. Bug verdicts below come from the Phase 2 agents
> (each with a deterministic reproducer). The orchestrator re-checked 3 of them against the code (marked ✔).
> No change was made to `app/`, `migrations/`, existing tests, `pyproject.toml` or `uv.lock`.

## 1. Execution summary
| Module | Tests | SQLite | PostgreSQL 16 |
|---|---|---|---|
| `test_stateful_invariants.py` (2A) | 10 (2 state machines + 4 `@given` ×300 + 4 regressions) | 5 failed / 5 passed | 6 failed / 4 passed |
| `test_protocol_rfc_torture.py` (2B) | 692 cases | 54 failed / 638 passed | 59 failed / 633 passed |
| `test_concurrency_torture.py` (2C) | 97 cases, 20–50 parallel requests, 5–8 rounds | 2 failed / 1 passed / 94 skipped (PG-only) | 19 failed / 78 passed (7 runs) |
| `test_boundary_mutants.py` (2D) | 298 cases, 47 boundaries, 46 hand mutants (41 killed) | 12 failed / 286 passed | 12 failed / 286 passed |

* ≈2,000+ state-machine examples, ≈25k HTTP requests per back end (2A), plus thousands of `@given` examples (2B).
* Existing suite baseline: 5214 passed (SQLite), 2263 passed (PG `db_sensitive`).
* **No failure is a harness failure**: every failing test maps to one of the 22 suspected bugs below.
* schemathesis / mutmut are not project dependencies and were not added (no `uv.lock` change).
* **Verdict:** core money invariants hold (no oversell, no over-refund under different keys, one charge per order,
  idempotent webhooks, RFC 9457 everywhere, no 500 under refresh races). Weak spots: PG lock interaction (deadlock → 503),
  numeric/depth bounds on inputs (500s on PG), refund-event ordering, ETag wildcard parsing.

## 2. Discovered bugs (reproducer → production location → fix)
Severity: H high, M medium, L low. Full counterexamples and diffs: `.cloud_torture/02a…02d_*.md`.

### High
| # | Bug | Reproducer | Location | Fix |
|---|---|---|---|---|
| C3 ✔ | Concurrent checkouts deadlock when one cart has a variant line and another the plain line of the same product → 503 (up to 23/30 in a burst) | `test_concurrency_torture.py::test_bug_*` (BUG-3) | `app/services/orders.py:84-91` (`with_for_update()` = FOR UPDATE blocks FK `KEY SHARE` from `order_items` insert) | `.with_for_update(key_share=True)` (FOR NO KEY UPDATE) on both selects; prototype green |
| A1 | Late `refund.created`/`charge.refunded` event rewinds a settled refund (succeeded→pending, order refunded→paid) | `test_stateful_invariants.py::test_bug_stale_refund_event_reverts_a_settled_refund` | `app/services/stripe_events.py:253-255` | make refund status monotonic (never downgrade terminal status) |
| A2 | `/refund` → 500 after provider already refunded, when an earlier `refund.failed` is unprocessed (`ck_payment_refunded_within_amount`) — money moved, nothing recorded | `test_bug_refund_after_an_unreported_refund_failure_is_a_500` | `app/api/v1/orders.py:1625→1767` | re-sync the charge's refunds from provider before recording |
| B2 | Nested JSON ≥255 levels in `attributes`/`characteristics` → 500; row committed first, so every public catalog GET returns 500 afterwards | `test_protocol_rfc_torture.py::test_bug_*nest*` | `app/core/etag.py:25`, `app/api/v1/admin.py:212,680`, `app/schemas/common.py:87` | bound JSON depth in `RequestBody` validation (e.g. ≤32) |

### Medium
| # | Bug | Location | Fix |
|---|---|---|---|
| C1 | Refresh rotated concurrently with logout survives logout | `app/services/redis_async.py:424`, `app/core/security.py:537-539` | mark family revoked first; reject tokens of revoked family |
| C2 | In-flight retry of `POST /orders` with same Idempotency-Key → 400 "Cart is empty" | `app/api/v1/orders.py:206,211-215` | re-check key before refusing empty cart (replay / 409) |
| C4 | Stripe events stored after GDPR purge keep PII forever | `app/tasks.py:341,363`; `app/services/stripe_events.py:438` | redact on store for erased users / re-run purge |
| A3 | Restock overflows int32 stock → 500 on cancel (PG), aborts expiry sweep | `app/services/orders.py:123,130`; `app/tasks.py:499`; `app/schemas/product.py:83,193` | cap admin stock or check before restock |
| B1 | Login 401 for registered IDNA/fullwidth/`。` emails | `app/schemas/user.py:104-108` vs `app/api/v1/users.py:124` | apply same email-validator normalisation on login |
| B3 | `NaN`/`Infinity`/`1e400` in free-form JSON → 500 on PG, null on SQLite | free-form JSON fields | reject non-finite numbers (RFC 8259 §6) |
| B4 ✔ | `*` anywhere in If-Match/If-None-Match = wildcard (`"stale", *` overwrites) | `app/core/etag.py:21` | treat `*` only as whole field value |
| B9 | PG: `?min_price=1e-16384` → 500 | `app/schemas/product.py:140-141` | bound digits/exponent (`max_digits`, `decimal_places`) |
| D5 | Order total > Numeric(10,2) → 500 on PG / stored on SQLite | `app/api/v1/orders.py:366-368` | check total ≤ 99,999,999.99 → 422 |
| D1 ✔ | Logged-out access token valid again up to ~1 s before exp | `app/core/security.py:625` (`int()` floors) | `math.ceil(payload["exp"] - time.time())` (mutant M39 verified) |

### Low
| # | Bug | Location | Fix |
|---|---|---|---|
| B5 | Money accepts non-ASCII digits (`"１０.５０"`) | `app/schemas/order.py:45,47` | `re.ASCII` |
| B6 | PATCH doesn't deep-merge nested objects (RFC 7396 §2) — may be a design choice, needs confirmation | `app/api/v1/admin.py:286,831` | recursive merge |
| B7 | `application/json-patch+json` applied as merge patch (should be 415) | PATCH routes | accept only merge-patch/json |
| B8 | If-Match ignored on `/users/me`, `/users/addresses/{id}` | `app/api/v1/users.py:511,732` | evaluate If-Match |
| C5 | Mock provider: concurrent same-key refunds both pass (dev/CI only) | `app/services/payment/stripe_provider.py:486-532` | lock around key check+record |
| D2 | Rate limit refuses at exactly Retry-After (1 ms) | `app/core/rate_limit.py:219-221,251-253` | align TTL with window |
| D3 | Attempt exactly 5 min old counted as stale | `app/api/v1/orders.py:925`; `app/services/stripe_events.py:87` | `>=` (mutants M20/M21) |
| D4 | Data-retention status floors days (up to ~1 day wrong) | `app/services/gdpr.py:180-181` | compare `timedelta`s |

## 3. Invariants & boundaries proven solid
* Stock conservation `initial + admin_adj == current + reserved + sold` at every state-machine step; never negative; no oversell in any burst (even deadlocking ones), no partial orders.
* Exactly one charge per order; refunds with different keys never exceed capture; provider total == DB.
* Refresh reuse: exactly one winner, whole family revoked. Same-key `/orders` and `/pay`: one side effect, identical replays, 422 on payload mismatch.
* Webhooks: 30 redeliveries → 1 row; 10 workers apply once. Concurrent cancels restock once.
* Passwords: NFC equivalents log in; no collision above 72 bytes. One canonical email → one account.
* Weak ETags fail If-Match, If-None-Match weak compare; pagination to 2^31-1 safe; no int overflow on ids.
* Every error response (incl. 500s) is valid RFC 9457 `application/problem+json`, no leakage.
* 47 time/count boundaries agree between SQLite and PG to the microsecond.
* Policy notes (not bugs): NFC rather than NFKC for passwords; min length 8 (< 15 of SP 800-63B-4); blocklist misses fullwidth spellings.

## 4. Remaining work (Phase 3 not completed)
1. Finish `@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: …")` on every `test_bug_*` (PG-only ones with `xfail(IS_POSTGRES, …)`).
2. Nondeterministic burst/state-machine tests: known-bug allowance, switchable via `TORTURE_STRICT=1`.
3. Mark the concurrency module `db_sensitive` + `timing_sensitive` (CI PG15 has 100 connections, xdist).
4. Verify each fix diff on a copy of `app/` (only D1, D3 and C3 were prototype-verified).
