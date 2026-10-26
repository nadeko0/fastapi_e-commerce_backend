# 02A — Payments, webhooks, idempotency, reconciliation (pre-release audit)

Auditor: Subagent 2A. Target: v4.0.3. Date: 2026-10-26.
Method: line-by-line read of every scoped file. Each hypothesis was then checked against
guards elsewhere (DB constraints, locks, re-reads, reconciliation, existing tests). Every
finding marked "Confirmed (repro)" was reproduced with a scratch pytest that reuses the
repo's own fixtures (`tests/conftest.py`, `tests/integration/payment_support.py`,
`stripe_event_support.py`). Nothing under `app/`, `tests/` or `migrations/` was modified.

Scratch repro file (outside the repo):
`/tmp/claude-0/-home-user-fastapi-e-commerce-backend/d6bb2a49-b210-591b-b7be-8a1e71874dff/scratchpad/repro/test_2a_repro.py`
Run it with:
```
cd /home/user/fastapi_e-commerce_backend && unset UV_NATIVE_TLS
PYTHONPATH=.:tests:tests/integration uv run pytest -q -p no:cacheprovider -p conftest --rootdir=. \
  /tmp/claude-0/-home-user-fastapi-e-commerce-backend/d6bb2a49-b210-591b-b7be-8a1e71874dff/scratchpad/repro/test_2a_repro.py
```
Result: 5 passed. Each test asserts the faulty behavior, so a pass confirms the finding.

---

## 1. Scope & files read

| File | Read |
|---|---|
| app/services/payment/__init__.py | ✔ |
| app/services/payment/base.py | ✔ |
| app/services/payment/exceptions.py | ✔ |
| app/services/payment/live_stripe_provider.py | ✔ |
| app/services/payment/stripe_provider.py (mock) | ✔ |
| app/services/payment/types.py | ✔ |
| app/services/stripe_events.py | ✔ |
| app/services/reconciliation.py | ✔ |
| app/services/orders.py | ✔ |
| app/core/idempotency.py | ✔ |
| app/core/celery_context.py | ✔ |
| app/core/currencies.py | ✔ |
| app/tasks.py (all tasks + beat schedule) | ✔ |
| app/models/payment.py, payment_discrepancy.py, payment_ledger.py, order.py, order_items.py | ✔ |
| app/schemas/webhook_event.py, app/schemas/order.py | ✔ |
| app/api/v1/orders.py (all 1920 lines) | ✔ |
| app/api/v1/admin.py 865-1071 | ✔ |
| migrations: 76d14f9f12e5 (money/stock CHECKs), a7c3e5b19d42 (refunds), c2d8f4a6b913 (disputes), d4b7e1f93a62 (discrepancies), d4e9a1c7b265 (webhook payloads), b6f1c8d2a047 (idempotency fingerprints) | ✔ |
| Followed: app/core/config.py 330-560 (payment settings, production checks), docker-compose.yml worker command, stripe-python 16.0.0 `WebhookSignature.verify_header` | ✔ |
| Tests consulted: tests/conftest.py, tests/integration/payment_support.py, stripe_event_support.py, test_payment_chain.py, test_reconciliation.py (header) | ✔ |

---

## 2. Summary of findings

| ID | Title | Severity | Confidence |
|---|---|---|---|
| 2A-01 | The unpaid-order expiry cancels and restocks orders that still have a live payment attempt. A later charge is kept, with no refund path in the app and no alert surface | **High** | Confirmed (repro) |
| 2A-02 | Reconciliation never checks Checkout-Session payments whose completion webhook was lost, which is exactly the failure it exists to catch | **High** | Confirmed (repro) |
| 2A-03 | Out-of-order refund events move a refund back from `succeeded` to `pending`. A fully refunded order goes back to PAID and can be fulfilled | **Medium** | Confirmed (repro) |
| 2A-04 | The 5-minute "stale attempt" window lets a second attempt start while the first Checkout Session or 3DS PaymentIntent can still be paid. Two charges for one order are only flagged | **Medium** | Confirmed (repro) |
| 2A-05 | Daily reconciliation is capped at the newest 200 rows (skipped rows count against the cap) and runs under the 240 s soft time limit. Older payments in the window are never checked | **Medium** | Confirmed by reading (cap); Plausible (time limit) |
| 2A-06 | `/pay` answers `PAID` even when the success was not applied (order cancelled, or already paid by another attempt) | Low | Confirmed by reading |
| 2A-07 | A /pay attempt left `processing` by a crash or unexpected error makes its Idempotency-Key answer 409 "retry later" forever. Nothing moves stale attempts to `unknown` | Low | Confirmed (repro) |
| 2A-08 | The refund Idempotency-Key is not stored locally. A retry after Stripe drops the key (24 h) is a second refund | Low | Confirmed by reading (documented limitation) |
| 2A-09 | Minor robustness items (body `order_id` ignored, webhook tolerance unbounded, random event id fallback) | Info | Confirmed by reading |

