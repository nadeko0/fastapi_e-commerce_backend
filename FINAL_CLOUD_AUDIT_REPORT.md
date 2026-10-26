# Final Cloud Audit Report: FastAPI E-commerce Backend v4.0.3

| | |
|---|---|
| Target | `fastapi_e-commerce_backend` v4.0.3, HEAD `66596f8` (application code identical to the audited `6bf493d`; later commits add only audit artefacts) |
| Date | 2026-10-26 |
| Method | Phase 1: inventory and baseline. Phase 2: four parallel code audits (2A payments, 2B catalog/orders, 2C auth/GDPR, 2D infrastructure and comments). Phase 3: docs drift (3A), test-suite audit (3B), interview-defense review (3C). Phase 4: two skeptical validators plus executable verification tests. |
| Precedence | Validator verdicts (`04_validation_phase2.md`, `04_validation_phase3.md`) override auditor severities. Items they discarded are not listed as findings. |
| Repository changes | This report is the only file written. No code, test or doc was changed, and nothing was committed. All patches below are proposals, checked with `git apply --check` against `66596f8`. |

---

## 1. Executive Portfolio and Production Readiness Verdict

### 1.1 What the system is

The system is a FastAPI 0.153 / SQLAlchemy 2.1 (async, psycopg 3) e-commerce backend on PostgreSQL 15-18 and Redis 7, with Celery workers and beat. It provides:
- a catalog with variants, ETags and JSON Merge Patch;
- a Redis cart with guest-cart merge;
- orders with an explicit state machine and row-locked stock;
- Stripe payments: PaymentIntent with 3DS, hosted Checkout Sessions, refunds and disputes;
- a durable webhook pipeline (store, enqueue, `SKIP LOCKED` claim, retry and sweeper) and daily reconciliation;
- JWT auth with refresh-token families, a password-reset watermark and per-email lockout;
- RFC 9457 problem details, rate limiting and load shedding;
- GDPR consent, erasure and retention jobs.

The size is 80 app modules (18.7k lines), 16 migrations and 182 test files (5,344 tests).

### 1.2 Strengths the auditors verified

Each item below was checked by an auditor and found sound. The source sections are listed.

| Area | Verified behaviour | Source |
|---|---|---|
| Idempotency | `POST /orders` reserves the key in the database (unique partial index), and the reservation is flushed before any stock change. `/pay` and `/checkout-session` commit a `processing` row under `FOR UPDATE` before calling Stripe. Stripe keys are deterministic and namespaced per order. Replaying a key with a different body gives 422 through canonical fingerprints. | 2A §4 Q1, Q2 |
| Webhooks | The signature is verified on the raw body (two-sided timestamp check). Unique `event_id` gives dedupe. `FOR UPDATE SKIP LOCKED` claims each event once. The processed mark commits atomically with the state change. Retry backoff (≤ 3000 s) stays below the broker `visibility_timeout`. | 2A §4 Q3, Q6, Q7 |
| Money | Decimal end to end. Only 2-decimal currencies are allowed. Input is decimal strings only. Over-refunds are blocked by a DB CHECK plus the order lock. | 2A §4 Q4; 2B §5 Q7 |
| Stock | No oversell: a conditional `UPDATE … WHERE stock >= n` plus CHECK constraints. No double restock: order `FOR UPDATE` plus a status re-check. Expiry uses `SKIP LOCKED` with a re-check. | 2B §6 |
| Schema | `alembic upgrade head` on PG 16 matches the models (`compare_metadata == []`, `alembic check` clean). There is a single head and a full down/up round trip. | 2D §4 |
| Auth | Token `type` separation. Refresh tokens are registered before the old one is consumed. Reset tokens are single-use and race-safe. Login timing is uniform (dummy hash). Every admin route is guarded (checked by script). No IDOR and no mass assignment. | 2C §6, §7 |
| Errors | RFC 9457 on every app-level error path, with one exception: CORS preflight, which was discarded as Info. | 2C §5 |
| Infra | `expire_on_commit=False` and eager loads, so no `MissingGreenlet`. Sessions are always released. Redis fail-open/fail-closed semantics are consistent. The real-Redis parity suite passes 56/56. | 2D §7 |
| Hygiene | Ruff clean. Bandit: 0 High. mypy: 91 errors, all false positives or dead code, none a reachable runtime bug. Baseline suite: 5,214 passed / 109 skipped, exit 0. | 01_baseline, 2D §6 |

### 1.3 Final severity counts (after validation)

| Category | Critical | High | Medium | Low | Info | Documented trade-off | Discarded |
|---|---|---|---|---|---|---|---|
| Code, concurrency and logic (canonical IDs plus BUG-NEW-1) | 0 | **1** | **12** | **19** | — | 6 | 11 |
| Stale code comments (2D-C) | 0 | 0 | 0 | 3 (as DOC-01) | 12 | — | 3 + the `database.py` half of C19 (C18 merged into GDPR-02) |
| Documentation drift (3A plus DOC-NEW-1) | 0 | **3** | **5** | 20 | 1 | — | 4 |
| Test-suite blind spots (3B) | 0 | **1** | **7** | 6 | 2 | — | 0 |

### 1.4 Verdict

> **Ready as a portfolio and interview piece.** The architecture is deliberate and well documented. Concurrency is handled with care: idempotent money paths, locked stock, a durable webhook pipeline. The test suite is unusually strong, and the system can defend most of its design decisions.
>
> **Not ready for real-money or real-customer production** until at least the following are fixed:
> - **PAY-01**: a customer can be charged for a cancelled, restocked order, and the app can neither refund nor surface that charge.
> - **PAY-03**: an out-of-order refund event makes a refunded order shippable again.
> - **PAY-02**: lost Checkout webhooks are invisible to reconciliation.
> - **GDPR-02, GDPR-04, GDPR-05, GDPR-01, GDPR-03**: erasure can fail permanently, erasure destroys in-flight fulfilment, the data export is not implemented, PII returns after erasure, and consent is unlogged.
> - **DB-01**: a deadlock that is reported as a database outage.
> - The three High doc issues: **3A-01** (the Compose quick start cannot reach the DB), **3A-02** (corrupt backups) and **3A-07** (a false GDPR export claim).
>
> No finding is Critical. Nothing allows unauthenticated access, injection or silent mass data loss. Every High and Medium finding comes with a patch that applies cleanly. Most are small: 16 lines (PAY-03) to about 180 lines (PAY-01).

### 1.5 Prioritised fix order

| # | Fix | Why first | Effort |
|---|---|---|---|
| 1 | **PAY-01** (+ follow-up for the 2A-04 trigger) | Money taken with no in-app remedy | M |
| 2 | **PAY-03** | One-line guard; prevents shipping refunded goods | XS |
| 3 | **DB-01** | Two-word change (`key_share=True`) ×4; removes deadlock and false outage alarms | XS |
| 4 | **GDPR-02** | Erasure silently never completes at volume | S |
| 5 | **REG-01** | Any registration without `full_name` cannot check out | XS |
| 6 | **PAY-02** | Restores reconciliation for the Checkout flow | S |
| 7 | **GDPR-04, GDPR-05, GDPR-01, GDPR-03** | Compliance: erasure vs fulfilment, export, late PII, consent log | M |
| 8 | **AUTH-02, AUTH-01** | Password oracle; reset not evicting racing sessions | S |
| 9 | **DATA-01** | Order address immutability (snapshot migration later) | S→M |
| 10 | Docs **3A-01, 3A-02, 3A-07**, then the Medium docs | Operators follow these literally | XS |
| 11 | Test guards **3B-06, 3B-07, 3B-01** (they hid REG-01, GDPR-02, DB-01) | Stop regressions | S |
| 12 | Lows (2A-06, 2B-07, 2B-08, 2D-04/05/06, OBS-01, BUG-NEW-1, …) | Hardening | S each |

---

## 2. Verified Code, Concurrency and Logic Findings

### 2.0 How the evidence and patches were produced

**Proof.** Each test in `tests/audit_verification/` asserts the *current, faulty* behaviour, so **PASSED = defect reproduced**. Three runs are cited:
- **SQLite run.** From `04_verification_results.md` §1: 29 passed, 4 skipped, exit 0. The orchestrator re-ran it after adding the BUG-NEW-1 test: `uv run pytest tests/audit_verification -q -p no:cacheprovider` gave **30 passed, 4 skipped, exit 0**.
- **PG16 run.** The independent orchestrator re-run in `.cloud_audit/_pytest_audit_verification_pg16.log`: **34 passed in 7.54 s, `EXIT=0`**.
- **PG16 server log.** `.cloud_audit/_pg16_server_log_excerpt.log` contains `ERROR:  deadlock detected` (×2) and `FATAL:  terminating connection due to idle-in-transaction timeout`.

**Patches.** Every diff below was written against the current tree, saved in the scratch directory, and checked:

1. **`git apply --check` against HEAD, each patch on its own: 23 / 23 pass** (marked ✅ `apply-check`).
2. **All 23 also apply in sequence** in the order listed in Appendix B, with no conflicts.
3. **Behavioural check (beyond what was asked).** All 23 were applied to a scratch copy of the tree.
   - `ruff check app` passes.
   - The full fast suite (`pytest tests/ -n 4 -m "not timing_sensitive"`) gave 5,217 passed and 28 failed.
   - **All 28 failures are expected.** 17 are SQLite audit tests that now fail because their defect is fixed. 11 are existing tests that pin the old behaviour (listed per finding under "Tests to update").
   - On a throwaway **PostgreSQL 16.15** cluster (since deleted), the PG audit tests for **DB-01 (×2) and 2B-08 flipped to failing**, meaning they are fixed.
   - A realistic PG probe (100 due accounts, a 50 ms hash stub, a 2 s idle timeout) **fails on HEAD with `IdleInTransactionSessionTimeout`** and **passes in 0.15 s with the GDPR-02 patch**.

---

### 2.1 HIGH

#### PAY-01: A late charge or a second charge on an expired, cancelled or superseded order can be neither refunded nor seen in the app
*Merged: 2A-01 (High), 2B-03 (High, duplicate), 2A-04 (Medium, second trigger). Validator: CONFIRMED, High.*

**Code (current line numbers):**

| Where | What |
|---|---|
| `app/tasks.py:479-499` | `expire_unpaid_orders` picks candidates by status, payment status and age only. It ignores `processing`/`requires_action`/`unknown` attempts, then calls `cancel_order_and_restock`. |
| `app/api/v1/orders.py:557` | `cancel_own_order` checks only `is_unpaid_and_new`, with no in-flight check. |
| `app/services/payment/live_stripe_provider.py:347-367` | The Checkout Session is created **without `expires_at`**, so Stripe's default 24 h applies. The order lives 30 min (`config.py:414`). `expire_checkout_session` (`base.py:138`) has no caller. |
| `app/api/v1/orders.py:919-926` | `_guard_no_attempt_in_flight` stops blocking after `PAYMENT_ATTEMPT_STALE_AFTER_MINUTES` (5). A still-payable session or 3DS intent then lets a second attempt start (2A-04). |
| `app/api/v1/orders.py:765-772` | The late success is only flagged (`requires_manual_review`). |
| `app/api/v1/orders.py:1540`, `:1556-1559` | `/refund` refuses the order, because it is not PAID and it skips flagged payments. No admin endpoint lists flagged payments. Reconciliation sees succeeded/succeeded and stays silent. |

**Failure scenario:**
1. A customer orders the last unit (stock 1→0) and starts `/pay` with 3DS, or opens a hosted Checkout.
2. At T+30-35 min the sweep cancels the order and restocks, and another customer buys the unit.
3. The first customer completes 3DS, or pays on the hosted page, which is valid for 24 h. Stripe captures the money and the webhook flags the attempt.
4. Admin `/refund` gives **409**, reconciliation opens **0** discrepancies, and the only trace is a WARNING log line.
5. The same unrefundable, flagged charge arises when a second Checkout Session starts more than 5 min after the first and both are paid.

**Proof:**
```
tests/audit_verification/test_cloud_audit.py::test_PAY01_late_3ds_charge_on_expired_order_is_unrefundable_and_unreported PASSED
tests/audit_verification/test_cloud_audit.py::test_PAY01_customer_cancel_ignores_in_flight_3ds_attempt PASSED
tests/audit_verification/test_cloud_audit.py::test_PAY01_second_checkout_session_after_stale_window_double_charges PASSED
```
All three pass on SQLite and in the PG16 re-run (`_pytest_audit_verification_pg16.log`, lines 1-3).

**Fix (✅ `apply-check`, 7 files).**
- Add a `CHECKOUT_SESSION_EXPIRE_MINUTES` setting (default 60; Stripe allows 30 min to 24 h).
- Pass it to Stripe as a **deterministic** `expires_at`: the attempt's `created_at` plus the setting. A replay with the same Stripe idempotency key therefore sends identical parameters.
- Add a shared `has_live_payment_attempt()`. The expiry sweep skips an order with a live attempt younger than that window, and the customer cancel answers 409 with `Retry-After: 60`.

```diff
diff --git a/app/api/v1/orders.py b/app/api/v1/orders.py
--- a/app/api/v1/orders.py
+++ b/app/api/v1/orders.py
@@ -22,7 +22,7 @@ from sqlalchemy.exc import IntegrityError
 from sqlalchemy.ext.asyncio import AsyncSession
 from sqlalchemy.orm import Session, joinedload, selectinload
 
-from app.core.clock import utc_now
+from app.core.clock import as_utc, utc_now
 from app.core.config import settings
 from app.core.dependencies import AdminUser, Cache, CurrentUser, DbSession, Provider
 from app.core.errors import Problem, ProblemType
@@ -68,6 +68,7 @@ from app.services.email import (
 from app.services.orders import (
     PAID_ONLY_STATUSES,
     cancel_order_and_restock,
+    has_live_payment_attempt,
     is_unpaid_and_new,
     is_valid_status_transition,
     lock_stock_rows,
@@ -559,6 +560,12 @@ async def cancel_own_order(
             status_code=status.HTTP_409_CONFLICT,
             detail="Only a new, unpaid order can be cancelled; contact support for a paid one"
         )
+    if await db.run_sync(has_live_payment_attempt, order):
+        raise HTTPException(
+            status_code=status.HTTP_409_CONFLICT,
+            detail="A payment for this order is in progress; finish or abandon it first",
+            headers={"Retry-After": "60"},
+        )
 
     await db.run_sync(cancel_order_and_restock, order)
     restocked = products_with_cached_stock(
@@ -1449,6 +1456,8 @@ async def create_checkout_session(
             cancel_url=cancel_url,
             description=f"Order #{order.id}",
             metadata={"order_id": str(order.id)},
+            expires_at=int(as_utc(payment_row.created_at).timestamp())
+            + settings.CHECKOUT_SESSION_EXPIRE_MINUTES * 60,
         )
     except PaymentProviderTimeoutError as e:
         # Same reasoning as /pay: unknown whether Stripe actually created
diff --git a/app/core/config.py b/app/core/config.py
--- a/app/core/config.py
+++ b/app/core/config.py
@@ -412,6 +412,12 @@ class Settings(BaseSettings):
     # paying, including 3D Secure; a payment that completes after expiry is
     # flagged for manual review (a refund), not applied.
     UNPAID_ORDER_EXPIRE_MINUTES: int = Field(30, ge=1)
+    # Lifetime of a Stripe Checkout Session (expires_at; Stripe accepts 30 min
+    # to 24 h, default 24 h) and how long a payment attempt that can still be
+    # paid (processing / requires_action / unknown) keeps its order from
+    # expiring or being cancelled by the customer. Without it an order could
+    # be cancelled and restocked while its customer could still pay for it.
+    CHECKOUT_SESSION_EXPIRE_MINUTES: int = Field(60, ge=30, le=1440)
     # How long a payment attempt whose outcome is not known yet (talking to
     # the provider, in 3D Secure, an open Checkout Session) keeps another
     # attempt for the same order from starting (409). Long enough for 3D
diff --git a/app/services/orders.py b/app/services/orders.py
--- a/app/services/orders.py
+++ b/app/services/orders.py
@@ -9,11 +9,15 @@ cancellation, a customer cancelling their own unpaid order, and the expiry
 of unpaid orders all go through cancel_order_and_restock.
 """
 
+from datetime import timedelta
+
 from sqlalchemy import select, update
 from sqlalchemy.orm import Session
 
 from app.core.clock import utc_now
+from app.core.config import settings
 from app.models.order import Order
+from app.models.payment import Payment
 from app.models.product import Product, ProductVariant
 from app.schemas.order import OrderStatus, PaymentStatus
 
@@ -61,6 +65,26 @@ def is_unpaid_and_new(order: Order) -> bool:
     return order.status == OrderStatus.NEW and order.payment_status in UNPAID_PAYMENT_STATUSES
 
 
+# A payment attempt whose outcome is not known yet: it may still charge.
+LIVE_ATTEMPT_STATUSES = ("processing", "requires_action", "unknown")
+
+
+def has_live_payment_attempt(db: Session, order: Order) -> bool:
+    """True while one of the order's payment attempts can still be paid
+    (an open Checkout Session, 3D Secure, a request whose outcome is
+    unknown) and is younger than CHECKOUT_SESSION_EXPIRE_MINUTES, the
+    lifetime given to the Checkout Session at Stripe. Cancelling such an
+    order would let the customer pay for an order already restocked."""
+    started_after = utc_now() - timedelta(minutes=settings.CHECKOUT_SESSION_EXPIRE_MINUTES)
+    return db.scalars(
+        select(Payment.id).where(
+            Payment.order_id == order.id,
+            Payment.status.in_(LIVE_ATTEMPT_STATUSES),
+            Payment.created_at > started_after,
+        ).limit(1)
+    ).first() is not None
+
+
 def lock_stock_rows(db: Session, lines) -> None:
     """
     Take the row locks of every stock row the `lines` (pairs of product id
diff --git a/app/services/payment/base.py b/app/services/payment/base.py
--- a/app/services/payment/base.py
+++ b/app/services/payment/base.py
@@ -120,6 +120,7 @@ class PaymentProvider(ABC):
         cancel_url: str,
         description: str | None = None,
         metadata: dict[str, Any] | None = None,
+        expires_at: int | None = None,
     ) -> CheckoutSession:
         """Mirrors stripe.checkout.Session.create(mode="payment", line_items=[...],
         success_url=..., cancel_url=..., ...). success_url/cancel_url are
diff --git a/app/services/payment/live_stripe_provider.py b/app/services/payment/live_stripe_provider.py
--- a/app/services/payment/live_stripe_provider.py
+++ b/app/services/payment/live_stripe_provider.py
@@ -343,6 +343,7 @@ class LiveStripePaymentProvider(PaymentProvider):
         cancel_url: str,
         description: str | None = None,
         metadata: dict[str, Any] | None = None,
+        expires_at: int | None = None,
     ) -> CheckoutSession:
         params: dict[str, Any] = {
             "mode": "payment",
@@ -367,6 +368,9 @@ class LiveStripePaymentProvider(PaymentProvider):
         }
         if metadata:
             params["metadata"] = metadata
+        if expires_at is not None:
+            # Unix seconds; Stripe accepts 30 min to 24 h from now.
+            params["expires_at"] = expires_at
         try:
             session = self._client.v1.checkout.sessions.create(
                 params=params, options={"idempotency_key": idempotency_key}
diff --git a/app/services/payment/stripe_provider.py b/app/services/payment/stripe_provider.py
--- a/app/services/payment/stripe_provider.py
+++ b/app/services/payment/stripe_provider.py
@@ -386,6 +386,7 @@ class StripePaymentProvider(PaymentProvider):
         cancel_url: str,
         description: str | None = None,
         metadata: dict[str, Any] | None = None,
+        expires_at: int | None = None,
     ) -> CheckoutSession:
         # Same validate-before-touching-state ordering as create_payment_intent:
         # a structurally invalid request never "reached Stripe" and must not
diff --git a/app/tasks.py b/app/tasks.py
--- a/app/tasks.py
+++ b/app/tasks.py
@@ -32,6 +32,7 @@ from app.services.email import (
 from app.services.orders import (
     UNPAID_PAYMENT_STATUSES,
     cancel_order_and_restock,
+    has_live_payment_attempt,
     is_unpaid_and_new,
     products_with_cached_stock,
 )
@@ -496,6 +497,11 @@ def expire_unpaid_orders(db) -> int:
         if order is None or not is_unpaid_and_new(order) or order.created_at >= cutoff:
             db.rollback()
             continue
+        if has_live_payment_attempt(db, order):
+            # Left for a later run: the attempt settles (webhook) or ages
+            # out together with its Checkout Session.
+            db.rollback()
+            continue
         cancel_order_and_restock(db, order)
         # What the email needs is read before the commit: after it the
         # attributes are expired, and reading order.user would open a new
```

