# Phase 3b - Validation of the stateful (A1-A3) and concurrency (C1-C5) findings

Files edited: `tests/torture_suite/test_stateful_invariants.py`, `tests/torture_suite/test_concurrency_torture.py`.
Raw outputs: `.cloud_torture/scratch_3b/` (f_* = final proof, v_* = fix verification, all_fixes.diff = verified diff).

## Verdicts

| Bug | Verdict | Severity | Reproducer(s) (xfail strict) | Location |
|---|---|---|---|---|
| A1 stale refund event rewinds settled refund (refunded -> paid) | REAL (Stripe does not order events) | medium | `test_bug_stale_refund_event_reverts_a_settled_refund` | app/services/stripe_events.py:253-255 |
| A2 /refund 500 while an earlier refund.failed is unprocessed | REAL (Stripe refunds can fail after creation and the amount becomes refundable again; mock model realistic) | medium | `test_bug_refund_after_an_unreported_refund_failure_is_a_500` | app/api/v1/orders.py:1625 -> :1767 (ck_payment_refunded_within_amount) |
| A3 restock past Int32Max | REAL (admin may set stock = Int32Max, API's own max) | low-medium | `test_bug_restock_past_int32_max_is_a_500_on_cancel` (PG: 500; SQLite: stored value > int32), `test_bug_restock_past_int32_max_aborts_the_expiry_sweep` (xfail on PG only) | app/services/orders.py:123,130; app/tasks.py:499 |
| C1 refresh rotated concurrently with logout survives | REAL | medium (security) | `test_bug_logout_racing_refresh_leaves_a_live_refresh_token` | app/services/redis_async.py:416-424, app/core/security.py:537-539 |
| C2 in-flight same-key POST /orders retry -> 400 "Cart is empty" | REAL | medium | `test_bug_order_replay_racing_its_first_request_answers_cart_empty` (SQLite + PG) | app/api/v1/orders.py:206-215 |
| C3 deadlock variant line vs plain line of same product -> 503 | REAL (confirmed 40P01) | high under load | `test_bug_variant_line_checkout_deadlocks_with_plain_line_checkout` (PG) | app/services/orders.py:84-91 |
| C4 Stripe events stored after GDPR purge keep PII | REAL | medium (GDPR) | `test_bug_webhook_stored_after_erasure_keeps_personal_data` (SQLite + PG) | app/tasks.py:341,363; stripe_events.py:438 |
| C5 mock-provider concurrent same-key refunds both succeed | REAL but mock-only (real Stripe is atomic per key) | low | `test_bug_mock_provider_refund_key_is_not_atomic` | app/services/payment/stripe_provider.py:486-532 |

No DESIGN items; TEST BUG fixes (stateful module): the end-of-test coverage assertions ("POST /orders 201",
"refund 200", ...) failed under the reduced default budgets (cart world on SQLite dev, payment world under ci).
They are now enforced only with `TORTURE_FULL=1`; the default budgets only require the common outcome
(`POST /cart/items 200` / `POST /orders 201`).

## Allowances (nondeterministic tests stay green, only the known signature tolerated)

All switched off by `TORTURE_STRICT=1`:
* stateful `KNOWN_BUG_ALLOWANCES`: state machines / properties skip the known-bug step shapes (bug 1 stale report
  delivery, bug 2 refund over an unreported failure, bug 3 overflowing cancel), counted in `SEEN["known bug ..."]`.
* concurrency: 503/40P01 on variant-vs-plain checkout bursts (C3), one live refresh token after logout race (C1),
  mock provider processing one refund Idempotency-Key twice (C5). Oversell, partial orders, other 5xx stay hard failures.

## Budgets / markers

* concurrency: `pytestmark = [db_sensitive, timing_sensitive]`, ~3 min on PG (ci profile), nothing heavier needed.
* stateful: `pytestmark = db_sensitive`, xdist-safe (checked with `-n 4`). Defaults lowered to
  cart 15 / payment 20 / @given 25 examples (ci profile doubles them); `TORTURE_FULL=1` restores 100 / 150 / 300 and
  the coverage assertions. First lowering (20/25/30) still took 4m34s on PG ci; (15/20/25) 3m20s.

## Verified fixes (applied in /tmp/claude-val-3b, copy of repo incl. .venv)

Evidence: with all fixes every strict reproducer XPASSes (SQLite: all 5 xfailed there - A1, A2, A3-cancel, C2, C4;
PG: all 9) = `scratch_3b/v_sqlite_bugs.txt`, `v_pg_bugs.txt`; `fixed_conc_pg.txt` (markers ignored): 97 passed incl. bursts;
existing suites with fixes: SQLite test_orders/refund_lifecycle/refund_bookkeeping/webhook_events/order_expiry/auth
166 passed; PG test_orders/order_invariants/order_expiry 56 passed (C3 fix).
Fix map: A1 stripe_events.py (ignore non-final report once succeeded); A2 orders.py refund route (reconcile
provider.list_refunds before recording); A3 services/orders.py `_restocked` CASE cap at INT32_MAX; C1 redis_async.py
`refresh_revoked:<family>` marker set first in revoke, checked in consume; C2 orders.py (answer from existing key order
when cart empty); C3 `with_for_update(key_share=True)` in lock_stock_rows; C4 `_redact_if_owner_erased` on processing;
C5 `threading.Lock` around the mock's create_refund. Full diff:

```diff
diff -ru -x __pycache__ /home/user/fastapi_e-commerce_backend/app/api/v1/orders.py /tmp/claude-val-3b/app/api/v1/orders.py
--- /home/user/fastapi_e-commerce_backend/app/api/v1/orders.py	2026-10-26 20:07:46.206925433 +0000
+++ /tmp/claude-val-3b/app/api/v1/orders.py	2026-10-26 21:53:19.821367779 +0000
@@ -77,6 +77,7 @@
     CardError,
     IdempotencyError,
     InvalidRequestError,
+    PaymentProviderError,
     PaymentProviderTimeoutError,
     SignatureVerificationError,
 )
@@ -210,6 +211,13 @@
 
     cart = await redis.get_cart(current_user.id)
     if not cart or not cart.items:
+        # A retry that overtook its first request: the key was not found
+        # above because the first one had not committed yet, and it has since
+        # committed and emptied the cart. The answer is its order, not 400.
+        existing = await _order_by_idempotency_key(db, current_user.id, idempotency_key)
+        if existing:
+            ensure_same_request(existing.idempotency_fingerprint, fingerprint)
+            return _created_order(request, response, existing)
         raise HTTPException(
             status_code=status.HTTP_400_BAD_REQUEST,
             detail="Cart is empty"
@@ -1609,12 +1617,30 @@
     except (CardError, InvalidRequestError) as e:
         raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=e.message) from e
 
+    # The provider's word on this charge's other refunds, read before the
+    # lock (no transaction held across a provider call). A refund that failed
+    # at the provider after it was recorded, whose refund.failed webhook has
+    # not been processed yet, still counts in amount_refunded here while the
+    # provider has made its amount refundable again (and may just have
+    # refunded it): recording this refund on top of it broke
+    # ck_payment_refunded_within_amount and answered 500 for a refund the
+    # provider had already made.
+    try:
+        provider_refunds = await run_in_threadpool(provider.list_refunds, payment_intent_id)
+    except PaymentProviderError:
+        provider_refunds = []  # the webhook will bring them up to date
     # Re-read under the order's lock before adding to it: payment_row was
     # read before the provider call, and another refund of this charge may
     # have been recorded since. Adding to that stale copy overwrote it - two
     # refunds taken by the provider, one recorded, and an order whose
     # refunds covered the charge left "paid".
     await db.run_sync(_lock_order_and_reload, order, payment_row)
+    from app.services.stripe_events import _apply_refund_object  # imports this module
+    for other in provider_refunds:
+        if other.id != refund.id and other.status.value in _REFUND_STATUSES_NOT_RETURNED:
+            await db.run_sync(_apply_refund_object, payment_row, {
+                "id": other.id, "status": other.status.value, "amount": other.amount,
+            })
     # provider.create_refund's own idempotency cache means a retried request
     # with the same Idempotency-Key returns the *same* refund.id instead of
     # creating a new one; recording it by that id (unique) keeps the replay
diff -ru -x __pycache__ /home/user/fastapi_e-commerce_backend/app/services/orders.py /tmp/claude-val-3b/app/services/orders.py
--- /home/user/fastapi_e-commerce_backend/app/services/orders.py	2026-10-26 20:07:46.215794779 +0000
+++ /tmp/claude-val-3b/app/services/orders.py	2026-10-26 21:53:31.133246469 +0000
@@ -9,12 +9,13 @@
 of unpaid orders all go through cancel_order_and_restock.
 """
 
-from sqlalchemy import select, update
+from sqlalchemy import case, select, update
 from sqlalchemy.orm import Session
 
 from app.core.clock import utc_now
 from app.models.order import Order
 from app.models.product import Product, ProductVariant
+from app.schemas.common import INT32_MAX
 from app.schemas.order import OrderStatus, PaymentStatus
 
 ORDER_STATUS_TRANSITIONS = {
@@ -77,20 +78,39 @@
     product's. Call it before the first stock UPDATE of the transaction;
     the UPDATEs themselves may then run in any order. SQLite ignores the
     lock, and with it the order.
+
+    The locks are FOR NO KEY UPDATE (key_share=True), the lock the stock
+    UPDATEs take anyway, not FOR UPDATE: FOR UPDATE also conflicts with the
+    FOR KEY SHARE that inserting an order_items row takes on the product of
+    a VARIANT line (the order_items -> products foreign key), a row this
+    function does not lock for that line. A checkout of {V of P} holding V
+    then waited at its INSERT for a checkout of {P, V} that held P and
+    waited for V: a deadlock (40P01), answered 503.
     """
     product_ids = sorted({product_id for product_id, variant_id in lines if variant_id is None})
     variant_ids = sorted({variant_id for _, variant_id in lines if variant_id is not None})
     db.execute(
-        select(Product.id).where(Product.id.in_(product_ids)).order_by(Product.id).with_for_update()
+        select(Product.id).where(Product.id.in_(product_ids)).order_by(Product.id)
+        .with_for_update(key_share=True)
     ).all()
     db.execute(
         select(ProductVariant.id)
         .where(ProductVariant.id.in_(variant_ids))
         .order_by(ProductVariant.id)
-        .with_for_update()
+        .with_for_update(key_share=True)
     ).all()
 
 
+def _restocked(column, quantity: int):
+    """`column + quantity`, capped at the largest stock the API accepts
+    (Int32Max). An admin may set a stock to 2147483647 while orders still
+    hold units of it; adding them back unchecked overflowed the INTEGER
+    column on PostgreSQL (NumericValueOutOfRange: the cancel answered 500 and
+    the expiry sweep stopped) and left an unreadable value on SQLite. The
+    comparison is written so that nothing overflows while it is computed."""
+    return case((column > INT32_MAX - quantity, INT32_MAX), else_=column + quantity)
+
+
 def cancel_order_and_restock(db: Session, order: Order) -> bool:
     """
     Move `order` to cancelled and put every line's quantity back into the
@@ -120,14 +140,14 @@
             db.execute(
                 update(ProductVariant)
                 .where(ProductVariant.id == item.variant_id)
-                .values({ProductVariant.stock_quantity: ProductVariant.stock_quantity + item.quantity})
+                .values({ProductVariant.stock_quantity: _restocked(ProductVariant.stock_quantity, item.quantity)})
                 .execution_options(synchronize_session=False)
             )
         else:
             db.execute(
                 update(Product)
                 .where(Product.id == item.product_id)
-                .values({Product.stock_quantity: Product.stock_quantity + item.quantity})
+                .values({Product.stock_quantity: _restocked(Product.stock_quantity, item.quantity)})
                 .execution_options(synchronize_session=False)
             )
     return True
diff -ru -x __pycache__ /home/user/fastapi_e-commerce_backend/app/services/payment/stripe_provider.py /tmp/claude-val-3b/app/services/payment/stripe_provider.py
--- /home/user/fastapi_e-commerce_backend/app/services/payment/stripe_provider.py	2026-10-26 20:07:46.216884986 +0000
+++ /tmp/claude-val-3b/app/services/payment/stripe_provider.py	2026-10-26 21:54:02.189053156 +0000
@@ -18,6 +18,7 @@
 """
 import hashlib
 import hmac
+import threading
 import time
 import uuid
 from typing import Any, ClassVar
@@ -218,6 +219,10 @@
     _sessions_by_id: ClassVar[dict[str, CheckoutSession]] = {}
     _refunds_by_key: ClassVar[dict[str, Refund]] = {}
     _refunds_by_id: ClassVar[dict[str, Refund]] = {}
+    # Real Stripe checks an Idempotency-Key and records its result as one
+    # step; the routes call this mock from a thread pool, so concurrent
+    # requests with one key both missed the cache and both refunded.
+    _refund_lock: ClassVar[threading.Lock] = threading.Lock()
 
     @classmethod
     def reset_mock_state(cls) -> None:
@@ -461,6 +466,19 @@
         self,
         *,
         payment_intent_id: str,
+        amount: int | None = None,
+        idempotency_key: str | None = None,
+    ) -> Refund:
+        with self._refund_lock:
+            return self._create_refund(
+                payment_intent_id=payment_intent_id, amount=amount,
+                idempotency_key=idempotency_key,
+            )
+
+    def _create_refund(
+        self,
+        *,
+        payment_intent_id: str,
         amount: int | None = None,
         idempotency_key: str | None = None,
     ) -> Refund:
diff -ru -x __pycache__ /home/user/fastapi_e-commerce_backend/app/services/redis_async.py /tmp/claude-val-3b/app/services/redis_async.py
--- /home/user/fastapi_e-commerce_backend/app/services/redis_async.py	2026-10-26 20:07:46.216884986 +0000
+++ /tmp/claude-val-3b/app/services/redis_async.py	2026-10-26 21:53:11.454533464 +0000
@@ -416,13 +416,27 @@
                 data, deleted = await pipe.execute()
             if not data or not deleted:
                 return None
-            return json.loads(data)
+            payload = json.loads(data)
+            # A family revoked while this token was being rotated: the
+            # revocation may have read the family's members before the new
+            # jti was added to it (revoke_refresh_family). Refused, so the
+            # caller revokes the family again, new jti included.
+            if await self._redis.exists(f"refresh_revoked:{payload.get('family')}"):
+                return None
+            return payload
         except RedisError as e:
             self._handle_redis_error(f"consume_refresh_token: {str(e)}")
             raise TokenStateUnavailable from e
 
     async def revoke_refresh_family(self, family_id: str) -> bool:
         try:
+            # Marked first: a rotation that adds a jti to the family after
+            # the SMEMBERS below cannot then redeem its old token
+            # (consume_refresh_token), so no token outlives the revocation.
+            await self._redis.set(
+                f"refresh_revoked:{family_id}", "1",
+                ex=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
+            )
             family_key = f"refresh_family:{family_id}"
             members = await self._redis.smembers(family_key)
             if members:
diff -ru -x __pycache__ /home/user/fastapi_e-commerce_backend/app/services/stripe_events.py /tmp/claude-val-3b/app/services/stripe_events.py
--- /home/user/fastapi_e-commerce_backend/app/services/stripe_events.py	2026-10-26 20:07:46.216884986 +0000
+++ /tmp/claude-val-3b/app/services/stripe_events.py	2026-10-26 21:53:48.148997398 +0000
@@ -42,6 +42,7 @@
     WebhookEvent,
 )
 from app.models.payment_ledger import DISPUTE_CLOSED_STATUSES, PaymentDispute, PaymentRefund
+from app.models.user import User
 
 logger = logging.getLogger(__name__)
 
@@ -252,6 +253,14 @@
         return False
     if refund_row.status in _REFUND_STATUSES_NOT_RETURNED:
         return False
+    # Stripe does not deliver events in order: a refund.created or
+    # charge.refunded snapshot taken while the refund was still pending can
+    # arrive after the refund.updated that reported it succeeded. A refund
+    # never goes back from succeeded, so such a stale report is ignored.
+    if refund_row.status == "succeeded" and status not in (
+        "succeeded", *_REFUND_STATUSES_NOT_RETURNED,
+    ):
+        return False
     refund_row.status = status
     if status not in _REFUND_STATUSES_NOT_RETURNED:
         return False
@@ -566,6 +575,7 @@
     after_commit = _AfterCommit()
     try:
         data = json.loads(row.payload).get("data")
+        _redact_if_owner_erased(db, row)
         row.status = WEBHOOK_EVENT_PROCESSED
         row.processed_at = utc_now()
         result = apply_event(db, row.event_type, data, after_commit)
@@ -707,6 +717,43 @@
     return {ref for ref in refs if isinstance(ref, str)}
 
 
+def _redact_if_owner_erased(db: Session, row: WebhookEvent) -> None:
+    """
+    Redact the stored payload of an event about a payment of an account
+    whose erasure was requested (committed with the event's processing).
+    The anonymisation task redacts the events stored before it ran, once
+    (redact_users_events), and then skips the account for good: an event
+    stored later - a redelivery, a refund or dispute months later, or one
+    that raced the task - kept the customer's personal data forever.
+    """
+    try:
+        payload = json.loads(row.payload)
+    except (TypeError, ValueError):
+        return
+    if not isinstance(payload, dict) or payload.get("redacted"):
+        return
+    refs = _event_references(payload)
+    order_ids = [int(r[6:]) for r in refs if r.startswith("order:") and r[6:].isdigit()]
+    provider_ids = [r for r in refs if not r.startswith("order:")]
+    about = []
+    if order_ids:
+        about.append(Order.id.in_(order_ids))
+    if provider_ids:
+        about.append(Payment.payment_intent_id.in_(provider_ids))
+        about.append(Payment.checkout_session_id.in_(provider_ids))
+    if not about:
+        return
+    erased = db.scalars(
+        select(User.id)
+        .join(Order, Order.user_id == User.id)
+        .outerjoin(Payment, Payment.order_id == Order.id)
+        .where(User.data_deletion_requested, or_(*about))
+        .limit(1)
+    ).first()
+    if erased is not None:
+        row.payload = redact_event_payload(row.payload)
+
+
 def redact_users_events(db: Session, user_ids: list[int]) -> int:
     """
     Redacts the stored payload of every webhook event about these users'
```

## Final proof (this validator)

| Run | Result | Time |
|---|---|---|
| stateful SQLite dev (2x) | 7 passed, 3 xfailed (both) | 1m41s, 1m30s |
| stateful SQLite ci `-n 4` | 7 passed, 3 xfailed | 1m30s |
| stateful PG dev | 6 passed, 4 xfailed | 1m32s (after lowering) |
| stateful PG ci | 6 passed, 4 xfailed | 3m26s |
| concurrency PG ci (2x, single process) | 92 passed, 5 xfailed | 3m03s, 3m02s |
| concurrency SQLite (`-m timing_sensitive`) | 1 passed, 2 xfailed, 94 skipped | 4s |
| `-k test_bug_` x5 on PG (both modules) | 9 xfailed every time (deterministic) | ~5s each |
| `TORTURE_STRICT=1` stateful SQLite ci | 1 failed (test_refund_events_in_any_order_converge...), 6 passed, 3 xfailed | 1m50s |
| `TORTURE_STRICT=1` concurrency PG | 15 failed (last_unit_contention x8, plain_and_variant x6, refund_same_key_different_amounts[0]), 77 passed, 5 xfailed | 3m12s |

0 failed, 0 XPASS in all non-strict runs. `uv run ruff check tests/torture_suite/` passes.