---

## 3. Detailed findings

### 2A-01 — The unpaid-order expiry ignores in-flight payment attempts. The late charge is kept with no in-app refund path and no alert surface (High)

**Code**
- `app/tasks.py:479-499`: candidates are selected only by order status, payment status and `Order.created_at`:
  ```python
  cutoff = utc_now() - timedelta(minutes=settings.UNPAID_ORDER_EXPIRE_MINUTES)   # 30 min
  ... Order.status == OrderStatus.NEW,
      Order.payment_status.in_([s.value for s in UNPAID_PAYMENT_STATUSES]),
      Order.created_at < cutoff,
  ...
  cancel_order_and_restock(db, order)
  ```
  The expiry ignores whether a Payment row for the order is `processing` / `requires_action` / `unknown` (an open Checkout Session, a 3DS challenge, an async PaymentIntent). It also never asks Stripe to expire the session or cancel the PaymentIntent. `PaymentProvider.expire_checkout_session` exists (`base.py:189`) but nothing calls it (`grep -rn expire_checkout_session app/` matches only definitions). The provider ABC has no `cancel_payment_intent`.
- `app/services/payment/live_stripe_provider.py:347-367`: the Checkout Session is created without `expires_at`, so Stripe's default lifetime of 24 h applies. The order lives 30 min (`config.py:414`).
- `app/api/v1/orders.py:765-767`: the late success is only flagged:
  ```python
  if order.status == OrderStatus.CANCELLED:
      _flag_payment_for_manual_review(db, payment_row, "order is cancelled")
      return False
  ```
- The app has no way to return that money:
  - `/refund` refuses the order because `payment_status` is still `pending` (`orders.py:1540`: "Only a paid order can be refunded").
  - `/refund` also skips flagged payments (`orders.py:1558`: `Payment.requires_manual_review.is_(False)`).
- Nothing surfaces it either:
  - No admin endpoint lists `requires_manual_review` payments across orders. Grep shows it only in the per-order `GET /orders/{id}/payments`.
  - Reconciliation sees ours=`succeeded` and theirs=`succeeded` and records nothing (`reconciliation.py:170`). Its docstring says it deliberately never looks at manual-review flags.
  - The only signal is a `logger.warning` (`orders.py:984`).

**Failure scenario** (reproduced in `test_A_late_charge_on_expired_order_is_unrefundable_and_invisible`)
1. A customer places an order for the last unit (stock 1 → 0) and starts `/pay` with a 3DS card (or `/checkout-session`). The attempt is `requires_action` / `processing`.
2. At T+30..35 min `cancel_expired_unpaid_orders` cancels the order. Stock goes back to 1 and another customer buys the unit.
3. The customer completes 3DS, or pays on the hosted page, which stays valid for 24 h. Stripe captures the money. `payment_intent.succeeded` / `checkout.session.completed` marks the attempt `succeeded, requires_manual_review=True`. The order stays `cancelled/pending`.
4. Admin `POST /orders/{id}/refund` returns **409**. `reconcile_payments` opens **0** discrepancies. The customer has been charged for a cancelled order whose goods were resold. Only a log line records it.

The existing test `tests/integration/test_payment_chain.py:426 test_expired_unpaid_order_and_a_late_payment` asserts this outcome as intended behavior. It does not check that the money can be returned or that anyone is told.