**Behavioural check.** After the patch, `test_PAY01_late_3ds…` and `test_PAY01_customer_cancel…` flip to failing, which means the defect is fixed.

**Tests to update** (they pin the old outcome as intended):
- `test_order_expiry.py::test_payment_completing_after_the_order_expired_is_flagged_not_applied`
- `test_payment_chain.py::test_expired_unpaid_order_and_a_late_payment[USD|EUR|GBP|PLN]`

**Not closed by this diff (follow-up, design change):**
1. **The 2A-04 trigger.** `test_PAY01_second_checkout_session…` still passes: the 5-min window still lets a second session start. `expires_at` now bounds the exposure to 60 min instead of 24 h.
   - Fix: after a success commits, call `provider.expire_checkout_session()` on the order's other open sessions in `_apply_payment_success`.
   - Add a `cancel_payment_intent` provider method for sibling `requires_action` intents.
   - I tried extending `_guard_no_attempt_in_flight` to block for the session lifetime. It broke 10 existing tests that legitimately pay through a second flow, so it was not included.
2. **A remedy for flagged charges.** Add an auto-refund task with a deterministic key `autorefund-payment-{payment.id}`, and an admin queue `GET /admin/payments?requires_manual_review=true`.
3. A retry of an `unknown` Checkout attempt more than 30 min after it started is refused by Stripe (`expires_at` must be at least 30 min ahead). It surfaces as a failed attempt; the client starts a new one. This is acceptable.

---

### 2.2 MEDIUM

#### PAY-02: Reconciliation skips Checkout attempts whose completion webhook never arrived
*2A-02, downgraded High → Medium. Stripe alerts on failing endpoints, so this is a detection gap, not silent loss.*

- **Code.** `app/services/reconciliation.py:289-291` skips every `pending-…` row, and a Checkout attempt keeps its `pending-cs-…` placeholder (`orders.py:1417`) until `checkout.session.completed` arrives. `retrieve_checkout_session` (`live_stripe_provider.py:384`) has no caller.
- **Scenario.** The webhook secret is rotated wrongly, or the endpoint is disabled. The customer pays on the hosted page. The order expires and is restocked. Daily reconciliation reports `checked=0, skipped=1` and opens no discrepancy.
- **Proof.** `test_cloud_audit.py::test_PAY02_checkout_session_without_webhook_is_skipped_by_reconciliation PASSED` (SQLite and PG16).
- **Fix (✅ `apply-check`).** For a pending row that has a `checkout_session_id`, ask Stripe for the session and record a `KIND_STATUS` discrepancy when it is `complete`/`paid`.

```diff
diff --git a/app/services/reconciliation.py b/app/services/reconciliation.py
--- a/app/services/reconciliation.py
+++ b/app/services/reconciliation.py
@@ -56,7 +56,12 @@ from app.models.payment_discrepancy import (
 )
 from app.services.payment.base import PaymentProvider
 from app.services.payment.exceptions import CardError, PaymentProviderError
-from app.services.payment.types import PaymentIntent, PaymentIntentStatus, Refund
+from app.services.payment.types import (
+    CheckoutSessionStatus,
+    PaymentIntent,
+    PaymentIntentStatus,
+    Refund,
+)
 
 logger = logging.getLogger(__name__)
 
@@ -254,6 +259,24 @@ def _ask_provider(
     return intent, provider.list_refunds(payment_intent_id)
 
 
+def _compare_session(
+    provider: PaymentProvider, session_id: str, ours_status: str
+) -> list[Difference]:
+    """A KIND_STATUS difference when Stripe says the Checkout Session was paid
+    but our attempt never learnt it (its completion webhook was lost)."""
+    try:
+        session = provider.retrieve_checkout_session(session_id)
+    except CardError as e:
+        if e.code != "resource_missing":
+            raise
+        return [Difference(KIND_PAYMENT_MISSING, "", session_id, None)]
+    if session.status == CheckoutSessionStatus.COMPLETE or session.payment_status == "paid":
+        return [Difference(
+            KIND_STATUS, "", ours_status, f"session {session.status.value}/{session.payment_status}"
+        )]
+    return []
+
+
 def reconcile_payments(
     db: Session,
     provider: PaymentProvider,
@@ -286,14 +309,24 @@ def reconcile_payments(
     consecutive_errors = 0
     for payment_id in ids[:limit]:
         payment = db.get(Payment, payment_id)
+        session_id = None
         if payment.payment_intent_id.startswith(PENDING_INTENT_ID_PREFIX):
-            result.skipped += 1
-            continue
+            if not payment.checkout_session_id:
+                result.skipped += 1
+                continue
+            # A Checkout attempt whose checkout.session.completed never
+            # arrived still looks pending here: ask Stripe about the session,
+            # the only place a lost completion webhook shows.
+            session_id, ours_status = payment.checkout_session_id, payment.status
         snapshot = snapshot_payment(payment)
         # End the read transaction: no connection waits on the provider.
         db.commit()
         try:
-            intent, refunds = _ask_provider(provider, snapshot.payment_intent_id)
+            if session_id is not None:
+                found = _compare_session(provider, session_id, ours_status)
+            else:
+                intent, refunds = _ask_provider(provider, snapshot.payment_intent_id)
+                found = compare(snapshot, intent, refunds)
         except PaymentProviderError as e:
             logger.warning(
                 "Reconciliation: payment %s (%s) could not be compared: %s",
@@ -307,7 +340,7 @@ def reconcile_payments(
                 break
             continue
         consecutive_errors = 0
-        opened, resolved = record(db, payment_id, compare(snapshot, intent, refunds), now)
+        opened, resolved = record(db, payment_id, found, now)
         result.checked += 1
         result.opened += opened
         result.resolved += resolved
```

- **Tests to update.** `test_reconciliation.py::test_a_checkout_attempt_without_a_payment_intent_yet_is_skipped` and `::test_pending_checkout_sessions_skipped_and_loop_continues` both pin `skipped == 1`. Their mock sessions should now be either open (no discrepancy) or complete (one discrepancy).

#### PAY-03: An out-of-order `refund.*` event moves a refund from `succeeded` back to `pending`, so a refunded order returns to PAID and can be shipped
*2A-03, Medium.*