**Why it is not handled elsewhere:** checked `_guard_no_attempt_in_flight` (only guards new attempts), reconciliation (status-only), `/refund` guards, and the admin endpoints in scope. None of them catches this case.

**Suggested minimal fix** (any one of these closes the money-loss part; do 1 and 2 together):
1. Do not expire an order while an attempt is in flight, and keep sessions shorter than that window:
```diff
--- a/app/tasks.py
+++ b/app/tasks.py
@@ def expire_unpaid_orders(db) -> int:
         if order is None or not is_unpaid_and_new(order) or order.created_at >= cutoff:
             db.rollback()
             continue
+        # An attempt that can still be paid (open Checkout Session, 3DS,
+        # async PaymentIntent) holds the order until it settles or goes stale.
+        if db.scalars(select(Payment.id).where(
+            Payment.order_id == order.id,
+            Payment.status.in_(("processing", "requires_action", "unknown")),
+            Payment.created_at > utc_now() - timedelta(minutes=settings.CHECKOUT_SESSION_EXPIRE_MINUTES),
+        ).limit(1)).first() is not None:
+            db.rollback()
+            continue
         cancel_order_and_restock(db, order)
```
```diff
--- a/app/services/payment/live_stripe_provider.py
+++ b/app/services/payment/live_stripe_provider.py
@@ def create_checkout_session(
             "success_url": success_url,
             "cancel_url": cancel_url,
+            # Stripe's minimum is 30 minutes; never longer than the order may wait.
+            "expires_at": int(time.time()) + settings.CHECKOUT_SESSION_EXPIRE_MINUTES * 60,
         }
```
   Here `CHECKOUT_SESSION_EXPIRE_MINUTES` is a new setting (≥ 30, and ≤ the expiry grace period).
2. Before cancelling, call `provider.expire_checkout_session(...)` for an open session. If Stripe answers "already complete", do not cancel and let the webhook apply the payment. Do the same with a new `cancel_payment_intent` for `requires_action` intents.
3. Turn the "order is cancelled" branch into an automatic refund task, with a deterministic Stripe idempotency key such as `f"autorefund-payment-{payment_row.id}"`. Then a retry or a duplicate event cannot refund twice. Also add `GET /admin/payments?requires_manual_review=true` so flagged charges show up in a queue.

**Pytest sketch:** `test_A_*` in the scratch file. It uses `Shop.pay(token=PAY_3DS)`, `Shop.expire_unpaid` (needs `tasks_db`), `Shop.intent_event`, `Shop.refund`, and `reconcile_payments(db, StripePaymentProvider())`. After the fix, assert that `expire_unpaid(...) == 0` while the attempt is in flight. For fix 3, assert that a refund was created.

---

### 2A-02 — Reconciliation skips Checkout-Session payments whose `checkout.session.completed` was lost (High)

**Code**
- A Checkout attempt keeps the placeholder `payment_intent_id = "pending-cs-…"` (`orders.py:1417`) until the `checkout.session.completed` handler fills in the real PaymentIntent id (`stripe_events.py:385-389`).
- `app/services/reconciliation.py:288-291`:
  ```python
  if payment.payment_intent_id.startswith(PENDING_INTENT_ID_PREFIX):
      result.skipped += 1
      continue
  ```
  So the attempt stays "skipped" for as long as the completion webhook is missing. `retrieve_checkout_session` exists in both providers (`live_stripe_provider.py:384`) but no code calls it.
- The same root cause affects later events. A `refund.created` / `charge.dispute.*` event for that PaymentIntent finds no Payment row (`stripe_events.py:119-128, 196-206`) and is committed as `processed, applied: False`. It is gone for good.

**Failure scenario** (reproduced in `test_B_checkout_lost_webhook_is_skipped_by_reconciliation`)
In the Checkout flow the webhook is the *only* signal that the customer paid; /pay at least has its synchronous response. Suppose events stop arriving. Typical causes:
- the webhook secret was rotated incorrectly, so every delivery gets 400 and Stripe gives up after 3 days;
- the endpoint was disabled in the Dashboard;
- the event exhausted `STRIPE_EVENT_MAX_RETRIES`.

Then:
1. The customer pays on the hosted page and Stripe captures the money.
2. Thirty minutes later the order expires (2A-01) and its stock is resold.
3. The daily reconciliation reports `checked=0, skipped=1` and opens no discrepancy. The documented purpose in the reconciliation docstring ("a webhook that never arrived") fails for the flow that relies on webhooks the most.

The third case (event exhausted its retries) partly shows up as a `failed` row in `GET /admin/webhook-events`. The first two cases leave no trace in the app.

**Suggested minimal fix**
```diff
--- a/app/services/reconciliation.py
+++ b/app/services/reconciliation.py
@@ def reconcile_payments(
         payment = db.get(Payment, payment_id)
-        if payment.payment_intent_id.startswith(PENDING_INTENT_ID_PREFIX):
+        if payment.payment_intent_id.startswith(PENDING_INTENT_ID_PREFIX) and not payment.checkout_session_id:
             result.skipped += 1
             continue
```
Then add a session branch to `_ask_provider` / `compare`. For a row with `checkout_session_id` and a pending PaymentIntent id, call `provider.retrieve_checkout_session(id)`. If `session.payment_status == "paid"` or `session.status == "complete"`, emit `Difference(KIND_STATUS, "", payment.status, "complete/paid")`. Optionally fill in `session.payment_intent` and re-run the PaymentIntent comparison. Also filter rows that will be skipped out of the SQL query so they do not use up the cap (see 2A-05).

**Pytest sketch:** `test_B_*` (scratch). Uses `Shop.checkout_session` and sets `StripePaymentProvider._sessions_by_id[...]` to complete/paid. After the fix, assert `result.checked == 1` and a `KIND_STATUS` discrepancy. Pattern to copy: `tests/integration/test_reconciliation.py`.

---

### 2A-03 — Out-of-order refund event moves a refund from `succeeded` back to `pending`, and a fully refunded order back to PAID (Medium)

**Code** — `app/services/stripe_events.py:253-257`:
```python
if refund_row.status in _REFUND_STATUSES_NOT_RETURNED:   # failed / canceled are final
    return False
refund_row.status = status                                # any other status overwrites, incl. succeeded -> pending
if status not in _REFUND_STATUSES_NOT_RETURNED:
    return False
```
Then `_settle_refunds` (`orders.py:1795-1804`) subtracts every refund that is not `succeeded` / `failed` / `canceled` from the returned total, so the order goes from `refunded` back to `paid`.

Stripe does not guarantee event order. Each event carries a snapshot of the object taken when the event was created, and redeliveries or this app's own retry backoff (`retry_delay_seconds`, up to 15 min) easily reorder events. For example, a `refund.created` snapshot with `status: pending` can be applied after a `refund.updated` with `status: succeeded`.

**Failure scenario** (reproduced in `test_C_stale_refund_event_regresses_refunded_order_to_paid`)
1. An order is paid and an admin issues a full refund, so the order is `refunded`.
2. A stale `refund.created` (or `refund.updated`) with `status: "pending"` for the same refund id is processed afterwards.
3. The order becomes `confirmed / paid`, and `PUT /orders/{id}/status {"status":"processing"}` now returns **200**. The goods can be shipped for an order whose money was returned. Next day reconciliation flags `refund_status`, but it does not repair anything and fulfilment may already have happened.

**Suggested minimal fix**
```diff
--- a/app/services/stripe_events.py
+++ b/app/services/stripe_events.py
@@ def _apply_refund_object(db: Session, payment_row: Payment, refund_object: dict) -> bool:
     if refund_row.status in _REFUND_STATUSES_NOT_RETURNED:
         return False
+    # Events are not ordered: an older snapshot (pending / requires_action)
+    # must not move a succeeded refund back. succeeded -> failed/canceled is real.
+    if refund_row.status == "succeeded" and status not in _REFUND_STATUSES_NOT_RETURNED:
+        return False
     refund_row.status = status
```
A more robust alternative is to re-fetch the Refund from Stripe for `refund.*` events and apply the live state.

**Pytest sketch:** `test_C_*` (scratch). Uses `Shop.pay`, `Shop.refund`, `Shop.webhook("refund.created", {... "status": "pending"})` and `order_state`. Belongs next to `tests/integration/test_refund_lifecycle.py`.