- **Code.** `app/services/stripe_events.py:253-257` lets any non-final status overwrite `succeeded`. `_settle_refunds` (`orders.py:1795-1804`) then returns the order to PAID, and `PUT /status processing` gives 200.
- **Scenario.** Full refund → order `refunded`. Then a stale `refund.created` snapshot with `status: pending` is applied (Stripe does not guarantee order, and the app's own retry backoff can reorder events). The order becomes `confirmed/paid`, and fulfilment can proceed.
- **Proof.** `test_cloud_audit.py::test_PAY03_stale_pending_refund_event_regresses_refunded_order_to_paid PASSED` (SQLite and PG16).
- **Fix (✅ `apply-check`; flips the test).**

```diff
diff --git a/app/services/stripe_events.py b/app/services/stripe_events.py
--- a/app/services/stripe_events.py
+++ b/app/services/stripe_events.py
@@ -252,6 +252,11 @@ def _apply_refund_object(db: Session, payment_row: Payment, refund_object: dict)
         return False
     if refund_row.status in _REFUND_STATUSES_NOT_RETURNED:
         return False
+    # Stripe does not order events: an older snapshot (pending,
+    # requires_action) must not move a succeeded refund back, which would
+    # return a refunded order to PAID. succeeded -> failed/canceled is real.
+    if refund_row.status == "succeeded" and status not in _REFUND_STATUSES_NOT_RETURNED:
+        return False
     refund_row.status = status
     if status not in _REFUND_STATUSES_NOT_RETURNED:
         return False
```

#### GDPR-02: The erasure and retention purges hash bcrypt inside one open transaction; at volume every run dies on the 30 s idle-in-transaction timeout
*2D-01 (downgraded High → Medium, because it is volume-dependent) + 2D-C18.*

- **Code.**
  - `app/tasks.py:356-372`: `_anonymise_pending` SELECTs a batch of 100, which opens the transaction. `_anonymise_account` then calls `get_password_hash()` **per account** while no statement is sent.
  - `app/core/database.py:59-61` applies `idle_in_transaction_session_timeout=30000` to every PG engine, including Celery's.
  - The comment at `tasks.py:286-289` ("~25 s: well inside task_soft_time_limit") points at the wrong limit.
- **Scenario.**
  - At cost 12, roughly 95 or more due accounts (about 50 at cost 13) exceed 30 s. PG kills the session and the batch rolls back.
  - The next day's run restarts from the same keyset, so erasures never complete.
  - The error is an `InternalError`, not an `OperationalError`, so it is not classified as an outage, and nothing alerts.
  - The validator measured, on PG16: cost 12 → 28.2 s (pass); cost 13 → **fail, 100 still pending**.
- **Proof.**
  - `test_cloud_audit.py::test_GDPR02_purge_hashes_bcrypt_inside_one_open_transaction PASSED` (SQLite and PG16).
  - `test_postgres_cloud_audit.py::test_GDPR02_purge_dies_on_idle_in_transaction_timeout PASSED` (PG16 only).
  - Server log: `FATAL:  terminating connection due to idle-in-transaction timeout`.
- **Fix (✅ `apply-check`).** One hash per batch, not one per account: the password is a random UUID nobody knows.
  - It is computed only when the batch has accounts, which keeps the existing "no hash when nothing is due" test green.
  - The remaining single hash (~0.3 s) inside the transaction has a 50-100× margin.

```diff
diff --git a/app/tasks.py b/app/tasks.py
--- a/app/tasks.py
+++ b/app/tasks.py
@@ -284,9 +284,10 @@ def send_order_status_update(order_id: int) -> None:
         send_order_status_update_email(order.user.email, order)
 
 # Accounts anonymised per transaction by purge_expired_deletion_requests.
-# Each account costs a bcrypt hash (BCRYPT_ROUNDS, ~0.25 s at 12), so a
-# batch of 100 is ~25 s: well inside task_soft_time_limit, and a run cut
-# short by it keeps every batch committed before.
+# One bcrypt hash per batch, not one per account: 100 hashes inside the
+# batch's transaction (~0.3 s each at cost 12) kept the connection idle in
+# transaction past idle_in_transaction_session_timeout (30 s), and the batch
+# rolled back on every run.
 PURGE_BATCH_SIZE = 100
 
 def purge_expired_deletion_requests(db, batch_size: int = PURGE_BATCH_SIZE) -> int:
@@ -358,8 +359,12 @@ def _anonymise_pending(db, pending, batch_size: int, due=None) -> int:
             break
         last_id = accounts[-1].id
         erased = [a for a in accounts if due is None or due(a)]
+        # A random password nobody knows: one hash per batch is as unusable
+        # as one per account. One hash (~0.3 s) inside the transaction, not
+        # one per account (100 x 0.3 s outlasted the 30 s idle timeout).
+        unusable_hash = get_password_hash(str(uuid4())) if erased else None
         for account in erased:
-            _anonymise_account(account)
+            _anonymise_account(account, unusable_hash)
         stripe_events.redact_users_events(db, [a.id for a in erased])
         purged_count += len(erased)
         db.commit()
@@ -367,9 +372,9 @@ def _anonymise_pending(db, pending, batch_size: int, due=None) -> int:
             break
     return purged_count
 
-def _anonymise_account(account) -> None:
+def _anonymise_account(account, unusable_hash: str) -> None:
     account.email = f"deleted-user-{account.id}@deleted.invalid"
-    account.hashed_password = get_password_hash(str(uuid4()))
+    account.hashed_password = unusable_hash
     account.full_name = None
     account.phone = None
     account.marketing_consent = False
```

- **Behavioural check.**
  - The SQLite structure test flips.
  - The realistic PG16 probe (100 accounts, 50 ms per hash, 2 s idle timeout) **fails on HEAD** (`IdleInTransactionSessionTimeout`) and **passes with the patch**.
  - The PG audit test `test_GDPR02_purge_dies…` still passes. Its stand-in holds even a *single* hash until the server kills the backend, so it models an infinitely slow hash. Replace it with the realistic probe above.
- **Test to update.** `test_tasks_gdpr.py::test_purge_commits_each_batch_so_a_failure_keeps_earlier_batches` counts hash calls per account; it should count per batch.

#### DB-01: `FOR UPDATE` stock locks against the FK `FOR KEY SHARE` taken at commit deadlock checkouts and cancels, and the result is reported as "database unavailable" (503)
*2B-01, Medium. PostgreSQL only.*

- **Code.**
  - `app/services/orders.py:81-91`: `lock_stock_rows` takes `FOR UPDATE` on products, then on variants.
  - `order_items` rows (`orders.py:357-364`) are INSERTed at commit (`autoflush=False`). Their FK check takes `FOR KEY SHARE` on the **product of a variant line**, *after* the variant locks. The real order is therefore products → variants → products.
  - `DeadlockDetected` is an `OperationalError`, which `database.py:140` maps to 503.
  - The same `FOR UPDATE` is used at `admin.py:257` and `admin.py:810`.
- **Scenario.** Cart A = `[variant V of P]`, cart B = `[plain P, variant V]`, or the cancel of such an order. A holds V and needs `KEY SHARE` on P; B holds P `FOR UPDATE` and waits on V. One side gets a 503.
- **Proof (PG16 only).**
  ```
  test_postgres_cloud_audit.py::test_DB01_variant_checkout_deadlocks_plain_plus_variant_checkout PASSED
  test_postgres_cloud_audit.py::test_DB01_variant_checkout_deadlocks_cancel_of_plain_plus_variant_order PASSED
  ```
  The server log shows `ERROR:  deadlock detected … Process 1252 waits for ShareLock on transaction 983; blocked by process 1247`, twice.
- **Fix (✅ `apply-check`; both PG tests flip on PG16).** `FOR NO KEY UPDATE` (`key_share=True`) still serialises stock writers but does not conflict with the FK `KEY SHARE`. The validator independently confirmed "both committed" with this variant.

```diff
diff --git a/app/api/v1/admin.py b/app/api/v1/admin.py
--- a/app/api/v1/admin.py
+++ b/app/api/v1/admin.py
@@ -254,7 +254,7 @@ async def update_product(
     if if_match is not None:
         # Hold the row until commit so the check and the write are atomic
         # (a no-op on SQLite).
-        stmt = stmt.with_for_update(of=Product)
+        stmt = stmt.with_for_update(of=Product, key_share=True)
     db_product = (await db.execute(stmt)).scalars().first()
     if not db_product:
         raise HTTPException(
@@ -807,7 +807,7 @@ async def update_product_variant(
         .limit(1)
     )
     if if_match is not None:
-        stmt = stmt.with_for_update()
+        stmt = stmt.with_for_update(key_share=True)
     db_variant = (await db.execute(stmt)).scalars().first()
     if not db_variant:
         raise HTTPException(
diff --git a/app/services/orders.py b/app/services/orders.py
--- a/app/services/orders.py
+++ b/app/services/orders.py
@@ -77,17 +77,28 @@ def lock_stock_rows(db: Session, lines) -> None:
     product's. Call it before the first stock UPDATE of the transaction;
     the UPDATEs themselves may then run in any order. SQLite ignores the
     lock, and with it the order.
+
+    FOR NO KEY UPDATE (key_share=True), not FOR UPDATE: it still serialises
+    the stock writers, but does not conflict with the FOR KEY SHARE that the
+    order_items foreign-key check takes on a variant line's product at
+    commit. With FOR UPDATE that implicit lock came after the variant locks
+    (products -> variants -> products) and deadlocked a checkout of a
+    variant against a cart or cancel holding the same product as a plain
+    line. The stock UPDATEs never change a key column.
     """
     product_ids = sorted({product_id for product_id, variant_id in lines if variant_id is None})
     variant_ids = sorted({variant_id for _, variant_id in lines if variant_id is not None})
     db.execute(
-        select(Product.id).where(Product.id.in_(product_ids)).order_by(Product.id).with_for_update()
+        select(Product.id)
+        .where(Product.id.in_(product_ids))
+        .order_by(Product.id)
+        .with_for_update(key_share=True)
     ).all()
     db.execute(
         select(ProductVariant.id)
         .where(ProductVariant.id.in_(variant_ids))
         .order_by(ProductVariant.id)
-        .with_for_update()
+        .with_for_update(key_share=True)
     ).all()
 
 
```

#### GDPR-01: Webhook payloads stored after an erasure keep the customer's PII indefinitely
*2C-01 + the `last_error` half of 2D-06. Medium.*

- **Code.**
  - `app/api/v1/orders.py:1903-1909` stores full `event.data`.
  - `redact_users_events` runs only once, at erasure (`tasks.py:363`).
  - Both purges exclude accounts that are already anonymised (`tasks.py:332-342`, `:426-433`).
  - No job deletes `webhook_events`.
- **Scenario.** A refund or dispute event (disputes can arrive up to 120 days later) with `billing_details` arrives after erasure and is never redacted. The Art. 17 erasure is silently undone.
- **Proof.** `test_cloud_audit.py::test_GDPR01_webhook_event_after_erasure_keeps_pii_forever PASSED` (SQLite and PG16).
- **Fix (✅ `apply-check`).** The daily `cleanup_inactive_accounts` task also runs `redact_late_events()`. That re-applies the existing idempotent `redact_users_events` to accounts erased within the last 180 days.
  - It lives in the task wrapper, not in `purge_expired_deletion_requests`, so the purge's statement-count and commit-count tests stay unchanged.
  - Verified with a scratch test: the event stored after erasure no longer contains the email once `tasks.redact_late_events(db)` has run.
  - The audit test calls only the purge, so it is unaffected. Update it to call the task or `redact_late_events`.
  - Optional: redact at ingest when the order's owner is already anonymised.

```diff
diff --git a/app/tasks.py b/app/tasks.py
--- a/app/tasks.py
+++ b/app/tasks.py
@@ -402,7 +402,38 @@ def _anonymise_account(account) -> None:
 )
 def cleanup_inactive_accounts() -> int:
     with task_session() as db:
-        return purge_expired_deletion_requests(db)
+        purged = purge_expired_deletion_requests(db)
+        redact_late_events(db)
+        return purged
+
+# Stripe keeps sending events about an erased customer's payments (refunds,
+# disputes up to 120 days later) with billing details in them. The erasure
+# redacts what is stored at that moment; every daily run redacts again what
+# arrived since, for this long after the erasure.
+LATE_EVENT_REDACTION_DAYS = 180
+
+def redact_late_events(db) -> int:
+    """Redacts webhook payloads stored after their customer was erased.
+    redact_users_events skips payloads already redacted, so this is cheap
+    to repeat. Commits per batch of users."""
+    since = utc_now() - timedelta(days=LATE_EVENT_REDACTION_DAYS)
+    anonymised_email = (
+        literal("deleted-user-") + cast(User.id, String) + literal("@deleted.invalid")
+    )
+    erased_ids = list(db.scalars(
+        select(User.id).where(
+            User.data_deletion_requested,
+            User.data_deletion_date >= since,
+            User.email == anonymised_email,
+        ).order_by(User.id)
+    ))
+    redacted = 0
+    for start in range(0, len(erased_ids), PURGE_BATCH_SIZE):
+        batch = erased_ids[start:start + PURGE_BATCH_SIZE]
+        redacted += stripe_events.redact_users_events(db, batch)
+        db.commit()
+    db.commit()
+    return redacted
 
 def purge_expired_retention(db, batch_size: int = PURGE_BATCH_SIZE) -> int:
     """
```

#### GDPR-04: Erasure ignores paid-but-unshipped orders and open disputes, and redacts the shipping address after 1 day
*2C-10, Medium. The patch also fixes 2D-03 (Low): SMTP inside the deletion transaction.*

- **Code.**
  - `app/services/gdpr.py:119-147` deactivates the account without any check.
  - `app/tasks.py:332-342` / `:386-397` sets every address to `REDACTED` after `DATA_DELETION_GRACE_PERIOD_DAYS = 1`, whatever the state of the orders.
- **Scenario.** A customer with a `confirmed/paid` order requests erasure. A day later the warehouse sees street `REDACTED`, and chargeback evidence is gone (Art. 17(3)(b)/(e) not applied).
- **Proof.** `test_cloud_audit.py::test_GDPR04_erasure_redacts_address_of_paid_unshipped_order PASSED` (SQLite and PG16).
- **Fix (✅ `apply-check`; flips the test).**
  1. `process_data_deletion` answers **409** while a paid order is in `confirmed/processing/sent` or a dispute is open.
  2. The purge defers such accounts (`NOT EXISTS`).
  3. The account is committed *before* any SMTP send (2D-03).

  Disputes opened *after* the request are not yet re-checked by the purge. Add the same `NOT EXISTS` on `payment_disputes` when that model is imported in `tasks.py`.

```diff
diff --git a/app/services/gdpr.py b/app/services/gdpr.py
--- a/app/services/gdpr.py
+++ b/app/services/gdpr.py
@@ -2,12 +2,17 @@ from datetime import timedelta
 
 from fastapi import HTTPException
 from fastapi.concurrency import run_in_threadpool
+from sqlalchemy import select
 from sqlalchemy.ext.asyncio import AsyncSession
 
 from app.core.clock import utc_now
 from app.core.config import settings
+from app.models.order import Order
+from app.models.payment import Payment
+from app.models.payment_ledger import DISPUTE_CLOSED_STATUSES, PaymentDispute
 from app.models.user import User
 from app.schemas.legal import ConsentUpdate
+from app.schemas.order import OrderStatus, PaymentStatus
 from app.schemas.user import GDPRExport
 
 
@@ -123,23 +128,55 @@ class GDPRService:
         """
         request_id = f"deletion_{user.id}_{utc_now().timestamp()}"
 
-        try:
-            # Send initial confirmation email
-            from app.services.email import send_gdpr_request_received
-            await run_in_threadpool(
-                send_gdpr_request_received, user.email, "deletion", request_id
+        # Art. 17(3)(b)/(e): the address and identity of a paid order not yet
+        # delivered, or of a charge under dispute, are still needed to perform
+        # the contract and to defend the claim. Erasing them after the 1-day
+        # grace period broke fulfilment and chargeback evidence.
+        open_order = await self.db.scalar(
+            select(Order.id).where(
+                Order.user_id == user.id,
+                Order.payment_status == PaymentStatus.PAID,
+                Order.status.in_(
+                    [OrderStatus.CONFIRMED, OrderStatus.PROCESSING, OrderStatus.SENT]
+                ),
+            ).limit(1)
+        )
+        open_dispute = await self.db.scalar(
+            select(PaymentDispute.id)
+            .join(Payment, Payment.id == PaymentDispute.payment_id)
+            .join(Order, Order.id == Payment.order_id)
+            .where(
+                Order.user_id == user.id,
+                PaymentDispute.status.notin_(DISPUTE_CLOSED_STATUSES),
+            ).limit(1)
+        )
+        if open_order is not None or open_dispute is not None:
+            raise HTTPException(
+                status_code=409,
+                detail=(
+                    "Erasure is deferred while an order is being fulfilled or a "
+                    "payment is disputed; request it again once it is settled"
+                ),
             )
 
+        try:
             # Mark user for deletion and deactivate immediately, as
             # an erasure request always does. The scheduled cleanup_inactive_accounts
-            # Celery task (app/tasks.py) performs the actual hard delete once
-            # DATA_DELETION_GRACE_PERIOD_DAYS has passed.
+            # Celery task (app/tasks.py) anonymises the account once
+            # DATA_DELETION_GRACE_PERIOD_DAYS has passed. Committed before any
+            # mail is sent: SMTP inside the open transaction (2D-03) could
+            # outlast idle_in_transaction_session_timeout.
             user.is_active = False
             user.data_deletion_requested = True
             user.data_deletion_date = utc_now()
 
             await self.db.commit()
 
+            from app.services.email import send_gdpr_request_received
+            await run_in_threadpool(
+                send_gdpr_request_received, user.email, "deletion", request_id
+            )
+
             # Send deletion confirmation email
             from app.services.email import send_gdpr_deletion_confirmation
             await run_in_threadpool(send_gdpr_deletion_confirmation, user.email, request_id)
diff --git a/app/tasks.py b/app/tasks.py
--- a/app/tasks.py
+++ b/app/tasks.py
@@ -338,6 +338,13 @@ def purge_expired_deletion_requests(db, batch_size: int = PURGE_BATCH_SIZE) -> i
         # produces an always-false filter; explicit `== False` is required.
         User.data_deletion_requested,
         User.data_deletion_date <= grace_cutoff,
+        # Deferred while a paid order is still being fulfilled (Art.
+        # 17(3)(b)): its shipping address is needed until delivery.
+        ~select(Order.id).where(
+            Order.user_id == User.id,
+            Order.payment_status == PaymentStatus.PAID,
+            Order.status.in_([OrderStatus.CONFIRMED, OrderStatus.PROCESSING, OrderStatus.SENT]),
+        ).exists(),
         User.email != anonymised_email,
     ).order_by(User.id)
 
```

- The 2D-03 audit test (`test_2D03_deletion_mail_is_sent_inside_the_open_transaction`) also flips.

#### GDPR-05: The Art. 15/20 data export is not implemented, but the email and `SECURITY.md:115` say it is
*2C-11, Medium. The doc side is 3A-07 (High).*

- **Code.** `app/services/gdpr.py:100-109` ("In production, the export payload would be assembled here") emails only `request_id`, `request_date` and `expires_at`. `messages.py:330` says "here is your personal data export".
- **Proof.** `test_cloud_audit.py::test_GDPR05_data_export_contains_no_personal_data PASSED` (SQLite and PG16).
- **Fix (✅ `apply-check`; flips the test). Core diff:**
  - assemble user, addresses, orders with lines, payments and refunds through bounded queries;
  - put them in `GDPRExport.data`;
  - render the JSON in the email to the account's own address.

  **Design note:** for production, store the export behind a signed, expiring link and log it in `users.data_export_requests`. That needs object storage and is outside this diff.

```diff
diff --git a/app/schemas/user.py b/app/schemas/user.py
--- a/app/schemas/user.py
+++ b/app/schemas/user.py
@@ -238,6 +238,9 @@ class GDPRExport(BaseModel):
     request_date: datetime = Field(..., description="When the export was requested")
     expires_at: datetime = Field(..., description="When the export data will be deleted")
     status: str = Field(default="processing", description="Current status of the export")
+    data: dict | None = Field(
+        default=None, description="The user's records (Art. 15/20), as JSON-ready values"
+    )
 
     model_config = ConfigDict(json_schema_extra={
             "examples": [{
diff --git a/app/services/email/messages.py b/app/services/email/messages.py
--- a/app/services/email/messages.py
+++ b/app/services/email/messages.py
@@ -1,4 +1,5 @@
 import html
+import json
 from datetime import datetime
 from typing import TYPE_CHECKING
 from urllib.parse import quote
@@ -336,6 +337,8 @@ def send_gdpr_export_email(to_email: str, export_data: "GDPRExport") -> bool:
                 <li>Expires at: {export_data.expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')}</li>
                 <li>Request ID: {export_data.request_id}</li>
             </ul>
+            <h3>Your data (JSON):</h3>
+            <pre>{html.escape(json.dumps(export_data.data or {}, indent=2))}</pre>
 
             <p>For your privacy and security:</p>
             <ul>
diff --git a/app/services/gdpr.py b/app/services/gdpr.py
--- a/app/services/gdpr.py
+++ b/app/services/gdpr.py
@@ -11,6 +11,36 @@ from app.schemas.legal import ConsentUpdate
 from app.schemas.user import GDPRExport
 
 
+def _row(obj, fields: tuple[str, ...]) -> dict:
+    """The named columns of `obj`, as JSON-ready values."""
+    out = {}
+    for name in fields:
+        value = getattr(obj, name)
+        out[name] = value.isoformat() if hasattr(value, "isoformat") else (
+            str(value) if value is not None and not isinstance(value, (bool, int, str, list, dict))
+            else value
+        )
+    return out
+
+
+_USER_FIELDS = (
+    "id", "email", "full_name", "phone", "created_at", "last_login", "is_email_verified",
+    "marketing_consent", "marketing_consent_date", "privacy_policy_accepted",
+    "privacy_policy_accepted_date", "consent_history",
+)
+_ADDRESS_FIELDS = (
+    "id", "street", "city", "state", "postal_code", "country", "address_type",
+    "delivery_phone", "delivery_instructions", "is_active", "created_at",
+)
+_ORDER_FIELDS = (
+    "id", "status", "payment_status", "total_amount", "currency", "shipping_address_id",
+    "created_at",
+)
+_ITEM_FIELDS = ("product_id", "variant_id", "quantity", "price_at_time")
+_PAYMENT_FIELDS = ("id", "order_id", "amount", "currency", "status", "created_at")
+_REFUND_FIELDS = ("payment_id", "amount", "currency", "status", "created_at")
+
+
 class GDPRService:
     # `db` is only needed by the two methods that write (update_user_consent,
     # process_data_deletion); the pure ones (get_consent_status,
@@ -78,6 +108,46 @@ class GDPRService:
                 detail="Failed to update consent preferences"
             ) from e
 
+    async def collect_export(self, user_id: int) -> dict:
+        """Art. 15/20: the user's account, addresses, orders with their
+        lines, payments and refunds. Bounded queries, eager-loaded lines."""
+        from sqlalchemy import select
+        from sqlalchemy.orm import selectinload
+
+        from app.models.address import Address
+        from app.models.order import Order
+        from app.models.payment import Payment
+        from app.models.payment_ledger import PaymentRefund
+
+        user = await self.db.get(User, user_id)
+        addresses = (await self.db.scalars(
+            select(Address).where(Address.user_id == user_id).order_by(Address.id)
+        )).all()
+        orders = (await self.db.scalars(
+            select(Order).options(selectinload(Order.items))
+            .where(Order.user_id == user_id).order_by(Order.id)
+        )).all()
+        payments = (await self.db.scalars(
+            select(Payment).join(Order, Order.id == Payment.order_id)
+            .where(Order.user_id == user_id).order_by(Payment.id)
+        )).all()
+        refunds = (await self.db.scalars(
+            select(PaymentRefund).join(Payment, Payment.id == PaymentRefund.payment_id)
+            .join(Order, Order.id == Payment.order_id)
+            .where(Order.user_id == user_id).order_by(PaymentRefund.id)
+        )).all()
+        await self.db.commit()  # no transaction stays open across the SMTP sends
+        return {
+            "user": _row(user, _USER_FIELDS),
+            "addresses": [_row(a, _ADDRESS_FIELDS) for a in addresses],
+            "orders": [
+                {**_row(o, _ORDER_FIELDS), "items": [_row(i, _ITEM_FIELDS) for i in o.items]}
+                for o in orders
+            ],
+            "payments": [_row(p, _PAYMENT_FIELDS) for p in payments],
+            "refunds": [_row(r, _REFUND_FIELDS) for r in refunds],
+        }
+
     async def process_data_export(self, user: User) -> str:
         """
         Process a GDPR data export request
@@ -97,14 +167,15 @@ class GDPRService:
             from app.services.email import send_gdpr_request_received
             await run_in_threadpool(send_gdpr_request_received, user.email, "export", request_id)
 
-            # In production, the export payload would be assembled here and
-            # stored in a secure location with a signed, expiring download
-            # link.
+            # The export goes to the account's own address, inline. A large
+            # account would rather get a signed, expiring download link.
             from app.services.email import send_gdpr_export_email
             export_data = GDPRExport(
                 request_id=request_id,
                 request_date=utc_now(),
-                expires_at=utc_now() + timedelta(hours=settings.GDPR_EXPORT_EXPIRY_HOURS)
+                expires_at=utc_now() + timedelta(hours=settings.GDPR_EXPORT_EXPIRY_HOURS),
+                status="completed",
+                data=await self.collect_export(user.id),
             )
             await run_in_threadpool(send_gdpr_export_email, user.email, export_data)
 
```

#### GDPR-03: `PATCH`/`PUT /users/me` changes marketing consent with no history entry and no date
*2C-07, Medium (Art. 7(1)).*

- **Code.** `app/schemas/user.py:159` exposes `marketing_consent`, and `app/api/v1/users.py:541-542` applies it with a raw `setattr`. Compare `gdpr.py:40-49`, which records history.
- **Proof.** `test_cloud_audit.py::test_GDPR03_patch_me_changes_marketing_consent_without_history PASSED` (SQLite and PG16).
- **Fix (✅ `apply-check`; flips the test).** Record a `consent_history` entry (with IP and UA) and `marketing_consent_date`, in the same shape `register_user` uses. The alternative is to remove the field from `UserUpdate`.

```diff
diff --git a/app/api/v1/users.py b/app/api/v1/users.py
--- a/app/api/v1/users.py
+++ b/app/api/v1/users.py
@@ -510,6 +510,7 @@ async def get_current_user_data(
 )
 async def update_current_user(
     user_in: UserUpdate,
+    request: Request,
     current_user: CurrentUser,
     db: DbSession,
 ):
@@ -538,6 +539,20 @@ async def update_current_user(
 
     try:
 
+        consent = update_data.pop("marketing_consent", None)
+        if consent is not None and consent != user.marketing_consent:
+            # Art. 7(1): a consent change must be demonstrable. Same entry
+            # shape as register_user and GDPRService.update_user_consent.
+            now = utc_now()
+            user.consent_history = (user.consent_history or []) + [{
+                "type": "marketing",
+                "granted": consent,
+                "timestamp": now.isoformat(),
+                "ip_address": request.client.host if request.client else None,
+                "user_agent": request.headers.get("user-agent"),
+            }]
+            user.marketing_consent = consent
+            user.marketing_consent_date = now
         for field, new_value in update_data.items():
             setattr(user, field, new_value)
 
```

#### AUTH-01: A password reset does not evict a login or a refresh rotation that is in flight
*2C-02 + 2C-03. Medium.*

- **Code.**
  - `app/api/v1/users.py:332-337` reads the hash, `:357-359` runs bcrypt, and `:386-388` mints tokens with no re-check.
  - `users.py:1055-1072`: reset writes the watermark **before** changing the hash, and does not revoke refresh families.
  - `app/core/security.py:527-539`: rotation checks the watermark, then mints.
- **Scenario.**
  - An attacker who knows the old password keeps one login permanently inside bcrypt (about 12 IPs at `RATE_LIMIT_AUTH`).
  - The login that is in flight when the reset lands mints tokens whose `iat` is later than the watermark.
  - The sliding refresh chain then lives on indefinitely.
- **Proof** (SQLite and PG16):
  ```
  test_cloud_audit.py::test_AUTH01_login_in_flight_during_password_reset_survives_it PASSED
  test_cloud_audit.py::test_AUTH01_refresh_rotation_in_flight_during_reset_survives_it PASSED
  ```
- **Fix (✅ `apply-check`; flips the login test).**
  - The login re-reads the hash after minting and refuses with 401 if it changed.
  - The reset writes a **second watermark after the commit** and revokes every refresh family.
  - Together these close both interleavings.
  - The rotation audit test still passes. It simulates the reset by calling `invalidate_tokens_issued_before_now` inside a stubbed `is_token_superseded`, not through `/password/reset/confirm`, so the route's second watermark and family revocation never run. Rewrite it to drive the reset route.

```diff
diff --git a/app/api/v1/users.py b/app/api/v1/users.py
--- a/app/api/v1/users.py
+++ b/app/api/v1/users.py
@@ -32,6 +32,7 @@ from app.core.security import (
     DUMMY_PASSWORD_HASH,
     RefreshTokenError,
     RevocationUnavailable,
+    async_redis_service,
     create_access_token,
     generate_email_verification_token,
     generate_password_reset_token,
@@ -392,6 +393,18 @@ async def login(
         # The password was right and nothing is wrong with the account: a
         # 503 the client retries, as for the failed-attempt count above.
         raise login_unavailable() from e
+    # A password reset that landed while bcrypt ran (the hash was read
+    # before it): these tokens would be newer than the reset's watermark and
+    # outlive the reset. Re-read after minting; a reset that commits later
+    # is caught by its second watermark (reset_password).
+    current_hash = await db.scalar(select(User.hashed_password).where(User.id == user_id))
+    await db.close()
+    if current_hash != hashed_password:
+        raise HTTPException(
+            status_code=status.HTTP_401_UNAUTHORIZED,
+            detail="Incorrect email or password",
+            headers={"WWW-Authenticate": "Bearer"},
+        )
     logger.info(f"Tokens generated for user {user_id}")
 
     # The cart the visitor filled before logging in becomes part of the
@@ -1073,6 +1086,15 @@ async def reset_password(
     if updated != 1:
         raise _invalid_reset_token()
 
+    # Evict sessions that raced the reset: a second watermark after the
+    # commit refuses tokens a login or refresh rotation minted while the
+    # hash was changing, and every refresh family is ended. Best effort:
+    # the password has already changed, the first watermark stands.
+    if not await invalidate_tokens_issued_before_now(user_id):
+        logger.error(f"Password reset of user {user_id}: second watermark not recorded")
+    if not await async_redis_service.revoke_user_refresh_families(user_id):
+        logger.error(f"Password reset of user {user_id}: refresh families not revoked")
+
     # The way out of a login refused for too many failed attempts: the
     # new password starts with a clean record - the email's own count
     # and its count at this address (other addresses' counts are their
```

#### AUTH-02: The deletion re-authentication is an unthrottled password oracle (300/min, no lockout)
*2C-04, Medium. The patch also reduces 2C-14 (export email amplification).*

- **Code.** `/legal/data-request` is missing from `_ROUTE_LIMITS` (`app/core/rate_limit.py:75-87`), and `app/api/v1/legal.py:338-343` never calls `check_login_attempt`.
- **Scenario.** With a stolen access or refresh token, an attacker gets about 432k guesses per day. A hit both reveals the password and erases the account.
- **Proof.** `test_cloud_audit.py::test_AUTH02_deletion_reauth_is_an_unthrottled_password_oracle PASSED` (SQLite and PG16).
- **Fix (✅ `apply-check`; flips the test).** Put the route under `RATE_LIMIT_AUTH`, and count deletion re-auth attempts in the same per-email lockout as `/users/login`.

```diff
diff --git a/app/api/v1/legal.py b/app/api/v1/legal.py
--- a/app/api/v1/legal.py
+++ b/app/api/v1/legal.py
@@ -7,6 +7,7 @@ from app.core.clock import utc_now
 from app.core.config import settings
 from app.core.dependencies import CurrentUser, DbSession
 from app.core.openapi import DocumentedRoute
+from app.core.rate_limit import check_login_attempt, clear_login_failures
 from app.core.security import verify_password_async
 from app.models.user import User
 from app.schemas.legal import (
@@ -314,6 +315,7 @@ async def update_user_consent(
 )
 async def request_personal_data(
     request_data: DataRequest,
+    request: Request,
     db: DbSession,
     current_user: CurrentUser
 ):
@@ -334,13 +336,17 @@ async def request_personal_data(
         request_id = await gdpr_service.process_data_export(current_user)
     elif request_data.request_type == "deletion":
         # Re-authentication. The schema has already refused a deletion
-        # request without a password (422).
+        # request without a password (422). Counted against the same
+        # failed-attempt limits as /users/login, or a stolen token would be a
+        # password oracle with no lockout.
+        await check_login_attempt(current_user.email, request)
         if not await verify_password_async(request_data.password, current_user.hashed_password):
             raise HTTPException(
                 status_code=401,
                 detail="Incorrect password",
                 headers={"WWW-Authenticate": "Bearer"},
             )
+        await clear_login_failures(current_user.email, request)
         # Deactivates the user and commits `db`, so it needs the row loaded
         # through `db` (see _load_user).
         request_id = await gdpr_service.process_data_deletion(
diff --git a/app/core/rate_limit.py b/app/core/rate_limit.py
--- a/app/core/rate_limit.py
+++ b/app/core/rate_limit.py
@@ -81,6 +81,8 @@ _ROUTE_LIMITS = {
     ("POST", "/users/verify-email"): "RATE_LIMIT_AUTH",
     ("POST", "/users/password/reset-token/validate"): "RATE_LIMIT_AUTH",
     ("POST", "/users/verify-email/resend"): "RATE_LIMIT_AUTH",
+    # Checks the account password (deletion) and sends mail (export).
+    ("POST", "/legal/data-request"): "RATE_LIMIT_AUTH",
     ("POST", "/orders"): "RATE_LIMIT_CHECKOUT",
     ("POST", "/orders/{order_id}/pay"): "RATE_LIMIT_CHECKOUT",
     ("POST", "/orders/{order_id}/checkout-session"): "RATE_LIMIT_CHECKOUT",
```

#### REG-01: Registering without `full_name` crashes the welcome/verification email, and checkout requires verification
*2C-05, Medium.*

- **Code.**
  - `app/services/email/messages.py:207` calls `html.escape(full_name)`, but `full_name` is optional (`app/schemas/user.py:92`).
  - The background task started at `users.py:213` dies with `AttributeError`.
  - `orders.py:186` refuses unverified users.
- **Proof.** `test_cloud_audit.py::test_REG01_registration_without_full_name_never_sends_verification_mail PASSED` (SQLite and PG16).
- **Fix (✅ `apply-check`; flips the test).**

```diff
diff --git a/app/services/email/messages.py b/app/services/email/messages.py
--- a/app/services/email/messages.py
+++ b/app/services/email/messages.py
@@ -194,7 +194,7 @@ def send_low_stock_alert_email(to_email: str, products: list[Product]) -> bool:
         html_content
     )
 
-def send_welcome_email(to_email: str, full_name: str, verification_token: str) -> bool:
+def send_welcome_email(to_email: str, full_name: str | None, verification_token: str) -> bool:
     verification_link = _frontend_link(VERIFY_EMAIL_PAGE_PATH, verification_token)
     # full_name is attacker-controlled (the caller-supplied name at
     # registration, app/api/v1/users.py) and was previously interpolated
@@ -203,8 +203,10 @@ def send_welcome_email(to_email: str, full_name: str, verification_token: str) -
     # body an email client renders. Escaped here at the point of use,
     # matching how every other f-string-built HTML email in this module
     # only ever interpolates server-controlled values (settings, IDs,
-    # enum .value, formatted dates).
-    safe_full_name = html.escape(full_name)
+    # enum .value, formatted dates). full_name is optional at registration
+    # (UserCreate): html.escape(None) raised, the background task died and
+    # the verification link - required for checkout - was never sent.
+    safe_full_name = html.escape(full_name or "customer")
 
     html_content = f"""
     <html>
```

#### DATA-01: Editing an address rewrites the shipping address of historical, paid and delivered orders
*2C-06, Medium.*

- **Code.** `app/models/order.py:39` (`shipping_address_id` FK, no snapshot) and `app/api/v1/users.py:777-778` (in-place `setattr`).
- **Proof.** `test_cloud_audit.py::test_DATA01_address_edit_rewrites_delivered_order PASSED` (SQLite and PG16).
- **Fix (✅ `apply-check`; flips the test).** Copy-on-write: if any order references the address, retire it and write the edit into a new row. The response carries the new id.
- **Migration note (long-term):** snapshot the `ship_to_*` columns onto `orders` at checkout. That means an Alembic migration that adds nullable columns and backfills them from `addresses`, and `OrderResponse` reading the snapshot. It also removes the GDPR-04 address coupling.

```diff
diff --git a/app/api/v1/users.py b/app/api/v1/users.py
--- a/app/api/v1/users.py
+++ b/app/api/v1/users.py
@@ -709,6 +709,12 @@ async def create_address(
             detail="This address already exists for the current user"
         ) from e
 
+# What an edit of an address that orders reference copies into its new row.
+_ADDRESS_COPIED_FIELDS = (
+    "street", "city", "state", "postal_code", "country",
+    "address_type", "delivery_phone", "delivery_instructions",
+)
+
 # PATCH shares the PUT handler: same schema, checks, side effects, response.
 @router.patch(
     "/addresses/{address_id:sint}",
@@ -758,8 +764,24 @@ async def update_address(
             detail=f"Address {address_id} is not active"
         )
 
-
-
+    # Copy-on-write: orders keep pointing at the address they were shipped
+    # to. Editing a row an order references rewrote the destination of paid
+    # and delivered orders (and their invoices); such an address is retired
+    # and the edit goes into a new row, whose id the response carries.
+    referenced = (await db.execute(
+        select(Order.id).where(Order.shipping_address_id == address.id).limit(1)
+    )).scalars().first()
+    if referenced is not None:
+        replacement = Address(
+            user_id=address.user_id,
+            **{field: getattr(address, field) for field in _ADDRESS_COPIED_FIELDS},
+            is_default=address.is_default,
+            is_active=True,
+        )
+        address.is_active = False
+        address.is_default = False
+        db.add(replacement)
+        address = replacement
 
     if address_in.is_default:
         existing_default = (await db.execute(
```

---

### 2.3 LOW

| ID | Title | File:line (current) | Failure scenario | Proof | Fix |
|---|---|---|---|---|---|
| **2A-05** | Reconciliation coverage gaps. (Downgraded from Medium; the cap is documented in `config.py:458-465`.) | `app/services/reconciliation.py:277-291` | Skipped `pending-` rows count against `RECONCILIATION_MAX_PAYMENTS`. `SoftTimeLimitExceeded` is not caught. | `test_2A05_pending_rows_consume_the_reconciliation_cap PASSED` | Filter rows that will be skipped in SQL (`~(payment_intent_id LIKE 'pending-%' AND checkout_session_id IS NULL)`). Order by a persisted `reconciled_at ASC`. Catch `SoftTimeLimitExceeded` and stop cleanly. |
| **2A-06** | `/pay` answers PAID when the success was not applied | `app/api/v1/orders.py:1292-1307` | The order is cancelled or paid by another attempt during the Stripe call, but the client is told PAID. | `test_2A06_pay_answers_paid_when_success_was_not_applied PASSED` | Diff below (✅). Update `test_payment_concurrency.py::test_pay_whose_order_is_paid_while_the_provider_call_is_in_flight_is_flagged` (now 409). |
| **2A-07** | An orphaned `processing` attempt answers 409 forever | `app/api/v1/orders.py:695-704` | A crash after the commit at `:1136` leaves the key returning 409 Retry-After indefinitely. | `test_2A07_orphaned_processing_attempt_answers_409_forever PASSED` | In `_replay_payment`, treat `processing` older than `PAYMENT_ATTEMPT_STALE_AFTER_MINUTES` like `unknown`, which means a re-send with the same deterministic Stripe keys. Set the row to `unknown` in the fresh-attempt `IdempotencyError` branch. |
| **2B-02** | The PG lock-order tests hook the sync engine, so the hold never fires | `tests/integration/test_postgres_only.py:172`, `:180` | The deadlock regression guards depend on thread timing; see 3B-01. | `test_2B02_sync_engine_hook_never_sees_route_stock_updates PASSED` | Diff below (✅). On PG16 the 3 lock-order tests pass with the hook firing; expect about 3 s per test while the barrier times out under correct locking. |
| **2B-04** | Nested merge-patch replaces objects; only the "RFC 7396" label is wrong | `app/api/v1/admin.py:286-287`, `:831-832` | `{"characteristics":{"color":…}}` drops `size`. A variant `attributes` `null` is stored as a value. | `test_2B04_merge_patch_replaces_nested_characteristics PASSED`, `test_2B04_merge_patch_stores_null_in_variant_attributes PASSED` | Either document "top-level merge only" (see 3A-11), or on PATCH apply a recursive RFC 7396 merge to dict fields from the raw body (`_merge(target, patch)`: `None` pops the key, dicts recurse). |
| **2B-07** | The order total overflows `Numeric(10,2)`, giving a 500 on PG | `app/api/v1/orders.py:366-368` | 2 × 60,000,000.00: SQLite gives 201, PG gives 500 (`numeric field overflow` in the PG log). | `test_2B07_order_total_is_not_bounded_by_its_numeric_column PASSED` | Diff below (✅; flips the test). |
| **2B-08** | A hard product delete racing a checkout gives a 500 | `app/api/v1/admin.py:339-349` | Count-then-delete. The checkout's `order_items` INSERT commits first, then FK violation → 500. | PG16: `test_2B08_hard_product_delete_racing_checkout_answers_500 PASSED` | Diff below (✅; the PG test flips on PG16). |
| **2B-09** | Guest-cart merge drops lines silently | `app/schemas/common.py:496-499`; `app/services/redis_codec.py:288-293` | More than 100 combined lines: the excess is logged, the guest key is deleted, and the user is never told. | `test_2B09_guest_cart_merge_drops_lines_silently PASSED` | Keep the unmerged remainder under the guest key and return a `cart_lines_dropped` flag on the next `GET /cart`. A small schema change. |
| **2B-11** | The expiry sweep aborts on the first error | `app/tasks.py:492-526` | One deadlock or connection error stops the whole sweep for 5 min. | `test_2B11_expiry_sweep_aborts_on_first_error PASSED` | Wrap each order's iteration in `try: … except OperationalError: db.rollback(); logger.warning(...); continue`. Not given as a diff: it re-indents the block that PAY-01 also edits. |
| **2C-12** | The deletion "confirmation" is sent at request time | `app/services/gdpr.py:143-145`; `messages.py:373` | "Your personal data has been deleted" is sent a day before the purge, and it lists retained order history. | `test_2C12_deletion_confirmation_claims_data_already_deleted PASSED` | Send "scheduled" now. Send "completed" from `purge_expired_deletion_requests`, listing what is retained and why (Art. 17(3)(b)). |
| **2C-14** | Export email amplification | `app/services/gdpr.py:98`, `:109` | Two synchronous SMTP sends per request at 300/min per user. | Plausible only; not reproduced. | Partly fixed by the AUTH-02 route limit. Also queue the mails and add a per-recipient cool-down (`mark_once`). |
| **OBS-01** | Celery worker and beat never configure the app's JSON logging | `app/main.py:26` (only caller); `app/tasks.py:10`, `:166` | Worker logs are plain text with no `request_id`/`trace_id`, and `LOG_LEVEL`/`LOG_FILE` are ignored. | `test_2D02_OBS01_importing_the_celery_app_configures_no_logging PASSED` | Diff below (✅). The audit test checks import-time state, which the signal approach does not change. Invert it with `celery.signals.setup_logging.send(sender=None)`, then assert a `JSONFormatter` handler. |
| **2D-03** | SMTP inside the deletion transaction | `app/api/v1/legal.py:346-348` → `app/services/gdpr.py:126-141` | SMTP slower than 30 s kills the connection: a 500 after the email has gone out. | `test_2D03_deletion_mail_is_sent_inside_the_open_transaction PASSED` | **Included in the GDPR-04 diff** (commit before mail; flips the test). |
| **2D-04** | `DATABASE_URI` is not URL-encoded | `app/core/config.py:208` | A password like `p@ss…` gives host `ss…@db`, so every request is a 503. | `test_2D04_database_uri_is_not_url_encoded PASSED` | Diff below (✅; flips the test). |
| **2D-05** | No DB connect timeout; `/health` has no time limit | `app/core/database.py:56-64`; `app/main.py:189` | Against a black-holed DB, connects wait for the kernel TCP timeout (about 2 min). | Read only. | Diff below (✅). |
| **2D-06** | Bound SQL parameters (PII) reach the logs and `webhook_events.last_error` | `app/core/database.py:69-79`; `app/tasks.py:152-161`; `app/services/stripe_events.py:594` | A `DataError` on a user write logs email, phone, address or hash. | Read only. | Diff below (✅). |
| **2D-07** | Dead order-email Celery tasks; email delivery is not durable | `app/tasks.py:194-284` (never enqueued; only `stripe_events.py:474` uses `apply_async`) | A crash after commit loses the order email, and the retry/dedup configuration is unused. | `test_2D07_order_email_celery_tasks_are_never_enqueued PASSED` | Either enqueue `send_order_confirmation.apply_async(args=[order.id])` after commit (replacing the `BackgroundTasks` at `orders.py:396`, `:510`, `:570`, `:781`), or delete the tasks, their routes and annotations, and their comments. |
| **DOC-01** | Operator-facing stale comments 2D-C02, C03 and C08 | `app/core/overload.py:12-22`; `.env.example:35-36`; `app/services/email/__init__.py:10` | They advise halving `MAX_IN_FLIGHT_REQUESTS`, mention a "sync pool", and claim the email default is `logging`. | Grep and read. | Diff below (✅). See §3. |
| **BUG-NEW-1** | A Redis blip during checkout answers 400 "Cart is empty"; `GET /cart` answers an empty 200 | `app/services/redis_async.py:213-222`; `app/api/v1/orders.py:211-216`; `app/api/v1/cart.py:402` | A transient Redis error reads as "no cart". Guests see an empty cart for a whole outage. | `test_BUGNEW1_redis_blip_on_cart_read_answers_400_cart_is_empty PASSED` (SQLite run of 30, and PG16 log line 30) | Diff below (✅; flips the test). Contract change: two tests pin "error → None" for the async `get_cart`: `tests/unit/test_redis_parity.py::test_a_redis_error_maps_to_the_same_result_on_both[async-…]` and `tests/integration/test_redis_hung_server.py::test_the_async_service_degrades_as_documented_when_redis_hangs`. |

**Low-severity diffs (✅ `apply-check` each):**

<details><summary>2A-06: return 409 when the success was not applied</summary>

```diff
diff --git a/app/api/v1/orders.py b/app/api/v1/orders.py
--- a/app/api/v1/orders.py
+++ b/app/api/v1/orders.py
@@ -1289,9 +1289,21 @@ async def process_payment(
             detail="The payment was not completed; please try again or use another payment method",
         )
 
-    await db.run_sync(
+    applied = await db.run_sync(
         _apply_payment_success, order, payment_row, background_tasks, current_user.email
     )
+    if not applied:
+        # The charge went through, but the order was cancelled or paid by
+        # another attempt while the provider was asked: the attempt is
+        # flagged for manual review. Telling the customer PAID contradicted
+        # GET /orders/{id}.
+        raise Problem(
+            status.HTTP_409_CONFLICT,
+            detail=(
+                "The payment was taken but the order could not be completed "
+                "(cancelled or already paid); it has been flagged for a refund"
+            ),
+        )
 
     return PaymentResponse(
         order_id=order.id,
```
</details>

<details><summary>2B-02: hook the async engine's sync engine and assert the hold fired</summary>

```diff
diff --git a/tests/integration/test_postgres_only.py b/tests/integration/test_postgres_only.py
--- a/tests/integration/test_postgres_only.py
+++ b/tests/integration/test_postgres_only.py
@@ -18,7 +18,13 @@ import time
 import uuid
 
 import pytest
-from conftest import TEST_DATABASE_URL, TestingSessionLocal, engine, requires_postgres
+from conftest import (
+    TEST_DATABASE_URL,
+    TestingSessionLocal,
+    async_engine,
+    engine,
+    requires_postgres,
+)
 from fastapi.testclient import TestClient
 from payment_support import Shop
 from sqlalchemy import create_engine, event, inspect, text
@@ -163,13 +169,16 @@ def _run_holding_after_first_stock_update(actions):
                 meeting.wait(timeout=3)
 
     def run(index):
-        # A client of its own per request: the routes are async defs on a
-        # synchronous session, so requests through one client run one after
-        # the other on its event loop and could never overlap.
+        # A client of its own per request: requests through one client run
+        # one after the other on its event loop and could never overlap.
         with TestClient(app) as own_client:
             results[index] = actions[index](own_client)
 
-    event.listen(engine, "after_cursor_execute", hold)
+    # The routes run on the AsyncSession: their statements go through
+    # async_engine's sync engine, not `engine` (a hook there never fired, and
+    # the tests depended on thread timing instead of this barrier).
+    hooked = async_engine.sync_engine
+    event.listen(hooked, "after_cursor_execute", hold)
     try:
         threads = [threading.Thread(target=run, args=(i,)) for i in range(len(actions))]
         for thread in threads:
@@ -177,8 +186,9 @@ def _run_holding_after_first_stock_update(actions):
         for thread in threads:
             thread.join(timeout=60)
     finally:
-        event.remove(engine, "after_cursor_execute", hold)
+        event.remove(hooked, "after_cursor_execute", hold)
     assert not any(thread.is_alive() for thread in threads)
+    assert len(seen) == len(actions), "the hold never fired: the interleaving was not forced"
     return results
 
 
```
</details>

<details><summary>2B-07: bound the order total to Numeric(10,2)</summary>

```diff
diff --git a/app/api/v1/orders.py b/app/api/v1/orders.py
--- a/app/api/v1/orders.py
+++ b/app/api/v1/orders.py
@@ -365,6 +365,13 @@ async def create_order(
 
         total_amount += price_at_time * item.quantity
 
+    if total_amount > Decimal("99999999.99"):  # orders.total_amount is Numeric(10, 2)
+        # Rolls back the stock decrements and the order row with it.
+        await db.rollback()
+        raise HTTPException(
+            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
+            detail="The order total exceeds the maximum of 99999999.99",
+        )
     order.total_amount = total_amount
     await db.commit()
     await _drop_product_details(redis, products_with_cached_stock(
```
</details>

<details><summary>2B-08: fall back to a soft delete on the FK race</summary>

```diff
diff --git a/app/api/v1/admin.py b/app/api/v1/admin.py
--- a/app/api/v1/admin.py
+++ b/app/api/v1/admin.py
@@ -344,9 +344,17 @@ async def delete_product(
         db_product.updated_at = utc_now()
         await db.commit()
     else:
-
         await db.delete(db_product)
-        await db.commit()
+        try:
+            await db.commit()
+        except IntegrityError:
+            # A checkout that held the product's lock while the count ran
+            # committed an order line for it: deactivate instead (as above).
+            await db.rollback()
+            db_product = await db.get(Product, product_id)
+            db_product.is_active = False
+            db_product.updated_at = utc_now()
+            await db.commit()
 
 
     await redis.invalidate_product_cache(product_id)
```
</details>

<details><summary>OBS-01: configure JSON logging in Celery processes</summary>

```diff
diff --git a/app/tasks.py b/app/tasks.py
--- a/app/tasks.py
+++ b/app/tasks.py
@@ -7,7 +7,13 @@ from uuid import uuid4
 
 from celery import Celery
 from celery.schedules import crontab
-from celery.signals import worker_process_init, worker_process_shutdown
+from celery.signals import (
+    setup_logging as configure_celery_logging,
+)
+from celery.signals import (
+    worker_process_init,
+    worker_process_shutdown,
+)
 from sqlalchemy import String, cast, create_engine, func, literal, select, update
 from sqlalchemy.exc import OperationalError
 from sqlalchemy.orm import joinedload, selectinload, sessionmaker
@@ -16,6 +22,7 @@ from app.core import celery_context, telemetry
 from app.core.clock import utc_now
 from app.core.config import settings
 from app.core.database import _build_connect_args
+from app.core.logging_config import setup_logging
 from app.core.security import get_password_hash
 from app.models.order import Order
 from app.models.order_items import OrderItem
@@ -166,6 +173,11 @@ SessionLocal = sessionmaker(autoflush=False, bind=engine)
 worker_process_init.connect(lambda **_: telemetry.setup(engines=[engine]), weak=False)
 worker_process_shutdown.connect(lambda **_: telemetry.shutdown(), weak=False)
 
+# The worker and beat log like the API: JSON lines with request_id/trace_id
+# (restored by app/core/celery_context.py), LOG_LEVEL and LOG_FILE honoured.
+# A receiver on this signal also stops Celery installing its own handlers.
+configure_celery_logging.connect(lambda **_: setup_logging(), weak=False)
+
 @contextmanager
 def task_session():
     # Every task must run its queries inside `with task_session() as db:`.
```
</details>

<details><summary>2D-04: build DATABASE_URI with URL.create</summary>

```diff
diff --git a/app/core/config.py b/app/core/config.py
--- a/app/core/config.py
+++ b/app/core/config.py
@@ -205,7 +205,16 @@ class Settings(BaseSettings):
             return v
         password = info.data.get("POSTGRES_PASSWORD")
         password = password.get_secret_value() if password is not None else None
-        return f"postgresql+psycopg://{info.data.get('POSTGRES_USER')}:{password}@{info.data.get('POSTGRES_SERVER')}/{info.data.get('POSTGRES_DB')}"
+        # URL.create quotes the user and password: a generated password with
+        # "@", "/" or "%" used to become part of the host or be decoded.
+        from sqlalchemy.engine import URL
+        return URL.create(
+            "postgresql+psycopg",
+            username=info.data.get("POSTGRES_USER"),
+            password=password,
+            host=info.data.get("POSTGRES_SERVER"),
+            database=info.data.get("POSTGRES_DB"),
+        ).render_as_string(hide_password=False)
 
     REDIS_HOST: str
     REDIS_PORT: int = 6379
```
</details>

<details><summary>2D-05: connect_timeout and a bounded /health</summary>

```diff
diff --git a/app/core/database.py b/app/core/database.py
--- a/app/core/database.py
+++ b/app/core/database.py
@@ -59,7 +59,10 @@ def _build_connect_args(database_uri: str) -> dict:
             "options": (
                 "-c idle_in_transaction_session_timeout=30000 "
                 "-c statement_timeout=60000"
-            )
+            ),
+            # libpq waits for the kernel's TCP timeout (about 2 min) on a
+            # black-holed host without it; pool_timeout does not cover it.
+            "connect_timeout": 5,
         }
     return {}
 
diff --git a/app/main.py b/app/main.py
--- a/app/main.py
+++ b/app/main.py
@@ -186,7 +186,10 @@ async def health_check(request: Request):
     Public and not rate limited, so it says nothing about the deployment:
     no version, no environment, no error text."""
     logger.debug("Health check endpoint called")
-    database_up, redis_up = await asyncio.gather(probes.database_is_up(), probes.redis_is_up())
+    database_up, redis_up = await asyncio.gather(
+        probes.database_is_up(probes.READINESS_TIMEOUT_SECONDS),
+        probes.redis_is_up(probes.READINESS_TIMEOUT_SECONDS),
+    )
     health_status = {
         "status": "healthy" if database_up and redis_up else "degraded",
         "services": {
```
</details>

<details><summary>2D-06: hide_parameters on every engine</summary>

```diff
diff --git a/app/core/database.py b/app/core/database.py
--- a/app/core/database.py
+++ b/app/core/database.py
@@ -75,7 +75,10 @@ _engine_kwargs = dict(
     connect_args=_connect_args,
     echo=settings.SQL_DEBUG,
     echo_pool=False,
-    logging_name=None
+    logging_name=None,
+    # Bound values (emails, phones, addresses, hashes) stay out of exception
+    # messages, which reach the logs and webhook_events.last_error.
+    hide_parameters=not settings.SQL_DEBUG,
 )
 
 engine = create_engine(settings.DATABASE_URI.get_secret_value(), **_engine_kwargs)
diff --git a/app/tasks.py b/app/tasks.py
--- a/app/tasks.py
+++ b/app/tasks.py
@@ -157,6 +157,7 @@ engine = create_engine(
     pool_pre_ping=settings.DB_POOL_PRE_PING,
     pool_recycle=settings.DB_POOL_RECYCLE,
     echo=settings.SQL_DEBUG,
+    hide_parameters=not settings.SQL_DEBUG,
     connect_args=_build_connect_args(settings.DATABASE_URI.get_secret_value()),
 )
 SessionLocal = sessionmaker(autoflush=False, bind=engine)
```
</details>

<details><summary>DOC-01: operator-facing comments (2D-C02, C03, C08)</summary>

```diff
diff --git a/.env.example b/.env.example
--- a/.env.example
+++ b/.env.example
@@ -32,8 +32,9 @@ WORKERS=4
 # Load shedding, per worker: answer 503 instead of accepting more requests
 # than the worker can serve. Without it, overload queues up until requests
 # time out; with it, the excess is refused in a millisecond and everything
-# admitted is served at normal speed. Keep it at or below the sync pool
-# (DB_POOL_SIZE + DB_MAX_OVERFLOW). Remove the line for no limit.
+# admitted is served at normal speed. Keep it at or below the async pool
+# (DB_POOL_SIZE + DB_MAX_OVERFLOW): one connection per request. Remove the
+# line for no limit.
 MAX_IN_FLIGHT_REQUESTS=15
 
 # Largest request body in bytes; larger gets 413 before it is read (default
diff --git a/app/core/overload.py b/app/core/overload.py
--- a/app/core/overload.py
+++ b/app/core/overload.py
@@ -11,15 +11,16 @@ class InFlightLimitMiddleware:
     request in progress holds pooled database connections until its
     dependencies are torn down, and once more requests are in flight than the
     pool has connections, the event loop blocks waiting for one that only the
-    loop itself could release (measured under locust - see README, "Load
-    testing"). Nothing is answered until pool_timeout, 30 seconds later.
+    loop itself could release (measured under locust - see
+    docs/PERFORMANCE.md and docs/reports/load-testing-3x.md). Nothing is
+    answered until pool_timeout, 30 seconds later.
 
     Capping in-flight requests below what the pool can serve keeps that from
     ever happening: the request that would have tipped the worker over gets
     an immediate 503 with Retry-After instead, and everyone already inside
-    is served at normal speed. An authenticated request can hold two sync
-    connections at once (get_current_user's session and the route's own), so
-    the safe ceiling is (DB_POOL_SIZE + DB_MAX_OVERFLOW) / 2.
+    is served at normal speed. A request holds at most one async connection
+    (get_current_user and the route share the request's DbSession), so the
+    ceiling is DB_POOL_SIZE + DB_MAX_OVERFLOW of the async pool.
 
     The count is per worker process, and it covers a request for as long as
     its ASGI call lasts, which includes dependency teardown - the phase the
diff --git a/app/services/email/__init__.py b/app/services/email/__init__.py
--- a/app/services/email/__init__.py
+++ b/app/services/email/__init__.py
@@ -7,9 +7,9 @@ from app.services.email.smtp_provider import SmtpEmailProvider
 def get_email_provider() -> EmailProvider:
     """Factory - swap this to switch email providers.
 
-    EMAIL_PROVIDER=logging (the default outside production) suppresses real
-    sends and only logs, so local dev and the test suite never need a real
-    SMTP server. Set EMAIL_PROVIDER=smtp to actually send mail.
+    EMAIL_PROVIDER defaults to smtp in every environment. Set
+    EMAIL_PROVIDER=logging (as the test suite does) to suppress real sends
+    and only log, so local dev never needs a real SMTP server.
     """
     if settings.EMAIL_PROVIDER == "smtp":
         return SmtpEmailProvider()
```
</details>

<details><summary>BUG-NEW-1: a cart read during a Redis failure answers 503 with Retry-After</summary>

```diff
diff --git a/app/api/v1/cart.py b/app/api/v1/cart.py
--- a/app/api/v1/cart.py
+++ b/app/api/v1/cart.py
@@ -2,6 +2,7 @@ import logging
 from typing import Annotated
 
 from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
+from redis.exceptions import RedisError
 from sqlalchemy import select
 from sqlalchemy.ext.asyncio import AsyncSession
 
@@ -399,7 +400,11 @@ async def get_cart(
     # that merely failed to parse was replaced for good. The first add
     # creates the key (the cart mutations' _load_or_create_cart). For a guest
     # without an identifier there is not even a read.
-    return await redis.get_cart(owner) or redis.empty_cart(owner)
+    try:
+        cart = await redis.get_cart(owner)
+    except RedisError:
+        _cart_or_503(None)
+    return cart or redis.empty_cart(owner)
 
 CurrentCart = Annotated[Cart, Depends(get_cart)]
 
diff --git a/app/api/v1/orders.py b/app/api/v1/orders.py
--- a/app/api/v1/orders.py
+++ b/app/api/v1/orders.py
@@ -17,6 +17,7 @@ from fastapi import (
 )
 from fastapi import status as http_status
 from fastapi.concurrency import run_in_threadpool
+from redis.exceptions import RedisError
 from sqlalchemy import func, select, update
 from sqlalchemy.exc import IntegrityError
 from sqlalchemy.ext.asyncio import AsyncSession
@@ -208,7 +209,14 @@ async def create_order(
         ensure_same_request(existing.idempotency_fingerprint, fingerprint)
         return _created_order(request, response, existing)
 
-    cart = await redis.get_cart(current_user.id)
+    try:
+        cart = await redis.get_cart(current_user.id)
+    except RedisError as e:
+        raise HTTPException(
+            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
+            detail="Cart is temporarily unavailable, please retry",
+            headers={"Retry-After": "5"},
+        ) from e
     if not cart or not cart.items:
         raise HTTPException(
             status_code=status.HTTP_400_BAD_REQUEST,
diff --git a/app/services/redis_async.py b/app/services/redis_async.py
--- a/app/services/redis_async.py
+++ b/app/services/redis_async.py
@@ -211,6 +211,10 @@ class AsyncRedisService:
     # -- Carts -------------------------------------------------------------
 
     async def get_cart(self, user_id: CartOwnerRef) -> Cart | None:
+        """The stored cart, or None when there is none. A Redis failure is
+        logged and re-raised, not read as "no cart": checkout answered 400
+        "Cart is empty" and GET /cart an empty 200 for a cart that was only
+        unreachable. Both callers answer 503 with Retry-After."""
         owner = as_cart_owner(user_id)
         if owner.key is None:
             # A guest without an identifier: there is nothing to look up.
@@ -219,7 +223,7 @@ class AsyncRedisService:
             return codec.load_cart(owner, await self._redis.get(owner.key))
         except RedisError as e:
             self._handle_redis_error(f"get_cart: {str(e)}")
-            return None
+            raise
 
     async def update_cart(self, cart: Cart) -> bool:
         try:
```
</details>

### 2.4 Documented trade-offs (known, not defects)

| ID | Trade-off | Where documented |
|---|---|---|
| 2A-08 | The refund Idempotency-Key relies on Stripe's 24 h retention. | `idempotency.py:73-78`; LIMITATIONS "Provider-Enforced Over-Refunds" |
| 2B-05 | Absolute stock writes with an optional If-Match. The strong ETag covers volatile stock. | `admin.py:246-250` docstring; standard REST semantics |
| 2B-06 | `SENT → CANCELLED` restocks. A cancelled paid order stays refundable through `/refund`. | ARCHITECTURE state diagram; pinned by `test_order_status_machine.py:96-115` |
| 2C-09 | A benign double refresh revokes the family (reuse detection). | LIMITATIONS "Concurrent Refresh-Token Reuse Window" |
| 2C-13 | The retention purge applies only to deactivated accounts. | LIMITATIONS "Data Retention for Inactive Live Accounts"; OPERATIONS.md:146 |
| 2C-15 | Per-email lockout DoS over IPv6 /64. | `rate_limit.py:368-370` |

### 2.5 Discarded (false positives and nitpicks)

| ID | One-line reason |
|---|---|
| 2A-09 | Operator-config bounds, an ignored body field, an unreachable event-id fallback, and a zero-total product decision. None is a reachable defect. |
| 2B-10 | Live Stripe refuses to refund a disputed charge (`charge_disputed`), so "money returned twice" cannot happen. Only the mock lacks the check. |
| 2B-12 | Unlocked read of the parent product: a benign race with no integrity impact. |
| 2B-13 | Design notes, not defects. |
| 2C-08 | Starlette's stock CORS preflight 400 `text/plain`. Browsers consume it. |
| 2C-16 / 2C-17 / 2C-18 / 2C-19 | Cosmetic, hygiene preferences, latent issues in dead code, or harm to the caller only. |
| 2D-08 / 2D-09 | redis-py defaults pinned by `uv.lock`; a migration that has already run (advice only). |
| 2D-C10 / C15 / C17, and the `database.py` half of C19 | Comment nitpicks: accurate for live code, a trivial name slip, or correct as history. |

---

## 3. Stale or Inaccurate Code Comments and Docstrings

These are the confirmed 2D-C entries only (validator: confirmed by grep or by reading). None affects runtime behaviour. The 3 operator-facing entries (Low, **DOC-01**) come first and have a patch (✅ in §2.3). The rest are Info. Discarded: C10, C15, C17, and the `database.py` half of C19.

| ID | File:line (current) | Current comment (quoted) | Why it is wrong | Suggested text |
|---|---|---|---|---|
| 2D-C02 (Low) | `app/core/overload.py:12-15`, `:20-22` | "…(measured under locust - see README, "Load testing")" / "An authenticated request can hold two sync connections at once (get_current_user's session and the route's own), so the safe ceiling is (DB_POOL_SIZE + DB_MAX_OVERFLOW) / 2." | Routes share one **async** `DbSession` per request (`database.py:202-208`; `security.py:313-314`). README has no "Load testing" section. Following the `/2` advice halves capacity. It is also the source of the 3A-15 doc drift. | "…see docs/PERFORMANCE.md and docs/reports/load-testing-3x.md…" / "A request holds at most one async connection (get_current_user and the route share the request's DbSession), so the ceiling is DB_POOL_SIZE + DB_MAX_OVERFLOW of the async pool." |
| 2D-C03 (Low) | `.env.example:35-36` | "Keep it at or below the sync pool (DB_POOL_SIZE + DB_MAX_OVERFLOW)." | Requests use the async pool. The sync pool serves only the admin reconciliation thread. | "Keep it at or below the async pool (DB_POOL_SIZE + DB_MAX_OVERFLOW): one connection per request." |
| 2D-C08 (Low) | `app/services/email/__init__.py:10` | "EMAIL_PROVIDER=logging (the default outside production)" | `EMAIL_PROVIDER: str = "smtp"` in every environment (`config.py:376`). Only the tests set `logging`. | "EMAIL_PROVIDER defaults to smtp in every environment. Set EMAIL_PROVIDER=logging (as the test suite does) to suppress real sends." |
| 2D-C01 | `app/core/config.py:406` | "production requires https (check_payment_settings_for_production)." | No such function. The rule is in `check_deployment_settings` (`config.py:503-549`) and applies to every environment except development and test. | "every deployed environment requires https (check_deployment_settings)." |
| 2D-C04 | `app/core/request_context.py:14-16` | "no traceparent is sent on outgoing calls or echoed back (the spec requires neither; there is no tracing SDK in the app)" | `app/core/telemetry.py` installs the OpenTelemetry SDK. With OTel on, `requests` and Celery propagate `traceparent`. | "With tracing off no traceparent is sent; with OTEL_* set, app/core/telemetry.py propagates it (requests, Celery). It is never echoed back." |
| 2D-C05 | `app/main.py:86`; `app/core/body_limit.py:29-30` | "Registered before everything else, so it is the innermost middleware" / "It is registered innermost, see app/main.py." | `HeadMiddleware` is added first (`main.py:83-84`) and is the innermost. BodySizeLimit is second-innermost. | "Registered right after HeadMiddleware, so it sits just outside it." |
| 2D-C06 | `app/main.py:111` | "Outside everything but the security headers: a request that is going to be shed should cost nothing" | `RequestContextMiddleware` and the OTel middleware are also outside it, as `main.py:117-118` says. | "Outside everything but request-id, security headers (and OTel)…" |
| 2D-C07 | `app/services/redis_async.py:8`, `:55-57`, `:169` | "the non-blocking path for those two callers" / "Key formats written by the sync side: RedisService.add_to_blacklist…" / "Stored by RedisService.setex" | Both keys are written by `AsyncRedisService` (`security.py:584`, `:607`), and every route uses the async client. | "Key formats written by AsyncRedisService.add_to_blacklist and .setex (invalidate_tokens_issued_before_now)"; "the path for every caller on the event loop." |
| 2D-C09 | `app/services/gdpr.py:134-136` | "The scheduled cleanup_inactive_accounts Celery task (app/tasks.py) performs the actual hard delete once DATA_DELETION_GRACE_PERIOD_DAYS has passed." | The task anonymises in place and never hard-deletes (`tasks.py:301-310`, `370-397`). The GDPR-04 patch also corrects this text. | "…anonymises the account (PII scrubbed, orders kept per Art. 17(3)(b)) once…" |
| 2D-C11 | `app/services/payment/stripe_provider.py:4`, `:11` | "This project has no real Stripe account and never will (portfolio project)." / "(see tests/test_payment.py's timeout test)" | `LiveStripePaymentProvider` exists and production requires it. `stripe` is a hard dependency. The test is `tests/integration/test_payment.py`. | "The in-process mock of Stripe (the live provider is live_stripe_provider.py)… see tests/integration/test_payment.py." |
| 2D-C12 | `app/services/payment/exceptions.py:5-8`; `app/services/payment/base.py:4-6` | "We deliberately do not depend on the real `stripe` package…" / "…swapping the mock provider for the real stripe SDK later is mostly a matter of writing a thin adapter" | The SDK is a dependency, and `live_stripe_provider.py` already maps its errors onto these classes. | "Provider-neutral exceptions; live_stripe_provider.py maps the SDK's errors onto them." |
| 2D-C13 | `app/models/product.py:28-35` | "list_products … filters on Product.characteristics[key].astext, plain JSON here would raise AttributeError" | `products.py:279` uses `["value"].as_string()`. `.astext` was the old bug (`products.py:271-278`). | "JSONB on Postgres for storage/indexing; list_products uses the cross-dialect .as_string()." |
| 2D-C14 | `app/api/v1/admin.py:388-389` | "see the identical note in orders.update_order_status - this shadows the fastapi.status module" | `update_order_status` has no `status` parameter and no such note. | "Named "status" for the query param; it shadows fastapi.status here, hence the http_status alias." |
| 2D-C16 | `app/core/rate_limit.py:43`; `app/api/v1/orders.py:1179`; `app/services/gdpr.py:96`; `app/services/email/messages.py:71` | "scripts/loadtest/results/rate-limiting-research.md" / "(scripts/loadtest, FINDINGS.md)" / "scripts/loadtest/FINDINGS.md" / "scripts/loadtest/traced_celery.py" | No `scripts/` directory exists (`git ls-files`). The material is in `docs/reports/`. | Point to `docs/reports/research.md` / `docs/reports/load-testing-3x.md`, or drop the path. |
| 2D-C18 (merged into GDPR-02) | `app/tasks.py:286-289` | "Each account costs a bcrypt hash (~0.25 s at 12), so a batch of 100 is ~25 s: well inside task_soft_time_limit" | Measured 0.30 s per hash, so about 30 s. The binding limit is the 30 s idle-in-transaction timeout. | Replaced in the GDPR-02 patch: "One bcrypt hash per batch, not one per account…" |
| 2D-C19 (Celery half, merged into OBS-01) | `app/core/celery_context.py:7-8` | "are restored into the same contextvar the JSON log formatter reads" | The worker installs no JSON formatter (OBS-01). | Accurate once the OBS-01 patch is applied; keep. |

---

## 4. Documentation vs Code Discrepancies

These are the confirmed 3A items plus DOC-NEW-1, at the validator's final severities, ranked. The line numbers are the validator's corrections, re-opened against HEAD.

| ID | Sev | Doc file:line | Code file:line | Discrepancy | Exact fix |
|---|---|---|---|---|---|
| 3A-01 | **High** | `SETUP.md:87-89`; `README.md:54` | `docker-compose.yml:31-37` (and the other services' `environment:` blocks); `.env.example:57`, `:75` | Says Compose sets `POSTGRES_SERVER=db` and `REDIS_HOST=redis`. Nothing does; `.env.example` has `localhost`. Every container connects to its own loopback, so the migration fails and `/health` answers 503. | SETUP.md:87-90: "`docker-compose.yml` does not set the service host names. In `.env`, set `POSTGRES_SERVER=db` and `REDIS_HOST=redis` before `docker compose up`; the `localhost` values are for running on the host." README.md:54: `# set ENVIRONMENT, POSTGRES_SERVER=db, REDIS_HOST=redis, passwords and secrets; see SETUP.md`. (Or add both to each service's `environment:`.) |
| 3A-02 | **High** | `docs/OPERATIONS.md:124`, `:127` (and `:120` "proven during T5") | `SETUP.md:137` uses `exec -T db sh -c '…'`; T5 ran a different command (`T5-migrations-backup.md:38`) | The dump runs without `-T`, so the TTY mangles the binary `-Fc` stream. `"$POSTGRES_USER"` expands in the host shell. The DB name is hard-coded. The result is a silently corrupt or failed backup. | `docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > backup_$(date +%Y%m%d_%H%M%S).dump` and `docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exit-on-error --no-owner' < backup.dump` |
| 3A-07 | **High** (upgraded) | `SECURITY.md:115`; `README.md:26`; `docs/API.md:16` | `app/services/gdpr.py:100-109` | Claims "bounded, eager-loaded queries export … in JSON". No export is assembled (GDPR-05). | SECURITY.md:115: "**Article 15 and 20 (not implemented):** the request is recorded and acknowledged by email; no export is assembled yet; exports must be produced manually." README.md:26: "GDPR erasure, consent records, retention jobs (data export: request intake only)". Revert both once the GDPR-05 patch lands. |
| 3A-03 | Medium | `docs/API.md:105` | `app/api/v1/cart.py:602-606` (`response_model=CartResponse`, 200) | `DELETE /api/v1/cart` is listed as 204 with no body. It returns 200 with the cart, and so does `DELETE /cart/items/{id}`. | "**204 No Content:** deleting an address (`DELETE /api/v1/users/addresses/{id}`) or a product (`DELETE /api/v1/admin/products/{id}`)… `DELETE /api/v1/cart` and `DELETE /api/v1/cart/items/{product_id}` return 200 with the updated cart." |
| 3A-04 + DOC-NEW-1 | Medium | `docs/ARCHITECTURE.md:197`, `:199-200` | `app/core/idempotency.py:50-57` (`:55` = `HTTP_422_UNPROCESSABLE_CONTENT`); `app/api/v1/orders.py:203-205`, `:703`, `:731`; the other IN_USE raises at `:1091`, `:1219`, `:1477`, `:1606` have no Retry-After | A reused key with a mutated body is documented as **409**; the code returns **422** (API.md:94/109/124 already say 422). `Retry-After: 5` is only conditional. The fingerprint is operation plus validated fields, not "user ID, URL and payload". | "…returning HTTP 422 `idempotency-key-reused`… A request whose key belongs to an attempt still in flight receives HTTP 409 `idempotency-key-in-use`, with `Retry-After: 5` while that attempt is being processed." `:197`: "…a SHA-256 fingerprint of the operation and the validated request fields, stored next to the key (keys are unique per user)." |
| 3A-06 | Medium | `docs/RESILIENCE.md:80` | `app/core/security.py:289-293`; `app/core/errors.py:145` | The documented type `service-unavailable` does not exist; the response has `about:blank`. | "…HTTP 503 problem details (`type: "about:blank"`, `title: "Service Unavailable"`) with `Retry-After: 30` and the `retry_after` member…" |
| 3A-08 | Medium | `docs/API.md:98` | `app/core/security.py:390-395`; `app/api/v1/orders.py:186-189` | `account-inactive` is said to cover unverified or suspended accounts. It is used only when `is_active` is false; unverified users get a plain 403. | "`account-inactive` \| 403 \| The token is valid but the account is deactivated. (An unverified email blocks `POST /orders` with a plain 403, type `about:blank`.)" |
| 3A-09 | Medium | `docs/API.md:108` | `app/api/v1/users.py:785-831` | "Deleting a default address" is listed as a 409. It is a 204 that promotes another address. | "`409 Conflict`: … (`insufficient-stock`, `idempotency-key-in-use`, an invalid order status transition, cancelling an order that is paid or no longer new, replaying a webhook event that has not failed)." |
| 3A-05 | Low (downgraded) | `docs/ARCHITECTURE.md:66`, `:71`, `:91` | OpenAPI: `OrderCreate`, `PaymentCreate`, `RefundCreate`; `extra="forbid"` (`schemas/common.py:63`) | The sequence-diagram bodies (`address_id`, `payment_method_id`, `reason`) would be rejected with 422. | `(Idempotency-Key, shipping_address_id)`; `(Idempotency-Key, order_id, payment_method, amount, payment_method_token)`; `(Idempotency-Key, optional amount)` |
| 3A-10 | Low | `docs/API.md:128`, `:199` | `app/api/v1/orders.py:598`; `app/core/idempotency.py:18-21`, `:67-71` | The key format is `{kind}-order-{id}:{key}`, not `pay:{id}:{key}`. Payment keys never expire locally; only refund keys rely on Stripe's 24 h. | "Payment-attempt keys are stored with the attempt and never expire… forwarded to Stripe as `pay-order-{order_id}:{key}` / `checkout-order-{order_id}:{key}`. Refund keys are not stored; Stripe honours them for at least 24 h." |
| 3A-11 | Low (downgraded) | `docs/API.md:154-158` | `app/api/v1/admin.py:286-287`, `:831-832` | Labelled "RFC 7396", but nested objects are replaced, not merged (2B-04). | Append: "Merging is one level deep: an object value (`characteristics`, `attributes`) replaces the stored one whole; send the complete object." |
| 3A-12 | Low | `docs/ARCHITECTURE.md:120`; `docs/RESILIENCE.md:54` | `app/tasks.py:682-685` (`timedelta(minutes=5)`) | Says "every minute". | "every 5 minutes" |
| 3A-13 | Low | `docs/OPERATIONS.md:116` | `app/core/database.py:60-61` | Says `statement_timeout=30s`. | "`statement_timeout=60s`" |
| 3A-14 | Low | `SECURITY.md:29` | `app/core/config.py:371` (`=48`); `security.py:706`; `messages.py:219` | The verification token lifetime is given as 24 h. | "48 hours (EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS)" |
| 3A-15 | Low | `SECURITY.md:103`; `docs/PERFORMANCE.md:57` | `app/core/database.py:202-208`; `security.py:313-314`; source: `overload.py:20-22` | The stale `(pool + overflow) / 2` formula. | "Keep `MAX_IN_FLIGHT_REQUESTS` at or below `DB_POOL_SIZE + DB_MAX_OVERFLOW` (15 by default); a request uses one session for its auth dependency and its route." |
| 3A-16 | Low | `docs/ARCHITECTURE.md:87-88`; `docs/OPERATIONS.md:82`; `docs/LIMITATIONS.md:37` | `stripe_events.py:474` is the only `apply_async`; email is sent via `BackgroundTasks` (`orders.py:396`, `:510`, `:570`, `:781`) | The `send_order_confirmation` Celery path is documented but never enqueued (2D-07). | OPERATIONS.md:82: "Order emails are sent in-process after commit… not retried; a crash after commit loses the email. The `send_order_*` Celery tasks exist but are not used." ARCHITECTURE.md:87: `Worker->>SMTP: Send confirmation email (after commit)`. |
| 3A-17 | Low | `docs/ARCHITECTURE.md:227`, `:230` | `.github/workflows/ci.yml` (1 workflow, 16 jobs; no `STRIPE_*`; no chaos job) | Overstates CI: says live Stripe, chaos, and "16 parallel workflows". | "CI (one workflow, 16 jobs) runs SQLite, PostgreSQL 15/16/18 (db-sensitive), real Redis parity and 7 time zones. Live Stripe and chaos runs were manual campaign stages (T4, T8)." |
| 3A-19 | Low | `docs/TESTING.md:11`; `docs/reports/final-test-campaign.md:10` | `pytest --collect-only`: unit 2,650, integration 2,694 | Says "~4800 unit tests". | "Unit tests (~2,650)", "Integration tests (~2,700)" |
| 3A-21 | Low | `README.md:87`; `docs/TESTING.md:9`, `:137`; `final-test-campaign.md:81` | 49 tests skip without a real key | "400" or "406" live Stripe tests cannot be reproduced from the repository. | State what 406 counted, or say "49 live Stripe tests". |
| 3A-23 | Low (partly) | `.env.example:7-9`, `:158`, `:173` | `app/core/config.py:503-549` | Omits `STRIPE_SECRET_KEY` and the non-secret refusals; says "production" where staging is affected too. | Lines 7-9: add "SMTP_PASSWORD and STRIPE_SECRET_KEY must be set and not placeholders. Also refused: PAYMENT_PROVIDER other than stripe, LOG_LEVEL=DEBUG, BCRYPT_ROUNDS below 12, a non-https FRONTEND_URL." Lines 158 and 173: "staging and production". |
| 3A-24 | Low | `docs/OPERATIONS.md:91` | `app/tasks.py:579-620` | "Sales velocity and view statistics": no view statistics exist. | "Caches the 20 best-selling products of the last 30 days (quantity, revenue) in Redis `product_stats`." |
| 3A-25 | Low | `SECURITY.md:105` | `app/core/rate_limit.py:320-325` (route limits fail open); `:398-406`, `:414-420` (login counter fails closed) | Says the auth limits fail closed. | "…per-route limits (auth routes included) fail open; the failed-login counter and token revocation checks fail closed (503, Retry-After: 30)." |
| 3A-26 | Low | `docs/RESILIENCE.md:67` | `app/tasks.py:128` (`visibility_timeout: 3600`); `docker-compose.yml:69-73` | Says "requeues immediately". | "If a pool process dies the task is requeued at once; if the whole worker is killed, the broker redelivers after `visibility_timeout` (1 hour)." |
| 3A-27 | Low | `docs/ARCHITECTURE.md:106-108`; `docs/API.md:205-206` | `app/api/v1/orders.py:1913-1915` | The diagram shows `Store → Ack → Enqueue`; the code enqueues before it acks. | `Store --> Enqueue --> Ack` (a broker failure is logged; the event is still acknowledged and later swept). |
| 3A-28 | Low | `docs/LIMITATIONS.md:55` | `app/schemas/common.py:155-159` | Says `skip`/`limit`. | "Offset pagination (`page` / `size`)" |
| 3A-29 | Low | `docs/reports/load-testing-3x.md:100`, `:103`, `:104` | `grep -rw` over `tests/` finds none | Cites three test names that do not exist. | Replace them with the real names or drop the column. |
| 3A-30 | Low | `SECURITY.md:114` | `app/schemas/legal.py:44-49` (`password`, `extra="forbid"`) | Says `current_password`, which would be a 422. | "…require the account's current password in the `password` field." |
| 3A-31 | Low | `docs/ARCHITECTURE.md:197` | `app/core/idempotency.py:37-48`; `orders.py:203-205` | Fingerprint contents (see DOC-NEW-1). | Fixed by the 3A-04 row. |
| 3A-33 | Info | `.github/workflows/ci.yml:14-15`; `app/core/rate_limit.py:43`; `app/services/gdpr.py:96`; `docs/reports/T7-load.md:24`; `.env.example` (no `EMAIL_PROVIDER`) | `tests/conftest.py:90-98` (`derandomize=True`); no `scripts/`; `config.py:376` | Says "random seeds"; dangling `scripts/loadtest` references; Compose quick start tries SMTP on localhost. | Say "derandomized"; repoint the paths (see 2D-C16); document `EMAIL_PROVIDER=logging` for local use. |

**Discarded (NITPICK):**
- 3A-18: "7 domain routers" is defensible.
- 3A-20: 98.8 % vs 98.9 % coverage, 0.1 pp across versions.
- 3A-22: the floor of 5 on the halved pool.
- 3A-32: informal status names in a sequence diagram.

---

## 5. Test Suite Blind Spots and `pragma: no cover` Review

The baseline suite is strong: 5,214 passed / 109 skipped. The orchestrator's final run (`.cloud_audit/_pytest_final_full.log`, including the 30 + 4 audit tests) gave **5,244 passed, 113 skipped, `EXIT=0`**. The mutation score is 98.4 %. The problems below are about *what the suite cannot see*, not about how much it covers.

### 5.1 Confirmed 3B findings (validator severities)

| ID | Sev | Test file:line | What is wrong | Evidence (validator re-run) | Fix |
|---|---|---|---|---|---|
| 3B-06 | **High** | `tests/conftest.py:447` | The `client` fixture stubs `app.api.v1.users.send_welcome_email` for **every** HTTP test, so the message builder never runs. | A direct `send_welcome_email('a@example.com', None, 'tok')` raises `AttributeError` (`messages.py:207`). **This hid REG-01.** | Stub at the transport (`EMAIL_PROVIDER=logging` is already set), not at the builder name. Parametrize registration with and without `full_name`. |
| 3B-01 | Medium | `tests/integration/test_postgres_only.py:172`, `:180` | The deadlock-test hold is attached to the **sync** `engine`; routes run on `async_engine`, so the hook never fires. | PG16: with the code intact the 3 tests pass in 0.99 s, which means no barrier. `MUT=nolock` fails only when run alone, and under CI's `-n auto` they pass with every lock removed. **This hid DB-01** (there is also no variant scenario). | The 2B-02 diff (§2.3). Add the plain + variant scenario from DB-01 and assert the hook fired. |
| 3B-02 | Medium | `test_postgres_only.py:88-114` | `pay_waits…` passes with **every** `with_for_update()` removed: the `payments` FK `KEY SHARE` blocks anyway. | PG16 `MUT=nofu`: PASSED. | Have the holder take `FOR KEY SHARE`, then assert that `/pay` blocks before writing anything (or inspect `pg_locks`). |
| 3B-03 | Medium | `test_postgres_only.py:117-143` | The double-charge test has no server-side barrier, so detection depends on load. | Alone under `nofu`: FAILED (it detects the mutation). Under `-n 4`: plausibly passes. | Add an `after_cursor_execute` barrier on the order `SELECT … FOR UPDATE` (via `async_engine.sync_engine`). |
| 3B-04 | Medium | — (lock sites `stripe_events.py:209`, `:341`, `:585`; `admin.py:257`, `:810`, `:1050`; `services/orders.py:84-91`) | 6 or more of the 16 `FOR UPDATE` sites have no test that fails when the lock is removed. | PG16 `MUT=nofu` on the refund, dispute, ETag and replay files: **67 passed, 0 failed**. | Extend the `postgres_sql` SQL-shape pattern in `test_payment_concurrency.py` to the refund and dispute handlers and to admin If-Match writes. Note: DB-01 changes these sites to `FOR NO KEY UPDATE`, so assert `"FOR NO KEY UPDATE"` there. |
| 3B-05 | Medium | `tests/integration/test_orders.py:614`, `:637`, `:661` | `test_cancel_from_{confirmed,processing,sent}` never leave `new`: the unpaid forward transitions get 409, and nobody asserts them. | PUT spy: `confirmed→409`, `processing→409`, `sent→409`, `cancelled→200`. | Pay first, assert `_transition(...).status_code == 200` at each step, and assert `status == "sent"` before cancelling. |
| 3B-07 | Medium | `tests/conftest.py:219` (`-c statement_timeout=60000` only); `conftest.py:41` (`BCRYPT_ROUNDS=4`); `test_tasks_gdpr.py:208`, `:248`, `:262` (hash stubbed) | The test engines lack the production `idle_in_transaction_session_timeout`, and bcrypt is near-free. | **This hid GDPR-02.** | Build the test engines with `database._build_connect_args(TEST_DB_URL)`. Add the PG probe from §2.2 (100 accounts, 50 ms hash, 2 s idle timeout): it fails on HEAD and passes with the fix. |
| 3B-08 | Medium | `tests/integration/test_order_invariants.py:50-53` (strategies capped at 9,999.99) | No test reaches the `Numeric(10,2)` limit. | Probe: SQLite **201** storing 120,000,000.00; PG16 **500** `NumericValueOutOfRange`. **This hid 2B-07.** | Widen the price strategy to the schema maximum and expect 201 or 4xx, never 500, on both databases. |
| 3B-09 | Low | `tests/conftest.py:138-157` | The `db_sensitive` filename glob misses 14 DB-dependent files (`test_text_input`, `test_int_bounds`, `test_catalog_money`, `test_gdpr_flows`, `test_data_retention`, …). | They still run on PG 15 (the full-suite job), but not on PG 16/18. | Invert the rule: mark every test that uses `db_session`, `client` or `tasks_db` as db-sensitive, with opt-outs. |
| 3B-10 | Low | `test_order_invariants.py:200-212` | The stateful alphabet is one product, no variants, the success card only, no webhooks or refunds, no forward admin transitions. | Scope observation only. | Add `pay_3ds`, `complete_3ds`, `refund`, `stale_refund_event`, `advance_status` and a variant product. Add a money invariant. |
| 3B-11 | Low | `test_stripe_event_pipeline.py:307-327`, `:329+` | `SKIP LOCKED` is only checked by SQL shape and a Python fake. | — | A two-connection PG test claiming one event. |
| 3B-12 | Low | `tests/unit/test_live_stripe_provider.py:586-602`; `test_stripe_rate_limit_mapping.py:118-123`; `tests/unit/test_config_matrix.py:131-133` | A tautological tolerance spy; a comment with no matching assert; an alphanumeric URI password (**this hid 2D-04**). | — | Sign with a stale timestamp and assert the error; assert `Payment.status == "unknown"`; use `POSTGRES_PASSWORD="p@ss:w/rd%"` with `make_url(...).password`. |
| 3B-13 | Low | `ci.yml:88` (gate include list) | `expire_checkout_session` and `retrieve_checkout_session` (no app caller) and the in-app mock count toward the 100 % payment gate. | `grep`: no app callers. PAY-01/PAY-02 patches give them callers. | Exclude the mock from the gate, and add a "provider method has a non-test caller" check. |
| 3B-15 | Low | `test_response_headers.py`, `test_db_dependency_errors.py`, `test_no_blocking_on_loop.py` | Class-level mock state (`stripe_provider.py:215-220`) is not reset in these files. A hazard only; not observed. | — | Move `StripePaymentProvider.reset_mock_state()` into a root autouse fixture. |
| 3B-14 | Info | — | `pragma: no cover` review, below. | — | — |
| 3B-16 | Info | — | The per-bug cross-reference, below. | — | — |

### 5.2 SQLite vs PostgreSQL blind spots

The default run uses SQLite plus fakeredis. CI runs the full suite on PG 15 and only `db_sensitive` files on PG 16/18. The 100 % payment coverage gate is computed from the **SQLite** job.

| Behaviour | SQLite | Real PG test exists? | Verdict |
|---|---|---|---|
| `FOR UPDATE` (16 sites) | dropped | 7 sites by SQL shape; real-blocking tests are weak (3B-01/02/03) | **Gap** (3B-04) |
| FK `KEY SHARE` vs `FOR UPDATE` | none | none; it accidentally makes 3B-02 pass | **Gap: hid DB-01** |
| Deadlock 40P01 → 503 | impossible | only load-dependent tests | **Gap** |
| `SKIP LOCKED` | dropped | SQL shape plus a Python fake | Gap (3B-11) |
| `Numeric(10,2)` overflow | **not enforced** (201) | none; PG gives 500 | **Gap: hid 2B-07** |
| `idle_in_transaction_session_timeout` | n/a | not set on the test engines | **Gap: hid GDPR-02** |
| CHECK constraints | enforced | `test_postgres_only.py:236-269`, constraint names | Covered |
| `statement_timeout`, pool recovery, migrations/drift | n/a | dedicated PG tests | Covered |
| int4 bounds, NUL in text, JSON filter, ILIKE/collation | lenient | yes, but PG 15 only (3B-09) | Partial |
| `lower(email)` unique, non-ASCII | ASCII `lower()` | ASCII case only | Low |
| Redis WATCH/MULTI, Lua, TTL | fakeredis | `test_real_redis_parity.py` (56/56 on a real server) | Covered |

### 5.3 Why each confirmed bug escaped the suite

| Bug | Existing test(s) | Why it escaped |
|---|---|---|
| PAY-01 | `test_payment_chain.py:426-445`, `test_order_expiry.py:327` | They **assert the flawed outcome as intended** (cancelled plus flagged) and never check refundability or visibility. Session params (`expires_at`) are never asserted. |
| PAY-02 | `test_reconciliation.py:468-479`, `:678-687` | They **pin `skipped == 1`** as correct. |
| PAY-03 | `test_refund_lifecycle.py:179-195` | Only "failed stays failed"; no succeeded→pending sequence. |
| 2A-04 (PAY-01 trigger) | `test_payment_concurrency.py:15-23` docstring | It documents the double charge as accepted. |
| GDPR-02 | `test_tasks_gdpr.py` | bcrypt stubbed or at 4 rounds; no server idle timeout (3B-07). |
| DB-01 | `test_postgres_only.py` lock tests | Plain products only, and the hook is on the wrong engine (3B-01). |
| GDPR-01 | `test_data_retention.py` | Covers only events stored *before* erasure. |
| GDPR-04 | `test_tasks_gdpr.py` | No paid, unshipped or disputed order during erasure. |
| GDPR-05 | — | No test asserts export content. |
| GDPR-03 | `test_users.py:169`, `test_merge_patch.py:110` | Null handling only; no history assert. |
| AUTH-01 | `test_auth.py:710` | Covers tokens issued *before* the reset; no interleaving. |
| AUTH-02 | `test_rate_limit_api.py:76`; the limiter is off globally (`conftest.py:36`) | No scan of "every route that verifies a password is limited". |
| REG-01 | — | The global welcome-email stub (3B-06). |
| DATA-01 | `test_merge_patch.py:149` | Never re-reads an order after an address edit. |
| 2B-07 | `test_order_invariants.py` | Strategies are capped, and SQLite ignores the precision (3B-08). |
| 2D-04 | `test_config_matrix.py:129-133` | Alphanumeric password (3B-12). |
| BUG-NEW-1 | `test_redis_parity.py`, `test_redis_hung_server.py` | They **pin "Redis error → None"** for `get_cart` as the documented degradation; no test drives checkout through it. |

### 5.4 `pragma: no cover` and suppression review (3B-14)

- **Coverage config:** `branch = true`, `fail_under = 85`, no `exclude_lines`, and a 100 % gate on 14 payment files (`ci.yml`).
- **`pragma: no cover` in `app/`: 2 sites, both re-raises after an `IntegrityError`:**
  - `app/api/v1/orders.py:278` (`create_order` flush). The only unique index on `orders` is the idempotency key, the FKs point at rows that are never hard-deleted, and the CHECKs cannot fail at flush.
  - `app/api/v1/orders.py:1440` (`checkout-session` INSERT). The placeholder is a fresh uuid and the order is locked.
  - **Verdict:** both are unreachable today and fail safe (generic 500, no money moved). The risk is low. Caveat: both sit inside the 100 %-gated file, so a future unique index or CHECK on `orders`/`payments` would make them reachable without failing the gate.
- **Other suppressions:** `type: ignore` 0. `noqa` in app: 5, all justified. `noqa` in tests: 23; `DTZ*`/`B017` and the unselected `UP042`/`UP046`/`SIM300` are dead suppressions (Info).
- **Skips and xfails:** no `skip` and no `xfail` markers (`strict_xfail = true`). Every `skipif` is environmental.

---

## 6. 100 % File Audit Matrix

**How it was built.** A script (`matrix.py`, in the scratch dir) does the following:
- takes `git ls-files '*.py' '*.md'`;
- removes the audit artefacts (`.cloud_audit/**`, `tests/audit_verification/**`, this report, `INTERVIEW_DEFENSE_GUIDE.md`);
- marks a file as audited by a report when the report names it by path, or names it by bare filename inside an explicit scope table (2A/2B/2C §1, 2D §1 Part 1, 3B §7);
- applies the reports' explicit whole-tree statements: 2D Part 2 read every comment and docstring in all 80 `app/*.py` files and all migrations; 3A checked every root and `docs/` `.md`; 3B ran grep/AST sweeps over all 182 test files.

**Result.** 309 files, **every file has at least one auditor**.

**Honest coverage notes:**
- **4 app files were covered only by 2D's comment/docstring sweep:** `app/api/__init__.py`, `app/core/__init__.py`, `app/schemas/__init__.py`, `app/services/__init__.py`. All are trivial package files.
- **106 test files were covered only by 3B's grep/AST sweep** (mocks, `monkeypatch` targets, skip/xfail, `noqa`, sleeps, freezegun, assert-free and status-only tests), not by an in-depth read.
- 3B read 31 test files in depth and sampled or cited 45 more.

Legend:
- 2A, 2B, 2C, 2D = in scope of that code audit.
- "2D (comment sweep)" = only every comment and docstring was read.
- 3A = doc checked claim by claim; "3A (cited)" = a code or doc file 3A used as ground truth.
- "3B (in depth)", "3B (sampled/cited)", "3B (grep/AST sweep)".
- 3C = referenced (and re-read) by the interview-defense guide; the validator spot-checked 30 of its references.

| # | File | Audited by | Finding IDs in this file |
|---|---|---|---|
| 1 | `CHANGELOG.md` | 3A | — |
| 2 | `README.md` | 3A | 3A-01, 3A-07, 3A-21 |
| 3 | `SECURITY.md` | 3A | 3A-07, 3A-14, 3A-15, 3A-25, 3A-30 |
| 4 | `SETUP.md` | 3A | 3A-01 |
| 5 | `app/__init__.py` | 2D | — |
| 6 | `app/api/__init__.py` | 2D (comment sweep) | — |
| 7 | `app/api/merge_patch.py` | 2B, 2D, 3C | — |
| 8 | `app/api/v1/__init__.py` | 2D | — |
| 9 | `app/api/v1/admin.py` | 2A, 2B, 2C, 2D, 3A (cited), 3B (cited), 3C | DB-01, 2B-04, 2B-08, 2D-C14 |
| 10 | `app/api/v1/cart.py` | 2B, 2C, 2D, 3A (cited), 3C | BUG-NEW-1 |
| 11 | `app/api/v1/legal.py` | 2C, 2D | AUTH-02, 2D-03 |
| 12 | `app/api/v1/orders.py` | 2A, 2B, 2C, 2D, 3A (cited), 3B (cited), 3C | PAY-01, GDPR-01, 2A-06, 2A-07, 2B-07, BUG-NEW-1, 2D-C16 |
| 13 | `app/api/v1/products.py` | 2B, 2D | — |
| 14 | `app/api/v1/users.py` | 2B, 2C, 2D, 3A (cited), 3B (cited), 3C | AUTH-01, GDPR-03, REG-01, DATA-01 |
| 15 | `app/core/__init__.py` | 2D (comment sweep) | — |
| 16 | `app/core/body_limit.py` | 2C, 2D | 2D-C05 |
| 17 | `app/core/celery_context.py` | 2A, 2D, 3C | OBS-01 (2D-C19) |
| 18 | `app/core/clock.py` | 2C, 2D | — |
| 19 | `app/core/config.py` | 2A, 2B, 2C, 2D, 3A (cited), 3C | 2D-04, 2D-C01 |
| 20 | `app/core/currencies.py` | 2A, 2D (comment sweep) | — |
| 21 | `app/core/database.py` | 2B, 2D, 3A (cited), 3B (cited), 3C | 2D-05, 2D-06 |
| 22 | `app/core/dependencies.py` | 2C, 2D (comment sweep) | — |
| 23 | `app/core/errors.py` | 2C, 2D (comment sweep), 3A (cited) | — |
| 24 | `app/core/etag.py` | 2B, 2D (comment sweep), 3C | — |
| 25 | `app/core/forwarded.py` | 2C, 2D (comment sweep) | — |
| 26 | `app/core/head.py` | 2B, 2C, 2D (comment sweep) | — |
| 27 | `app/core/idempotency.py` | 2A, 2D (comment sweep), 3A (cited), 3C | — |
| 28 | `app/core/location.py` | 2B, 2D (comment sweep) | — |
| 29 | `app/core/logging_config.py` | 2D, 3C | — |
| 30 | `app/core/openapi.py` | 2C, 2D (comment sweep) | — |
| 31 | `app/core/overload.py` | 2C, 2D, 3A (cited), 3C | DOC-01 (2D-C02) |
| 32 | `app/core/passwords.py` | 2C, 2D (comment sweep) | — |
| 33 | `app/core/probes.py` | 2C, 2D, 3A (cited) | — |
| 34 | `app/core/rate_limit.py` | 2C, 2D, 3A (cited), 3C | AUTH-02, 2D-C16 |
| 35 | `app/core/request_context.py` | 2C, 2D | 2D-C04 |
| 36 | `app/core/security.py` | 2C, 2D, 3A (cited), 3C | AUTH-01 |
| 37 | `app/core/security_headers.py` | 2C, 2D (comment sweep), 3A (cited) | — |
| 38 | `app/core/sorting.py` | 2B, 2D (comment sweep) | — |
| 39 | `app/core/telemetry.py` | 2D | — |
| 40 | `app/core/text_input.py` | 2C, 2D | — |
| 41 | `app/main.py` | 2C, 2D, 3A (cited), 3C | 2D-05, 2D-C05, 2D-C06 |
| 42 | `app/models/__init__.py` | 2D | — |
| 43 | `app/models/address.py` | 2C, 2D | — |
| 44 | `app/models/base.py` | 2B, 2C, 2D | — |
| 45 | `app/models/category.py` | 2B, 2D | — |
| 46 | `app/models/enums.py` | 2B, 2D, 3A (cited) | — |
| 47 | `app/models/order.py` | 2A, 2B, 2C, 2D, 3B (cited), 3C | DATA-01 |
| 48 | `app/models/order_items.py` | 2A, 2B, 2D | — |
| 49 | `app/models/payment.py` | 2A, 2C, 2D, 3C | — |
| 50 | `app/models/payment_discrepancy.py` | 2A, 2D | — |
| 51 | `app/models/payment_ledger.py` | 2A, 2D | — |
| 52 | `app/models/product.py` | 2B, 2D | 2D-C13 |
| 53 | `app/models/user.py` | 2C, 2D | — |
| 54 | `app/schemas/__init__.py` | 2D (comment sweep) | — |
| 55 | `app/schemas/address.py` | 2C, 2D (comment sweep) | — |
| 56 | `app/schemas/admin.py` | 2C, 2D (comment sweep) | — |
| 57 | `app/schemas/cart.py` | 2B, 2D (comment sweep) | — |
| 58 | `app/schemas/category.py` | 2B, 2D (comment sweep) | — |
| 59 | `app/schemas/common.py` | 2B, 2C, 2D, 3A (cited) | 2B-09 |
| 60 | `app/schemas/legal.py` | 2C, 2D (comment sweep), 3A (cited) | — |
| 61 | `app/schemas/order.py` | 2A, 2B, 2D (comment sweep) | — |
| 62 | `app/schemas/product.py` | 2B, 2D (comment sweep) | — |
| 63 | `app/schemas/user.py` | 2C, 2D (comment sweep) | GDPR-03, REG-01 |
| 64 | `app/schemas/webhook_event.py` | 2A, 2C, 2D | — |
| 65 | `app/services/__init__.py` | 2D (comment sweep) | — |
| 66 | `app/services/email/__init__.py` | 2C, 2D | DOC-01 (2D-C08) |
| 67 | `app/services/email/base.py` | 2C, 2D | — |
| 68 | `app/services/email/logging_provider.py` | 2C, 2D (comment sweep) | — |
| 69 | `app/services/email/messages.py` | 2C, 2D, 3A (cited), 3B (cited) | REG-01, GDPR-05, 2C-12, 2D-C16 |
| 70 | `app/services/email/smtp_provider.py` | 2C, 2D | — |
| 71 | `app/services/gdpr.py` | 2C, 2D, 3A (cited), 3C | GDPR-04, GDPR-05, 2C-12, 2C-14, 2D-03, 2D-C09, 2D-C16 |
| 72 | `app/services/orders.py` | 2A, 2B, 2D (comment sweep), 3A (cited), 3B (cited), 3C | DB-01 |
| 73 | `app/services/payment/__init__.py` | 2A, 2D (comment sweep) | — |
| 74 | `app/services/payment/base.py` | 2A, 2D (comment sweep) | PAY-01, 3B-13, 2D-C12 |
| 75 | `app/services/payment/exceptions.py` | 2A, 2D | 2D-C12 |
| 76 | `app/services/payment/live_stripe_provider.py` | 2A, 2B, 2D, 3B (cited), 3C | PAY-01 |
| 77 | `app/services/payment/stripe_provider.py` | 2A, 2D, 3B (cited) | 2D-C11, 3B-13, 3B-15 |
| 78 | `app/services/payment/types.py` | 2A, 2D (comment sweep) | — |
| 79 | `app/services/reconciliation.py` | 2A, 2B, 2D, 3C | PAY-02, 2A-05 |
| 80 | `app/services/redis.py` | 2B, 2C, 2D, 3C | — |
| 81 | `app/services/redis_async.py` | 2B, 2C, 2D, 3C | BUG-NEW-1, 2D-C07 |
| 82 | `app/services/redis_codec.py` | 2B, 2D, 3A (cited) | 2B-09 |
| 83 | `app/services/stripe_events.py` | 2A, 2B, 2C, 2D, 3A (cited), 3B (cited), 3C | PAY-03, GDPR-01, 2D-06 |
| 84 | `app/tasks.py` | 2A, 2B, 2C, 2D, 3A (cited), 3B (cited), 3C | PAY-01, GDPR-01, GDPR-02, GDPR-04, 2B-11, OBS-01, 2D-06, 2D-07 |
| 85 | `docs/API.md` | 3A | 3A-03, 3A-08, 3A-09, 3A-10, 3A-11, 3A-27 |
| 86 | `docs/ARCHITECTURE.md` | 3A, 3C | 3A-04 + DOC-NEW-1, 3A-05, 3A-12, 3A-16, 3A-17, 3A-27, 3A-31 |
| 87 | `docs/HOW-THIS-WAS-BUILT.md` | 3A | — |
| 88 | `docs/LIMITATIONS.md` | 3A, 3C | 3A-16, 3A-28 |
| 89 | `docs/OPERATIONS.md` | 3A | 3A-02, 3A-13, 3A-16, 3A-24 |
| 90 | `docs/PERFORMANCE.md` | 2D, 3A, 3C | 3A-15 |
| 91 | `docs/RESILIENCE.md` | 3A, 3C | 3A-06, 3A-12, 3A-26 |
| 92 | `docs/TESTING.md` | 3A, 3C | 3A-19, 3A-21 |
| 93 | `docs/reports/README.md` | 3A | — |
| 94 | `docs/reports/T1-supply-chain.md` | 3A | — |
| 95 | `docs/reports/T10-mutation.md` | 3A, 3C | — |
| 96 | `docs/reports/T2-test-environment.md` | 3A | — |
| 97 | `docs/reports/T3-schemathesis.md` | 3A | — |
| 98 | `docs/reports/T4-e2e.md` | 3A | — |
| 99 | `docs/reports/T5-migrations-backup.md` | 3A | 3A-02 (evidence) |
| 100 | `docs/reports/T6-dast-owasp.md` | 3A | — |
| 101 | `docs/reports/T7-load.md` | 3A | 3A-33 |
| 102 | `docs/reports/T8-chaos.md` | 3A | — |
| 103 | `docs/reports/T9-live-server.md` | 3A | — |
| 104 | `docs/reports/daylight-saving-time.md` | 3A | — |
| 105 | `docs/reports/final-test-campaign.md` | 3A | 3A-19, 3A-21 |
| 106 | `docs/reports/fixes-4.0.x.md` | 3A | — |
| 107 | `docs/reports/load-testing-3x.md` | 2D, 3A | 3A-29 |
| 108 | `docs/reports/release-4.0.0.md` | 3A | — |
| 109 | `docs/reports/research.md` | 2D, 3A | — |
| 110 | `docs/reports/security-review-3.0.md` | 3A | — |
| 111 | `main.py` | 2C, 2D, 3A (cited) | — |
| 112 | `migrations/env.py` | 2D | — |
| 113 | `migrations/versions/2026_08_18_2342-482750d08d54_initial_schema.py` | 2D | — |
| 114 | `migrations/versions/2026_08_19_0016-5fe510d5d45e_add_product_variants.py` | 2D | — |
| 115 | `migrations/versions/2026_08_19_0852-b6d7b96c6d17_add_variant_id_to_order_items.py` | 2D | — |
| 116 | `migrations/versions/2026_08_19_1200-ed98c07e2fbc_add_checkout_session_columns_to_.py` | 2D | — |
| 117 | `migrations/versions/2026_10_20_1000-e1959653f4a2_add_currency_to_orders.py` | 2D | — |
| 118 | `migrations/versions/2026_10_20_1200-3f6c2a9d1b7e_unique_index_on_lower_email.py` | 2D | — |
| 119 | `migrations/versions/2026_10_20_2200-76d14f9f12e5_add_money_and_stock_check_constraints.py` | 2D | — |
| 120 | `migrations/versions/2026_10_21_1200-a7c3e5b19d42_add_payment_refunds.py` | 2D | — |
| 121 | `migrations/versions/2026_10_21_1300-c2d8f4a6b913_add_payment_disputes.py` | 2D | — |
| 122 | `migrations/versions/2026_10_22_1000-d4b7e1f93a62_add_payment_discrepancies.py` | 2D | — |
| 123 | `migrations/versions/2026_10_22_1100-d4e9a1c7b265_store_webhook_event_payloads.py` | 2D | — |
| 124 | `migrations/versions/2026_10_22_1200-ada27dd39537_timestamps_with_time_zone.py` | 2D | — |
| 125 | `migrations/versions/2026_10_23_1000-821e97899aca_index_foreign_keys_drop_useless_indexes.py` | 2D | — |
| 126 | `migrations/versions/2026_10_23_1100-b6f1c8d2a047_idempotency_fingerprints.py` | 2D | — |
| 127 | `migrations/versions/2026_10_24_1000-c5e2a8f4b196_constraint_naming_convention.py` | 2D | — |
| 128 | `tests/conftest.py` | 2A, 2B, 2D, 3A (cited), 3B (in depth), 3C | 3B-06, 3B-07, 3B-09 |
| 129 | `tests/integration/payment_support.py` | 2A, 3B (in depth) | — |
| 130 | `tests/integration/pg_pool_support.py` | 3B (in depth) | — |
| 131 | `tests/integration/route_support.py` | 3B (grep/AST sweep) | — |
| 132 | `tests/integration/stripe_event_support.py` | 2A, 3B (grep/AST sweep) | — |
| 133 | `tests/integration/test_account_routes.py` | 3B (grep/AST sweep) | — |
| 134 | `tests/integration/test_admin.py` | 3B (sampled/cited) | — |
| 135 | `tests/integration/test_admin_orders.py` | 3B (sampled/cited) | — |
| 136 | `tests/integration/test_admin_webhook_events.py` | 3B (grep/AST sweep) | — |
| 137 | `tests/integration/test_allow_header.py` | 3B (grep/AST sweep) | — |
| 138 | `tests/integration/test_api_docs.py` | 3B (grep/AST sweep) | — |
| 139 | `tests/integration/test_async_client.py` | 3B (grep/AST sweep) | — |
| 140 | `tests/integration/test_auth.py` | 3B (in depth) | — |
| 141 | `tests/integration/test_auth_dependencies.py` | 3B (grep/AST sweep) | — |
| 142 | `tests/integration/test_body_limit_api.py` | 3B (grep/AST sweep) | — |
| 143 | `tests/integration/test_cart.py` | 3B (sampled/cited) | — |
| 144 | `tests/integration/test_catalog_money.py` | 3B (sampled/cited) | — |
| 145 | `tests/integration/test_catalog_rules.py` | 3B (grep/AST sweep) | — |
| 146 | `tests/integration/test_catalog_search.py` | 3B (sampled/cited) | — |
| 147 | `tests/integration/test_check_constraints.py` | 2D, 3B (grep/AST sweep) | — |
| 148 | `tests/integration/test_checkout_session.py` | 3B (sampled/cited) | 3B-13 |
| 149 | `tests/integration/test_checkout_session_live_stripe.py` | 3B (sampled/cited) | — |
| 150 | `tests/integration/test_cors.py` | 3B (grep/AST sweep) | — |
| 151 | `tests/integration/test_data_retention.py` | 3B (sampled/cited) | — |
| 152 | `tests/integration/test_db_dependency_errors.py` | 3B (sampled/cited) | 3B-15 |
| 153 | `tests/integration/test_db_pool_concurrency.py` | 2D, 3B (in depth) | — |
| 154 | `tests/integration/test_db_session_scope.py` | 3B (sampled/cited) | — |
| 155 | `tests/integration/test_disputes.py` | 3B (sampled/cited) | — |
| 156 | `tests/integration/test_dst_clock.py` | 3B (sampled/cited) | — |
| 157 | `tests/integration/test_email_case.py` | 3B (grep/AST sweep) | — |
| 158 | `tests/integration/test_etag.py` | 2B, 3B (sampled/cited) | — |
| 159 | `tests/integration/test_forwarded_client.py` | 3B (grep/AST sweep) | — |
| 160 | `tests/integration/test_gdpr_flows.py` | 3B (sampled/cited) | — |
| 161 | `tests/integration/test_guest_cart.py` | 3B (grep/AST sweep) | — |
| 162 | `tests/integration/test_guest_cart_limit.py` | 3B (sampled/cited) | — |
| 163 | `tests/integration/test_head_method.py` | 3B (grep/AST sweep) | — |
| 164 | `tests/integration/test_health.py` | 3B (sampled/cited) | — |
| 165 | `tests/integration/test_idempotency_expiry.py` | 3B (grep/AST sweep) | — |
| 166 | `tests/integration/test_idempotency_fingerprint.py` | 2D, 3B (sampled/cited) | — |
| 167 | `tests/integration/test_int_bounds.py` | 3B (sampled/cited) | — |
| 168 | `tests/integration/test_legal.py` | 3B (grep/AST sweep) | — |
| 169 | `tests/integration/test_legal_async.py` | 3B (grep/AST sweep) | — |
| 170 | `tests/integration/test_list_sorting.py` | 3B (sampled/cited) | — |
| 171 | `tests/integration/test_log_privacy.py` | 3B (grep/AST sweep) | — |
| 172 | `tests/integration/test_long_password.py` | 3B (grep/AST sweep) | — |
| 173 | `tests/integration/test_merge_patch.py` | 3B (in depth) | — |
| 174 | `tests/integration/test_money_currency.py` | 3B (grep/AST sweep) | — |
| 175 | `tests/integration/test_no_blocking_on_loop.py` | 3B (sampled/cited) | 3B-15 |
| 176 | `tests/integration/test_openapi_responses.py` | 3B (grep/AST sweep) | — |
| 177 | `tests/integration/test_order_expiry.py` | 2B, 3B (in depth), 3C | pins PAY-01 outcome (3B-16) |
| 178 | `tests/integration/test_order_expiry_time.py` | 3B (grep/AST sweep) | — |
| 179 | `tests/integration/test_order_invariants.py` | 3B (in depth) | 3B-08, 3B-10, 3B-12 |
| 180 | `tests/integration/test_order_payments_view.py` | 3B (grep/AST sweep) | — |
| 181 | `tests/integration/test_order_status_machine.py` | 3B (in depth) | — |
| 182 | `tests/integration/test_orders.py` | 2B, 3B (in depth) | 3B-05 |
| 183 | `tests/integration/test_orders_last_branches.py` | 3B (grep/AST sweep) | — |
| 184 | `tests/integration/test_pagination_order.py` | 3B (sampled/cited) | — |
| 185 | `tests/integration/test_password_policy.py` | 3B (grep/AST sweep) | — |
| 186 | `tests/integration/test_payment.py` | 2D, 3B (sampled/cited) | — |
| 187 | `tests/integration/test_payment_chain.py` | 2A, 3B (in depth), 3C | pins PAY-01 outcome (3B-16) |
| 188 | `tests/integration/test_payment_concurrency.py` | 3B (in depth) | — |
| 189 | `tests/integration/test_payment_live_journey.py` | 3B (sampled/cited) | — |
| 190 | `tests/integration/test_payment_live_stripe.py` | 3B (grep/AST sweep) | — |
| 191 | `tests/integration/test_payment_paths.py` | 2D, 3B (sampled/cited) | — |
| 192 | `tests/integration/test_payment_rules.py` | 3B (sampled/cited) | — |
| 193 | `tests/integration/test_payment_state_machine.py` | 3B (grep/AST sweep) | — |
| 194 | `tests/integration/test_payment_time.py` | 3B (sampled/cited) | — |
| 195 | `tests/integration/test_payment_webhook_mutants.py` | 3B (grep/AST sweep) | — |
| 196 | `tests/integration/test_postgres_constraint_names.py` | 2D, 3B (sampled/cited) | — |
| 197 | `tests/integration/test_postgres_data_migrations.py` | 2D, 3B (sampled/cited) | — |
| 198 | `tests/integration/test_postgres_indexes.py` | 2D, 3B (in depth) | — |
| 199 | `tests/integration/test_postgres_only.py` | 2B, 2D, 3B (in depth), 3C | 2B-02 / 3B-01, 3B-02, 3B-03 |
| 200 | `tests/integration/test_postgres_pool_recovery.py` | 2D, 3B (in depth) | — |
| 201 | `tests/integration/test_postgres_statement_timeout.py` | 2D, 3B (in depth) | — |
| 202 | `tests/integration/test_problem_details.py` | 3B (grep/AST sweep) | — |
| 203 | `tests/integration/test_product_cache_freshness.py` | 3B (grep/AST sweep) | — |
| 204 | `tests/integration/test_product_images.py` | 3B (grep/AST sweep) | — |
| 205 | `tests/integration/test_product_serialization_warnings.py` | 3B (grep/AST sweep) | — |
| 206 | `tests/integration/test_products.py` | 3B (sampled/cited) | — |
| 207 | `tests/integration/test_query_counts.py` | 3B (grep/AST sweep) | — |
| 208 | `tests/integration/test_rate_limit_api.py` | 3B (in depth) | — |
| 209 | `tests/integration/test_real_redis_parity.py` | 2B, 2D, 3B (in depth) | — |
| 210 | `tests/integration/test_reconciliation.py` | 2A, 3B (in depth) | pins PAY-02 outcome (3B-16) |
| 211 | `tests/integration/test_reconciliation_admin.py` | 3B (grep/AST sweep) | — |
| 212 | `tests/integration/test_reconciliation_live_stripe.py` | 3B (grep/AST sweep) | — |
| 213 | `tests/integration/test_reconciliation_migration.py` | 2D, 3B (grep/AST sweep) | — |
| 214 | `tests/integration/test_redis_hung_server.py` | 3B (grep/AST sweep) | — |
| 215 | `tests/integration/test_refund_bookkeeping.py` | 3B (grep/AST sweep) | — |
| 216 | `tests/integration/test_refund_lifecycle.py` | 2A, 3B (in depth) | — |
| 217 | `tests/integration/test_registration_and_verification.py` | 3B (grep/AST sweep) | — |
| 218 | `tests/integration/test_removed_deprecations.py` | 3B (grep/AST sweep) | — |
| 219 | `tests/integration/test_request_id_app.py` | 3B (grep/AST sweep) | — |
| 220 | `tests/integration/test_response_headers.py` | 3B (sampled/cited) | 3B-15 |
| 221 | `tests/integration/test_response_shape.py` | 3B (grep/AST sweep) | — |
| 222 | `tests/integration/test_security_header_values.py` | 3B (grep/AST sweep) | — |
| 223 | `tests/integration/test_sessions.py` | 3B (grep/AST sweep) | — |
| 224 | `tests/integration/test_status_codes.py` | 3B (grep/AST sweep) | — |
| 225 | `tests/integration/test_stored_rows_read.py` | 3B (sampled/cited) | — |
| 226 | `tests/integration/test_strict_input.py` | 3B (grep/AST sweep) | — |
| 227 | `tests/integration/test_stripe_event_pipeline.py` | 3B (in depth) | 3B-11 |
| 228 | `tests/integration/test_tasks.py` | 3B (grep/AST sweep) | — |
| 229 | `tests/integration/test_tasks_gdpr.py` | 3B (in depth) | 3B-07 |
| 230 | `tests/integration/test_tasks_worker.py` | 3B (grep/AST sweep) | — |
| 231 | `tests/integration/test_text_input.py` | 3B (sampled/cited) | — |
| 232 | `tests/integration/test_timestamps.py` | 3B (sampled/cited) | — |
| 233 | `tests/integration/test_token_unicode.py` | 3B (grep/AST sweep) | — |
| 234 | `tests/integration/test_users.py` | 3B (sampled/cited) | — |
| 235 | `tests/integration/test_validation_errors.py` | 3B (grep/AST sweep) | — |
| 236 | `tests/integration/test_webhook_events.py` | 3B (grep/AST sweep) | — |
| 237 | `tests/integration/test_webhook_matching.py` | 3B (grep/AST sweep) | — |
| 238 | `tests/integration/test_webhook_signature_sdk.py` | 3B (in depth) | — |
| 239 | `tests/integration/time_support.py` | 3B (in depth) | — |
| 240 | `tests/unit/fake_stripe_api.py` | 3B (in depth) | — |
| 241 | `tests/unit/test_beat_schedule.py` | 3B (grep/AST sweep) | — |
| 242 | `tests/unit/test_body_limit.py` | 3B (grep/AST sweep) | — |
| 243 | `tests/unit/test_cart_properties.py` | 3B (sampled/cited) | — |
| 244 | `tests/unit/test_cart_schema.py` | 3B (grep/AST sweep) | — |
| 245 | `tests/unit/test_catalog_schemas.py` | 3B (grep/AST sweep) | — |
| 246 | `tests/unit/test_celery_config.py` | 3B (grep/AST sweep) | — |
| 247 | `tests/unit/test_celery_context.py` | 3B (sampled/cited) | — |
| 248 | `tests/unit/test_clock.py` | 3B (sampled/cited) | — |
| 249 | `tests/unit/test_config.py` | 3B (grep/AST sweep) | — |
| 250 | `tests/unit/test_config_matrix.py` | 3B (in depth) | 3B-12 |
| 251 | `tests/unit/test_config_secrets.py` | 3B (sampled/cited) | — |
| 252 | `tests/unit/test_constraint_naming.py` | 3B (grep/AST sweep) | — |
| 253 | `tests/unit/test_currencies_exhaustive.py` | 3B (grep/AST sweep) | — |
| 254 | `tests/unit/test_database.py` | 3B (grep/AST sweep) | — |
| 255 | `tests/unit/test_database_engine_config.py` | 3B (grep/AST sweep) | — |
| 256 | `tests/unit/test_docker_compose.py` | 3B (grep/AST sweep) | — |
| 257 | `tests/unit/test_dst_beat_schedule.py` | 3B (grep/AST sweep) | — |
| 258 | `tests/unit/test_dst_zones.py` | 3B (sampled/cited) | — |
| 259 | `tests/unit/test_email_links.py` | 3B (grep/AST sweep) | — |
| 260 | `tests/unit/test_email_migration.py` | 3B (grep/AST sweep) | — |
| 261 | `tests/unit/test_email_service.py` | 3B (sampled/cited) | — |
| 262 | `tests/unit/test_errors.py` | 3B (grep/AST sweep) | — |
| 263 | `tests/unit/test_foreign_key_indexes.py` | 3B (grep/AST sweep) | — |
| 264 | `tests/unit/test_forwarded.py` | 3B (grep/AST sweep) | — |
| 265 | `tests/unit/test_gdpr_service.py` | 3B (grep/AST sweep) | — |
| 266 | `tests/unit/test_idempotency.py` | 3B (grep/AST sweep) | — |
| 267 | `tests/unit/test_jwt_compat.py` | 3B (grep/AST sweep) | — |
| 268 | `tests/unit/test_jwt_required_claims.py` | 3B (grep/AST sweep) | — |
| 269 | `tests/unit/test_live_stripe_provider.py` | 3B (in depth) | 3B-12 |
| 270 | `tests/unit/test_logging_config.py` | 3B (sampled/cited) | — |
| 271 | `tests/unit/test_migrations_offline.py` | 3B (grep/AST sweep) | — |
| 272 | `tests/unit/test_mock_test_cards.py` | 3B (sampled/cited) | — |
| 273 | `tests/unit/test_models_schemas_review.py` | 3B (grep/AST sweep) | — |
| 274 | `tests/unit/test_money_properties.py` | 3B (in depth) | — |
| 275 | `tests/unit/test_openapi_examples.py` | 3B (grep/AST sweep) | — |
| 276 | `tests/unit/test_openapi_snapshot.py` | 3B (grep/AST sweep) | — |
| 277 | `tests/unit/test_order_emails.py` | 3B (grep/AST sweep) | — |
| 278 | `tests/unit/test_order_schemas.py` | 3B (grep/AST sweep) | — |
| 279 | `tests/unit/test_order_status_rules.py` | 3B (grep/AST sweep) | — |
| 280 | `tests/unit/test_overload.py` | 3B (grep/AST sweep) | — |
| 281 | `tests/unit/test_payment_currencies_exhaustive.py` | 3B (grep/AST sweep) | — |
| 282 | `tests/unit/test_payment_mutants.py` | 3B (grep/AST sweep) | — |
| 283 | `tests/unit/test_payment_provider_config.py` | 3B (grep/AST sweep) | — |
| 284 | `tests/unit/test_payment_provider_contract.py` | 3B (in depth) | 3B-13 |
| 285 | `tests/unit/test_phone_e164.py` | 3B (grep/AST sweep) | — |
| 286 | `tests/unit/test_rate_limit.py` | 3B (sampled/cited) | — |
| 287 | `tests/unit/test_reconciliation_adapter.py` | 3B (grep/AST sweep) | — |
| 288 | `tests/unit/test_reconciliation_compare.py` | 3B (grep/AST sweep) | — |
| 289 | `tests/unit/test_reconciliation_provider.py` | 3B (grep/AST sweep) | — |
| 290 | `tests/unit/test_redis_async.py` | 3B (grep/AST sweep) | — |
| 291 | `tests/unit/test_redis_codec_wire_format.py` | 3B (grep/AST sweep) | — |
| 292 | `tests/unit/test_redis_parity.py` | 3B (grep/AST sweep) | — |
| 293 | `tests/unit/test_redis_service.py` | 3B (grep/AST sweep) | — |
| 294 | `tests/unit/test_redis_timeouts.py` | 3B (grep/AST sweep) | — |
| 295 | `tests/unit/test_request_context.py` | 3B (grep/AST sweep) | — |
| 296 | `tests/unit/test_response_round_trip.py` | 3B (sampled/cited) | — |
| 297 | `tests/unit/test_security_async.py` | 3B (grep/AST sweep) | — |
| 298 | `tests/unit/test_security_time.py` | 3B (grep/AST sweep) | — |
| 299 | `tests/unit/test_settings_take_effect.py` | 3B (grep/AST sweep) | — |
| 300 | `tests/unit/test_stripe_events.py` | 3B (grep/AST sweep) | — |
| 301 | `tests/unit/test_stripe_events_enqueue.py` | 3B (in depth) | — |
| 302 | `tests/unit/test_stripe_rate_limit_mapping.py` | 3B (in depth) | 3B-12 |
| 303 | `tests/unit/test_telemetry.py` | 3B (grep/AST sweep) | — |
| 304 | `tests/unit/test_test_infrastructure.py` | 3B (grep/AST sweep) | — |
| 305 | `tests/unit/test_timestamps_migration.py` | 3B (grep/AST sweep) | — |
| 306 | `tests/unit/test_user_schemas.py` | 3B (grep/AST sweep) | — |
| 307 | `tests/unit/test_version.py` | 3B (grep/AST sweep) | — |
| 308 | `tests/unit/test_webhook_event_migration.py` | 3B (grep/AST sweep) | — |
| 309 | `tests/unit/test_webhook_signature.py` | 3B (sampled/cited) | — |

**Audit artefacts, listed separately and not audited as product code:**
- `.cloud_audit/*.md` (12 reports)
- `.cloud_audit/repro/**` (10 scratch repro scripts)
- `.cloud_audit/*.log`
- `tests/audit_verification/test_cloud_audit.py` (30 tests)
- `tests/audit_verification/test_postgres_cloud_audit.py` (4 PG-only tests)
- `INTERVIEW_DEFENSE_GUIDE.md` (3C output)
- `FINAL_CLOUD_AUDIT_REPORT.md` (this file)

---

## Appendix A: How to reproduce

```bash
cd /home/user/fastapi_e-commerce_backend
unset UV_NATIVE_TLS

# Baseline fast suite (01_baseline: 5214 passed / 109 skipped; final: 5244 passed / 113 skipped, EXIT=0)
uv run pytest tests/ -q -n 4 -m "not timing_sensitive" -p no:cacheprovider

# Audit verification tests: a PASS means the defect is reproduced
uv run pytest tests/audit_verification -q -p no:cacheprovider            # SQLite: 30 passed, 4 skipped
TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:<port>/postgres \
  uv run pytest tests/audit_verification -v -p no:cacheprovider          # PG16: 34 passed

# Throwaway PostgreSQL 16 (as used by the auditors; data dir must be traversable by the postgres user)
mkdir -p /var/tmp/pg && chown postgres /var/tmp/pg
su postgres -c "/usr/lib/postgresql/16/bin/initdb -D /var/tmp/pg/data -A trust -U postgres"
su postgres -c "/usr/lib/postgresql/16/bin/pg_ctl -D /var/tmp/pg/data -o '-p 55450 -k /var/tmp/pg -c listen_addresses=127.0.0.1' -l /var/tmp/pg/log start"
# ... run the tests, then: pg_ctl -D /var/tmp/pg/data -m fast stop && rm -rf /var/tmp/pg

# Validate a proposed patch (saved from this report to a file)
git apply --check PAY-01.diff
# Lint gate
uv run ruff check app tests
```

## Appendix B: Patch inventory and verification

| # | Patch | Finding(s) | `git apply --check` (HEAD, alone) | Applies in sequence | Behavioural effect on the scratch copy |
|---|---|---|---|---|---|
| 1 | PAY-01 | 2A-01, 2B-03 (2A-04 partly) | ✅ | ✅ | 2 audit tests flip; 5 pinning tests need updating |
| 2 | PAY-02 | 2A-02 | ✅ | ✅ | flips; 2 pinning tests |
| 3 | PAY-03 | 2A-03 | ✅ | ✅ | flips |
| 4 | GDPR-02 | 2D-01, 2D-C18 | ✅ | ✅ | flips (SQLite); PG probe fails on HEAD and passes patched; 1 test counts hashes |
| 5 | DB-01 | 2B-01 | ✅ | ✅ | both PG16 tests flip |
| 6 | GDPR-01 | 2C-01 | ✅ | ✅ | scratch probe: payload redacted |
| 7 | GDPR-04 | 2C-10, 2D-03 | ✅ | ✅ | GDPR04 and 2D03 tests flip |
| 8 | GDPR-05 | 2C-11 | ✅ | ✅ | flips |
| 9 | GDPR-03 | 2C-07 | ✅ | ✅ | flips |
| 10 | AUTH-01 | 2C-02, 2C-03 | ✅ | ✅ | login test flips |
| 11 | AUTH-02 | 2C-04 (2C-14) | ✅ | ✅ | flips |
| 12 | REG-01 | 2C-05 | ✅ | ✅ | flips |
| 13 | DATA-01 | 2C-06 | ✅ | ✅ | flips |
| 14 | 2A-06 | 2A-06 | ✅ | ✅ | flips; 1 pinning test |
| 15 | 2B-02 | 2B-02 / 3B-01 | ✅ | ✅ | PG lock tests pass with the hook firing |
| 16 | 2B-07 | 2B-07 | ✅ | ✅ | flips |
| 17 | 2B-08 | 2B-08 | ✅ | ✅ | PG16 test flips |
| 18 | OBS-01 | 2D-02, 2D-C19 | ✅ | ✅ | import-time test unchanged (by design) |
| 19 | 2D-04 | 2D-04 | ✅ | ✅ | flips |
| 20 | 2D-05 | 2D-05 | ✅ | ✅ | suite green |
| 21 | 2D-06 | 2D-06 | ✅ | ✅ | suite green |
| 22 | DOC-01 | 2D-C02, C03, C08 | ✅ | ✅ | comments only |
| 23 | BUG-NEW-1 | BUG-NEW-1 | ✅ | ✅ | flips; 2 parity tests pin the old contract |

**Combined result.** All 23 were applied to a scratch copy:
- `ruff check app`: All checks passed.
- Full fast suite: **5,217 passed, 28 failed, 113 skipped**. Every failure is an intended flip (17 audit tests) or a test that pins the old behaviour (11, named in §2).
- PG16: 3 of 4 PG audit tests flip (DB-01 ×2, 2B-08). GDPR-02's stand-in is explained in §2.2.

Not given as diffs (Low; precise fix descriptions in §2.3): 2A-05, 2A-07, 2B-04, 2B-09, 2B-11, 2C-12, 2D-07.

## Appendix C: Artefacts in `.cloud_audit/`

| File | Content |
|---|---|
| `00_inventory.md` | Audit checklist: 279 `.py` + 30 `.md` files with line counts |
| `01_baseline.md` | Baseline: uv sync, fast suite (5214 passed / 109 skipped), ruff, mypy (91), bandit (0 High) |
| `02a_payments.md` | 2A: payments, webhooks, idempotency, reconciliation |
| `02b_catalog_orders.md` | 2B: catalog, cart, orders, lock-order matrix, ETag, merge patch |
| `02c_auth_gdpr.md` | 2C: auth, tokens, rate limiting, RFC 9457 matrix, GDPR collision matrix |
| `02d_infra_and_comments.md` | 2D: infrastructure, model/migration drift (none), 2D-C comment audit, mypy triage |
| `03a_docs_drift.md` | 3A: documentation vs code (3A-01..33) |
| `03b_tests_audit.md` | 3B: tests audit, mutations, SQLite/PG matrix, pragma review |
| `03c_interview_defense.md` | 3C: interview defense guide (source of DOC-NEW-1 and BUG-NEW-1) |
| `04_validation_phase2.md` | Validator: canonical IDs and final severities for code findings |
| `04_validation_phase3.md` | Validator: final verdicts for 3A, 3B, DOC-NEW-1 and BUG-NEW-1 |
| `04_verification_results.md` | Verification test author: commands, outputs, ID→test map |
| `_pytest_baseline.log`, `_pytest_final_full.log` | Fast-suite logs (final: 5244 passed, 113 skipped, EXIT=0) |
| `_pytest_audit_verification_pg16.log`, `_pg16_server_log_excerpt.log` | Orchestrator PG16 re-run (34 passed, EXIT=0) and server log (deadlock detected ×2, idle-in-transaction timeout) |
| `_ruff.log`, `_mypy.log`, `_bandit.log` | Static analysis logs |
| `repro/2b`, `repro/2c`, `repro/2d`, `repro/validator` | Scratch reproductions (deadlock_fk.py, purge_repro.py, drift.py, 2C race tests, validator 2A tests) |