---

### 2A-04 — Two payable attempts for one order after the 5-minute stale window; the second charge is only flagged (Medium)

**Code**
- `orders.py:919-926`: `_guard_no_attempt_in_flight` only counts attempts with `created_at > now - PAYMENT_ATTEMPT_STALE_AFTER_MINUTES` (default 5, `config.py:421`).
- The first Checkout Session stays payable for 24 h (no `expires_at`, see 2A-01). A `requires_action` PaymentIntent stays confirmable until it is cancelled.
- When one attempt succeeds, sibling sessions are not expired and sibling intents are not cancelled. `apply_event` → `_apply_payment_success` (`orders.py:768-772`) flags the second success as "already paid by another attempt".

**Failure scenario** (reproduced in `test_E_two_payable_checkout_sessions_double_charge`)
1. Tab A opens Checkout session 1. The customer waits more than 5 minutes (looking for a card, for example).
2. Tab B clicks "Pay" again, which sends a new Idempotency-Key. The request is allowed and creates session 2.
3. The customer pays in B, and the order is paid.
4. The customer then pays in tab A as well; session 1 is still open at Stripe. They are charged twice. Payment 1 becomes `succeeded, requires_manual_review=True` and is not refunded.

The same happens with a 3DS intent whose approval arrives late. As in 2A-01, the flagged charge cannot be refunded through `/refund` and is not listed anywhere.

**Suggested fix**
- Set `expires_at` on sessions (2A-01).
- Inside `_apply_payment_success`, after the commit, expire the order's other open sessions and cancel its other `requires_action` intents.
- Auto-refund "already paid by another attempt" charges, using a deterministic key `autorefund-payment-{id}`.
- Alternatively, keep blocking new attempts while an open session exists (`checkout_session_id IS NOT NULL AND status='processing'`) until the session expires, instead of the 5-minute cut-off.

**Pytest sketch:** `test_E_*` (scratch). After the fix, assert that session 1's mock status is `expired` once session 2 succeeds, or that the second `checkout-session` call returns 409.

---

### 2A-05 — Reconciliation coverage: capped at the newest 200 rows, and the soft time limit can cut a run short (Medium)

**Code**
- `reconciliation.py:278`: `ids = list(db.scalars(query.order_by(Payment.id.desc()).limit(limit + 1)))` with `RECONCILIATION_MAX_PAYMENTS=200`. Each run walks the same newest-first slice. When more than 200 attempts fall in the 7-day window, the older ones are **never** checked. The run only logs a warning and sets `truncated`.
- Rows that will be skipped (`pending-…` placeholders: every abandoned Checkout Session, every create-timeout) are counted inside the 200 (`ids[:limit]` comes before the skip at line 289). A store with many abandoned checkouts therefore reconciles even fewer real payments.
- The task runs under `task_soft_time_limit=240` (`tasks.py:139`). Each payment makes two Stripe calls (`retrieve` plus at least one `list`). At a measured 0.4-0.7 s per call (`orders.py:1152-1153`), 200 payments take about 160-280 s. A slow Stripe or 429 retries (`STRIPE_MAX_NETWORK_RETRIES=2`, read timeout 20 s) push it further. `SoftTimeLimitExceeded` is not a `PaymentProviderError`, so it escapes the loop (`reconciliation.py:297`) and ends the run. Work committed so far is kept, but the run always ends in the same place.
- Reconciliation only iterates local rows; it never lists Stripe objects. A charge with no local row (or with a pending placeholder, see 2A-02) is never discovered.

**Failure scenario**: a store takes more than 200 attempts a day (failed attempts count). A Dashboard refund or a lost success webhook on a payment that is 2 days old is never reported.

**Suggested fix**: exclude rows that will be skipped in SQL. Use a persisted cursor, for example `payments.reconciled_at` ordered ascending (least recently reconciled first). Catch `SoftTimeLimitExceeded` and stop cleanly. Or fan out one task per chunk of N ids.

**Pytest sketch**: in the style of `tests/integration/test_reconciliation.py`, set `RECONCILIATION_MAX_PAYMENTS=2` with monkeypatch and create 3 aged payments plus 2 pending-cs rows. Assert that the oldest real payment is checked across two runs. Today it never is.

---

### 2A-06 — `/pay` answers PAID when the success was not applied (Low)

**Code** — `orders.py:1292-1307`: the return value of `_apply_payment_success` is discarded:
```python
await db.run_sync(_apply_payment_success, order, payment_row, background_tasks, current_user.email)
return PaymentResponse(..., status=PaymentStatus.PAID, ...)
```
When the order was cancelled after the guards ran (the lock is released at line 1136 before the Stripe call), or was already paid by another attempt, the function returns `False` and flags the attempt. The client is still told PAID. The order itself (GET) shows `cancelled/pending`. The window is the duration of the Stripe call, so this is rare, but the result is a contradiction shown to a customer who was charged.

**Fix**: `applied = await db.run_sync(...)`. If `applied` is false, return a 409 problem such as "payment taken but the order could not be completed; it will be refunded", or a distinct status.

**Pytest sketch**: monkeypatch `StripePaymentProvider.confirm_payment_intent` so that, before it returns, it cancels the order in the database with a separate `TestingSessionLocal` session. Then assert the response status.

---

### 2A-07 — A `processing` attempt with no live request makes its key answer 409 forever (Low)

**Code**
- `orders.py:695-704`: `_replay_payment` returns 409 "still being processed; repeat it later with the same key" for status `processing`.
- The following paths leave a row `processing` with no request still running:
  - a crash or OOM after the commit at line 1136;
  - an uncaught exception, for example a DB error at the commit on line 1170;
  - an `IdempotencyError` on a fresh attempt (line 1208: rollback, and only a *retried* attempt is set to `unknown`).
- No sweeper moves stale `processing` rows to `unknown`, and reconciliation accepts `processing` for a `requires_confirmation` intent.

**Scenario** (reproduced in `test_D_crash_leaves_key_stuck_processing`): the client follows the `Retry-After: 5` advice forever. After 5 minutes it can make progress only by sending a *new* key, which goes against the key's own instructions. No money is at risk, because the PaymentIntent was never confirmed on these paths.

**Fix**: treat `processing` older than `PAYMENT_ATTEMPT_STALE_AFTER_MINUTES` like `unknown` in `/pay`. That means re-sending to Stripe with the same provider keys, which is safe because the keys are deterministic (`_provider_idempotency_key`). Also set the row to `unknown` in the `IdempotencyError` branch for fresh attempts.

---

### 2A-08 — The refund key is not stored locally; a retry after Stripe's 24 h key pruning is a new refund (Low)

**Code** — `orders.py:1522-1530` and `1589-1596`. The `/refund` key is only forwarded to Stripe and no local row is reserved. `REFUND_KEY_HEADER_DESCRIPTION` (`idempotency.py:73-78`) documents the 24 h limit. Example: an admin tool retries a partial refund of 40% after more than 24 h, say from an offline queue. Stripe accepts it as a new refund because the charge is still under its limit, so the money goes back twice. Each refund is recorded once, so the ledger is consistent, just doubled.

**Fix**: store `(order_id, idempotency_key, fingerprint) -> refund_id` in `payment_refunds` (or a small table) before calling Stripe. Replay from it, as `/pay` does.

---

### 2A-09 — Info

- `PaymentCreate.order_id` (body) is never compared with the path `order_id` (`orders.py:1011-1113`). It is harmless: the path order is the one charged and the amount is checked against it. It is still confusing, and it is part of the fingerprint.
- `STRIPE_WEBHOOK_TOLERANCE_SECONDS` has no bounds (`config.py:392`). A negative value rejects every event and a huge value turns off replay protection. Add `Field(300, ge=1, le=600)`.
- `base.py:91`: an event without `id` gets a random `evt_…`, which turns dedupe off. Only reachable with a valid signature, and Stripe always sends `id`.
- Zero-total orders (`ck_order_total_non_negative` allows 0, `models/order.py:56`) can never be paid, because both providers reject amount 0. They always expire. This is a product decision; it should be documented or handled.

---

## 4. Checked and found sound

**Q1 — Idempotency-Key**
- **POST /orders** reserves the key in the DB *before* any side effect. The unique partial index `uix_order_user_idempotency_key` (`models/order.py:68-73`) is hit on `db.flush()` (`orders.py:262-278`) before the stock UPDATEs. A concurrent twin blocks on the index until the first request commits or rolls back. It then either replays the first order (IntegrityError → rollback → reread) or proceeds normally if the first rolled back.
- **/pay and /checkout-session** take `SELECT … FOR UPDATE` on the order (`orders.py:1043-1048`, `1354-1359`) and commit the `processing` Payment row (unique `(order_id, idempotency_key)`, `models/payment.py:76-78`) before calling Stripe. A twin waits on the lock, then sees `processing` → 409 with Retry-After (`orders.py:695-704`). If both pass the pre-check, the IntegrityError branch replays (`orders.py:1137-1149`).
- **Same key, different body** → 422 through `request_fingerprint` + `ensure_same_request` (`idempotency.py:36-58`). The fingerprint is canonical JSON of the validated model, so `19.9` and `19.90` match.
- **Key reused across users**: orders are scoped per user; payments are scoped per order, which is scoped per user (other users get 404). Stripe keys are namespaced per order with `_provider_idempotency_key` (`orders.py:589-599`). Create and confirm use different keys (`:confirm`), as Stripe requires.
- **TTL**: order keys expire through `clear_expired_idempotency_keys` (`tasks.py:700-743`), which clears the key and leaves the order row. Payment keys never expire locally. A retry of an `unknown` attempt more than 24 h later cannot hit a pruned Stripe key, because the order has been cancelled by then (30 min) and `_guard_order_payable` refuses it (`orders.py:869`).
- **Response stored vs. commit**: there is no separate response cache. Replays are rebuilt from committed rows, so a stored response without a commit (or the reverse) cannot happen.

**Q2 — Partial failure.** Stripe calls use deterministic keys derived from (kind, order id, client key).
- Stripe succeeds, then the local commit fails: the row has the real PaymentIntent id (committed at line 1170 before confirm), and `payment_intent.succeeded` applies it. If the id was not committed, confirm was never sent, so nothing was charged.
- Timeout → `unknown` → retry with the same key re-sends the same Stripe keys, so there is no double charge (`orders.py:1082-1122`).
- An event that overtakes its attempt: `_attempt_still_storing_its_id` (`stripe_events.py:56-90`) keeps it retrying instead of dropping it.
- Refund succeeds at Stripe but the local commit fails: `refund.created` records it with `source="provider"`, and a same-key retry within 24 h gets the same refund id, deduplicated by unique `refund_id` (`orders.py:1752-1768`).
- **Remaining gaps**: 2A-02 and 2A-05.

**Q3 — Out-of-order and duplicate webhooks**
- Duplicate delivery: unique `event_id` + `store_event` IntegrityError → `already_processed` (`stripe_events.py:438-459`).
- Concurrent delivery of the same stored event to two workers: `_claim_event` `FOR UPDATE SKIP LOCKED` plus a status check means it is applied once (`stripe_events.py:531-565`). The status mark and the state changes commit together; an error rolls both back. `_record_failure` refuses to retry an event already marked processed (`stripe_events.py:587-592`).
- A failure after a success is ignored (`orders.py:833-835`). A success after a failure is applied. A failure never moves a PAID/REFUNDED order to FAILED (`orders.py:817, 837`).
- A dispute before the success: the dispute is recorded against the attempt (found by PaymentIntent id) and the hold applies.
- Wrong amount/currency in a success → flagged, not applied (`orders.py:954-970`, `stripe_events.py:349-362, 407-412`).
- Late success on a cancelled order → flagged (but see 2A-01).

**Q4 — Decimal**
- `STORE_CURRENCY` is validated to Stripe currencies with two decimals: zero-decimal, three-decimal and ISK are refused (`currencies.py`, `config.py:479-500`). So `*100` (`orders.py:607`) is exact.
- Inputs are decimal strings only, with at most 10 digits and 2 decimal places (`schemas/order.py:41-121`). Floats and ints are refused. There is no rounding step that could drop a sub-cent fraction.
- `total_amount` is a sum of products of 2-dp Decimals, so it is exact.
- Over-refunds: Stripe enforces the refundable amount, and locally `ck_payment_refunded_within_amount` plus the order lock in `_record_refund` / `_settle_refunds` keep the totals straight. The only float is the stats cache (`tasks.py:609`), outside the money paths.

**Q5 — Reconciliation API use**
- `list_refunds` paginates correctly with `starting_after=last id` and `has_more`. It is bounded at 5×100 and raises instead of returning a partial list (`live_stripe_provider.py:450-473`).
- Each payment's provider error is counted and the run moves on. It aborts after 5 consecutive errors.
- No transaction is held open during Stripe calls (`reconciliation.py:294`).
- The time window uses naive-UTC `utc_now()` with inclusive bounds.
- Discrepancy rows are idempotent (unique `(payment_id, kind, subject)`).
- Gaps: 2A-02, 2A-05.

**Q6 — Celery**
- `acks_late` + `reject_on_worker_lost` + `cancel_long_running_tasks_on_connection_loss`. The event row lock with SKIP LOCKED makes a redelivery or duplicate a no-op.
- The retry countdown is ≤ 3000 s, below the 3600 s `visibility_timeout` (`config.py:444`).
- The sweeper re-publishes `received`/`retrying` events older than 20 min, more than the 15 min maximum backoff.
- A rolled-back transaction leaves the event `received`, so no work is hidden forever by SKIP LOCKED: the lock disappears with the transaction and the sweeper catches lost messages.
- The worker runs without `-Q` (`docker-compose.yml:68`), so it consumes every queue in `task_queues`, including `payments`.
- Commit-then-raise inside `apply_event` is handled: the processed mark is committed with the change, and `_record_failure` skips it.
- `expire_unpaid_orders` uses SKIP LOCKED, re-checks the order under the lock, and commits per order.

**Q7 — Webhook signature**
- The raw body is read with `request.body()` and verified before parsing (`orders.py:1864-1880`).
- Live provider: stripe-python 16.0.0 `verify_header`, which also refuses an empty secret. On top of that the app adds a two-sided timestamp check, so future-dated signatures and tolerance 0 are handled (`live_stripe_provider.py:505-519`). Non-ASCII headers and non-UTF-8 bodies → 400 instead of 500.
- The mock refuses an empty secret, compares in constant time, and accepts multiple v1 signatures.
- Production requires a webhook secret of at least 32 bytes that is not a placeholder (`config.py:527`).
- Unverified data is never used. Verified data is cross-checked: rows are found by PaymentIntent or session id, never by `metadata.order_id`. Metadata is only used to decide "retry later". Success amount and currency are compared with the local order.

**Other things that look risky but are handled**
- **Admin cancel of a paid order** keeps it `cancelled/paid`, still refundable through `/refund`. This is by design and tested (`test_payment_chain.py:404`).
- **Refunding a disputed charge** is refused (`orders.py:1571`). A lost dispute → REFUNDED (`orders.py:1788-1794`).
- **A refund that fails after it was recorded** is taken back out of `amount_refunded` and flagged (`stripe_events.py:258-266`). The amount never double-counts and cannot break the CHECK.
- **`celery_context`**: tokens are keyed by task id and reset in postrun; eager runs inside a request are left alone. No leak across tasks.

---

## 5. Open questions

1. Which events is the production webhook endpoint subscribed to, and at which API version? The code relies on `refund.created/updated/failed` (`charge.refunded` carries no `refunds` list in current API versions) and on `payment_intent.canceled` and `checkout.session.async_payment_*`.
2. Is the `"needs manual review"` WARNING log wired to an alert? Today it is the only operational signal for 2A-01 and 2A-04.
3. Is the 30-minute `UNPAID_ORDER_EXPIRE_MINUTES` meant to be shorter than the Checkout Session lifetime? If so, which party is expected to expire the session?
4. Is the Stripe account shared with other deployments (staging and prod with the same account)? `_provider_idempotency_key` is namespaced by local `order.id` only. Collisions would need the same client key, which is negligible for UUID keys but not for predictable ones.
5. Expected payment volume per day, which decides whether 2A-05 matters before launch.
