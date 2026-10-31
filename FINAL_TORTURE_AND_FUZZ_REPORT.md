# Final Runtime Torture & Stateful Fuzzing Report — fastapi_e-commerce_backend v4.0.3

Branch `claude/elegant-johnson-b5n6yr` · all four phases complete · no change to `app/`, `migrations/`,
existing tests, `pyproject.toml` or `uv.lock`.

## 1. Execution summary

| Module | Cases | SQLite (CI mode) | PostgreSQL 16 (CI mode) |
|---|---|---|---|
| `test_stateful_invariants.py` — 2 `RuleBasedStateMachine`s + 4 `@given` + 4 regressions | 10 | 7 passed, 3 xfailed | 6 passed, 4 xfailed |
| `test_protocol_rfc_torture.py` — NIST/Unicode, RFC 7396, ETag, money, pagination, RFC 9457 | 692 | 646 passed, 46 xfailed | 642 passed, 50 xfailed |
| `test_concurrency_torture.py` — 20–50 simultaneous requests, `asyncio.Barrier`, 5–8 rounds | 97 | 1 passed, 2 xfailed, 94 skipped (PG only) | 92 passed, 5 xfailed |
| `test_boundary_mutants.py` — 47 boundaries at T±1µs / limit±1 | 298 | 288 passed, 10 xfailed | 288 passed, 10 xfailed |
| `test_harness_smoke.py` | 1 | 1 passed | 1 passed |

**Whole suite, CI commands:**
- SQLite `-n 4 -m "not timing_sensitive"`: **942 passed, 59 xfailed** (48 s).
- PG `-n 4`: **937 passed, 64 xfailed** (118 s).
- PG `db_sensitive and timing_sensitive`: **92 passed, 5 xfailed** (190 s).
- Totals: **0 failed, 0 XPASS**. Ruff is clean.

**Volume:**
- Campaign budget (`TORTURE_FULL=1`) per back end:
  - Cart machine: 100 × 30 steps.
  - Payment machine: 150 × 50 steps.
  - 4 `@given` properties × 300 examples.
  - Together that is about 2,000+ state-machine examples and about 25k HTTP requests.
- Protocol module: thousands of `@given` examples on top (200–300 per property).
- Default CI budget is reduced (15 / 20 / 25) so the stateful module runs in ≤ 3.5 min on PG under the `ci` profile.

**Switches:**
- `TORTURE_FULL=1` restores the campaign budgets.
- `TORTURE_STRICT=1` removes every known-bug allowance from the non-deterministic tests. Measured: 15 concurrency failures and 1 stateful failure reappear.

**Markers:**
- The concurrency module is `db_sensitive` and `timing_sensitive`, so CI runs it single-process on PG (PG15 has 100 connections).
- The stateful module is `db_sensitive`.

**Tooling:**
- schemathesis and mutmut are not project dependencies and were not added (doing so would change the lockfile).
- Instead: Hypothesis, plus 46 hand-written operator mutants. 41 are killed; the 2 survivors in the expiry double-check mask each other.

**Fixes verified together:**
- The patches `.cloud_torture/fixes/fixes_3a.patch` and `fixes_3b.patch` apply cleanly with `git apply`.
- With both applied:
  - All 59 + 64 + 5 strict xfails XPASS, so every bug is closed by its fix.
  - Existing suite on SQLite: 5211 passed. Existing suite on PG `db_sensitive`: 2263 passed.
- The SQLite run has 3 expected follow-ups:
  - Regenerate `tests/snapshots/openapi.json`, because B9 adds parameter bounds.
  - Update `test_logout_everywhere_leaves_exactly_these_keys` for the new `refresh_revoked:*` key from C1.
  - The C4 fix first broke `test_webhook_matching[metadata3]` (order id 10**30 overflowed). That is fixed in the patch, and the 63 webhook/GDPR tests are green.

**Verdict:**
- **Proven solid:** the core money and inventory invariants, plus the RFC 9457 error contract.
- **Weak spots:**
  - PostgreSQL lock interaction (a deadlock surfaces as 503).
  - Refund-event ordering.
  - Missing bounds on input depth and number size (500s on PG).
  - ETag wildcard parsing.
  - Refresh/logout atomicity.

## 2. Discovered bugs (19 real, all `xfail(strict=True, reason="BUG DISCOVERED: …")`)

Line numbers are those of the current code (v4.0.3). The fix for each bug is in the appendix patches.

| # | Sev | Bug | Reproducer (`tests/torture_suite/…`) | Production location | Fix (in patch) |
|---|---|---|---|---|---|
| C3 | **H** | A variant-line checkout and a plain-line checkout of the same product deadlock (40P01 surfaces as 503, 23/30 in a burst). `FOR UPDATE` blocks the FK `KEY SHARE` taken by the `order_items` insert. | `test_concurrency_torture.py::test_bug_variant_line_checkout_deadlocks_with_plain_line_checkout` | `app/services/orders.py:84-91` | `with_for_update(key_share=True)` (FOR NO KEY UPDATE) |
| A1 | **H** | A stale `refund.created` / `charge.refunded` "pending" event rewinds a settled refund (succeeded→pending) and the order (refunded→paid). | `test_stateful_invariants.py::test_bug_stale_refund_event_reverts_a_settled_refund` | `app/services/stripe_events.py:253-255` | ignore a non-final report once the refund has succeeded |
| A2 | **H** | `/refund` returns 500 after the provider has already refunded, while an earlier `refund.failed` is unprocessed (`ck_payment_refunded_within_amount`). The money moved and nothing was recorded. | `…::test_bug_refund_after_an_unreported_refund_failure_is_a_500` | `app/api/v1/orders.py:1625→1767` | reconcile `provider.list_refunds` before recording |
| B2 | **H** | Nested JSON of 255+ levels in `characteristics` / `attributes` returns 500. The row is committed first, so every public catalog GET is a 500 afterwards. | `test_protocol_rfc_torture.py::test_bug_deeply_nested_characteristic_is_a_500_and_breaks_the_public_catalog` | `app/core/etag.py:25`; `app/api/v1/admin.py:212,680`; `app/schemas/common.py:87` | `text_input.first_unusable_text` rejects depth > 64 |
| D5 | **H** (PG) | An order total above Numeric(10,2) gives a 500 on PG; on SQLite an unpayable order is stored. Counterexample: 2 × 50,000,000.00. | `test_boundary_mutants.py::test_bug_order_total_past_numeric_10_2_is_not_refused_cleanly` | `app/api/v1/orders.py:366-368` | 422 when total > 99,999,999.99 |
| C1 | M (sec) | A refresh token rotated concurrently with logout survives the logout. | `test_concurrency_torture.py::test_bug_logout_racing_refresh_leaves_a_live_refresh_token` | `app/services/redis_async.py:424`; `app/core/security.py:537-539` | `refresh_revoked:<family>` marker set first and checked on consume |
| C2 | M | An in-flight same-key retry of `POST /orders` gets 400 "Cart is empty" instead of a replay. | `…::test_bug_order_replay_racing_its_first_request_answers_cart_empty` | `app/api/v1/orders.py:206-215` | on empty cart, answer from the order stored under the key |
| C4 | M (GDPR) | Stripe events stored after the erasure purge keep personal data forever. | `…::test_bug_webhook_stored_after_erasure_keeps_personal_data` | `app/tasks.py:341,363`; `app/services/stripe_events.py:438` | `_redact_if_owner_erased` at processing (with an id range guard) |
| A3 | M | Restock overflows the int32 stock column: 500 on cancel and an aborted expiry sweep on PG; > int32 stored on SQLite. | `test_stateful_invariants.py::test_bug_restock_past_int32_max_is_a_500_on_cancel`, `…_aborts_the_expiry_sweep` (PG) | `app/services/orders.py:123,130`; `app/tasks.py:499` | cap restock at INT32_MAX |
| B1 | M | Login returns 401 for accounts registered as `jane@xn--mnchen-3ya.de`, a fullwidth domain or `。`: registration stores the IDNA form, login only applies NFC+lower. | `test_protocol_rfc_torture.py::test_bug_login_does_not_normalise_email_like_registration` | `app/schemas/user.py:104-108` vs `app/api/v1/users.py:124` | canonicalise login email via `email_validator` |
| B3 | M | `NaN` / `Infinity` / `1e400` in free-form JSON: 500 on PG, null on SQLite (RFC 8259 §6). | `…::test_bug_non_finite_numbers_in_free_form_json` | `app/core/text_input.py` (no check) | reject non-finite numbers |
| B4 | M | `*` anywhere in If-Match / If-None-Match is treated as the wildcard: `"stale", *` overwrites concurrent changes (RFC 9110 §13.1.1). | `…::test_bug_if_match_wildcard_inside_a_malformed_field_is_honoured` | `app/core/etag.py:21` | accept `*` only as the whole field, else a well-formed tag list |
| D1 | M (sec) | A logged-out access token is accepted again up to about 1 s before `exp`, because the TTL is floored with `int()`. | `test_boundary_mutants.py::test_bug_logout_blacklist_expires_before_the_access_token` | `app/core/security.py:625` | `math.ceil` |
| B9 | L (PG) | `GET /products?min_price=1e-16384` (or `0E-1000000`) returns 500. | `test_protocol_rfc_torture.py::test_bug_price_filter_beyond_postgres_numeric_is_500` | `app/schemas/product.py:140-141` | `le`, `decimal_places`, zero normalisation |
| B5 | L | Money accepts non-ASCII digits (`"１０.５０"` → 10.50). | `…::test_bug_money_accepts_non_ascii_digits` | `app/schemas/order.py:45,47` | `[0-9]` instead of `\d` |
| B7 | L | `application/json-patch+json` is applied as a merge patch (the docs say 415). | `…::test_bug_json_patch_media_type_is_applied_as_a_merge_patch` | `app/core/text_input.py` (PATCH media type check) | 415 for `+json` other than merge-patch |
| C5 | L | Mock provider only: concurrent same-key refunds all succeed (5 refunds for one key). Live Stripe is unaffected. | `test_concurrency_torture.py::test_bug_mock_provider_refund_key_is_not_atomic` | `app/services/payment/stripe_provider.py:486-532` | `threading.Lock` |
| D2 | L | A client that waits exactly Retry-After is refused again (window effectively +1 ms). | `test_boundary_mutants.py::test_bug_rate_limit_client_waiting_exactly_retry_after_is_refused_again` | `app/core/rate_limit.py:219-221,251-253` | Lua: delete the key when `PTTL == 0` |
| D4 | L | `/legal/data-retention` reports "within retention" for up to a day after the period ends (age floored to days). | `…::test_bug_data_retention_status_truncates_age_to_whole_days` | `app/services/gdpr.py:180-181` | compare `timedelta`s |

**Reclassified as documented policy, not bugs (tests assert the documented behaviour):**
- **B6:** PATCH replaces nested objects wholesale. The docs/OpenAPI say "members present replace the stored value"; this deviates from RFC 7396 §2 deep-merge.
- **B8:** `/users/me` and `/users/addresses` emit no ETag, so If-Match is not evaluated there.
- **D3:** an attempt exactly 5 min old is pinned as stale by the existing test `test_an_attempt_exactly_at_the_stale_limit_does_not`.

**NIST SP 800-63B observations (policy):**
- Passwords are normalised NFC, not NFKC.
- Minimum length is 8, below 800-63B-4's 15 for single-factor.
- The blocklist misses fullwidth / ZWSP spellings.
- The 72-byte cap is below 64 characters for non-ASCII passwords.

## 3. Invariants & boundaries proven solid
- **Inventory:**
  - `initial + admin_adjustments == current + reserved(unpaid live) + sold(paid live)` holds at every state-machine step.
  - Stock is never negative.
  - No oversell in any burst, including the deadlocking ones; no partial orders.
- **Money:**
  - Exactly one charge per order.
  - Refunds never exceed the capture (different keys).
  - The provider total equals the DB, and the ledger is consistent.
  - Order, payment, refund and dispute transitions stay inside the derived graph (except A1).
- **Concurrency:**
  - Refresh reuse: exactly one winner, and the whole family is revoked.
  - Same-key `/orders` and `/pay`: one side effect, identical replays, 422 on a payload mismatch.
  - 30 webhook redeliveries store 1 row; 10 workers apply the event once.
  - Concurrent customer and admin cancels restock exactly once.
  - The GDPR race produces no 5xx and leaves every token refused.
- **Protocol:**
  - NFC/NFD-equivalent passwords log in, and there is no collision past 72 bytes.
  - One canonical email maps to one account.
  - Weak ETags fail If-Match, and If-None-Match uses weak comparison.
  - Explicit `null` on every PATCH field matches the schema; unknown members get 422; non-JSON media types get 415.
  - Pagination up to 2^31-1 is safe, with no id overflow.
  - **Every error response, 500s included, is a valid RFC 9457 `application/problem+json` with no leakage.**
- **Boundaries:**
  - All 47 time and count windows agree between SQLite and PG to the microsecond, including admin date filters at +05:30 and −08:00.
  - JWT `exp` follows RFC 7519.
  - The reconciliation window is inclusive at both ends.

## 4. Artefacts
- `.cloud_torture/01_environment_baseline.md`: environment and baseline.
- `.cloud_torture/02a…02d_*.md`: Phase 2 campaign logs with counterexamples.
- `.cloud_torture/03a_validation_protocol_boundary.md`, `03b_validation_stateful_concurrency.md`: Phase 3 triage and evidence.
- `.cloud_torture/fixes/fixes_3a.patch` (B1–B5, B7, B9, D1, D2, D4, D5), `fixes_3b.patch` (A1–A3, C1–C5): apply with `git apply`, in either order.

## Appendix A — `fixes_3a.patch`
```diff
diff --git a/app/services/gdpr.py b/app/services/gdpr.py
index f02f2d3..c3558f7 100644
--- a/app/services/gdpr.py
+++ b/app/services/gdpr.py
@@ -177,5 +177,4 @@ class GDPRService:
             return True
 
         retention_days = user.data_retention_period or settings.USER_DATA_RETENTION_DAYS
-        age_in_days = (utc_now() - user.created_at).days
-        return age_in_days <= retention_days
+        return utc_now() - user.created_at <= timedelta(days=retention_days)
diff --git a/app/schemas/product.py b/app/schemas/product.py
index c6fa151..e9e0b54 100644
--- a/app/schemas/product.py
+++ b/app/schemas/product.py
@@ -137,10 +137,16 @@ class ProductListResponse(BaseModel):
 class ProductFilter(BaseModel):
     """Schema for product filtering"""
     category_id: Int32 | None = None
-    min_price: Decimal | None = Field(None, ge=0)
-    max_price: Decimal | None = Field(None, ge=0)
+    min_price: Decimal | None = Field(None, ge=0, le=Decimal("99999999.99"), decimal_places=6)
+    max_price: Decimal | None = Field(None, ge=0, le=Decimal("99999999.99"), decimal_places=6)
     in_stock: bool | None = None
     characteristics: dict[str, Any] | None = None
+
+    @field_validator("min_price", "max_price")
+    @classmethod
+    def _zero_has_no_exponent(cls, v):
+        return Decimal(0) if v == 0 else v
+
     # Bounded like ProductSearch.query: a search term, not a document.
     search_query: str | None = Field(None, max_length=100)
     sort_by: str | None = Field(None, pattern="^(name|price|created_at)_(asc|desc)$")
diff --git a/app/schemas/order.py b/app/schemas/order.py
index fa9deaf..54d1391 100644
--- a/app/schemas/order.py
+++ b/app/schemas/order.py
@@ -42,7 +42,7 @@ def _to_cents(value: Decimal) -> Decimal:
 # digits after a point (trailing zeros do not count: 10.500 is 10.50, as
 # ORDER_AMOUNT_DECIMAL_PLACES says). A sign is let through so that a negative
 # amount is refused by its bound (with the message of the bound).
-_MONEY_UNSIGNED = r"\d+(\.\d{1,2}0*)?"
+_MONEY_UNSIGNED = r"[0-9]+(\.[0-9]{1,2}0*)?"
 _MONEY_PATTERN = "-?" + _MONEY_UNSIGNED
 _MONEY_INPUT = re.compile(_MONEY_PATTERN)
 # The same, for the OpenAPI document only, with the whole-part budget of the
diff --git a/app/api/v1/users.py b/app/api/v1/users.py
index 1ae63f8..b5c48ad 100644
--- a/app/api/v1/users.py
+++ b/app/api/v1/users.py
@@ -1,6 +1,7 @@
 import logging
 from typing import Annotated
 
+from email_validator import EmailNotValidError, validate_email
 from fastapi import (
     APIRouter,
     BackgroundTasks,
@@ -120,8 +121,13 @@ async def _user_by_email(db: AsyncSession, email: str):
     lower(email) on the column, not just the normalised input: rows stored
     before emails were normalised keep the spelling their owner typed. The
     comparison is what the unique index on lower(email) covers."""
+    canonical = normalize_email(email)
+    try:  # the spelling registration stores (EmailStr: IDNA / UTS #46 domain)
+        canonical = validate_email(canonical, check_deliverability=False).normalized
+    except EmailNotValidError:
+        pass
     result = await db.execute(
-        select(User).where(func.lower(User.email) == normalize_email(email)).limit(1)
+        select(User).where(func.lower(User.email) == canonical).limit(1)
     )
     return result.scalars().first()
 
diff --git a/app/api/v1/orders.py b/app/api/v1/orders.py
index 3789916..dcfe806 100644
--- a/app/api/v1/orders.py
+++ b/app/api/v1/orders.py
@@ -365,6 +365,13 @@ async def create_order(
 
         total_amount += price_at_time * item.quantity
 
+    if total_amount > Decimal("99999999.99"):  # Order.total_amount is Numeric(10, 2)
+        await db.rollback()
+        raise Problem(
+            status.HTTP_422_UNPROCESSABLE_ENTITY,
+            type=ProblemType.VALIDATION_ERROR,
+            detail="Order total exceeds the maximum amount",
+        )
     order.total_amount = total_amount
     await db.commit()
     await _drop_product_details(redis, products_with_cached_stock(
diff --git a/app/core/text_input.py b/app/core/text_input.py
index 0c638b3..40c1a32 100644
--- a/app/core/text_input.py
+++ b/app/core/text_input.py
@@ -28,6 +28,7 @@ It is applied once per kind of input, not per field:
   where that JSON is parsed.
 """
 
+import math
 import re
 from collections.abc import Iterable
 from typing import Any
@@ -41,6 +42,7 @@ NUL_MESSAGE = "String must not contain NUL characters"
 SURROGATE_TYPE = "invalid_unicode"
 SURROGATE_MESSAGE = "String must be valid Unicode"
 
+MAX_JSON_DEPTH = 64
 _NUL = re.compile("\x00")
 _SURROGATE = re.compile("[\ud800-\udfff]")
 
@@ -69,6 +71,10 @@ def first_unusable_text(value: Any, loc: tuple[str | int, ...] = ()) -> dict | N
     pending = [(value, loc)]
     while pending:
         item, where = pending.pop()
+        if isinstance(item, float) and not math.isfinite(item):
+            return {"type": "finite_number", "loc": where, "msg": "Number must be finite"}
+        if isinstance(item, (dict, list, tuple)) and len(where) - len(loc) > MAX_JSON_DEPTH:
+            return {"type": "too_deep", "loc": where, "msg": "Value is nested too deeply"}
         if isinstance(item, str):
             found = _problem(item)
             if found:
@@ -104,6 +110,10 @@ async def reject_unusable_text(request: Request) -> None:
     reason to accept it. Raised as RequestValidationError, so the answer is
     the same 422 problem as any other bad parameter. It runs ahead of the
     route's own dependencies, so such a request is a 422 before it is a 401."""
+    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
+    if request.method == "PATCH" and media_type.endswith("+json") and media_type != "application/merge-patch+json":
+        from app.core.errors import _unsupported_media_type  # RFC 5789 2.2: a patch format we do not know
+        raise _unsupported_media_type(request)
     sources = [("path", request.path_params), ("query", _by_name(request.query_params.multi_items()))]
     route = request.scope.get("route")
     body_field = getattr(route, "body_field", None)
diff --git a/app/core/security.py b/app/core/security.py
index d52a5d2..b21a66d 100644
--- a/app/core/security.py
+++ b/app/core/security.py
@@ -1,6 +1,7 @@
 import asyncio
 import hashlib
 import hmac
+import math
 import os
 import re
 import time
@@ -622,7 +623,7 @@ async def logout(access_token: str, refresh_token: str | None = None) -> None:
         )
         # time.time(), not a naive UTC datetime's timestamp(): the latter reads a
         # naive UTC value as local time and is off by the host's UTC offset.
-        expires_in = max(int(payload["exp"] - time.time()), 1)
+        expires_in = max(math.ceil(payload["exp"] - time.time()), 1)
     except jwt.PyJWTError:
         expires_in = 1
     # The access token was authenticated by the route a moment ago, so it
diff --git a/app/core/etag.py b/app/core/etag.py
index 7ae1f94..db5fb7b 100644
--- a/app/core/etag.py
+++ b/app/core/etag.py
@@ -18,7 +18,8 @@ from pydantic import BaseModel
 IfMatch = Annotated[str | None, Header(description="Entity tag(s) the current resource must match, or *")]
 IfNoneMatch = Annotated[str | None, Header(description="Entity tag(s) the client already holds, or *")]
 
-_TAG = re.compile(r'(W/)?"([^"]*)"|\*')
+_ENTITY = re.compile(r'(W/)?"([^"]*)"')
+_LIST = re.compile(r'[ \t,]*(?:W/)?"[^"]*"(?:[ \t]*,[ \t,]*(?:W/)?"[^"]*")*[ \t,]*')
 
 
 def etag_for(model: BaseModel) -> str:
@@ -28,7 +29,12 @@ def etag_for(model: BaseModel) -> str:
 
 def _parse(header: str) -> list[tuple[bool, str | None]]:
     """(is_weak, opaque_tag) pairs; a bare '*' is (False, None)."""
-    return [(bool(m.group(1)), m.group(2)) for m in _TAG.finditer(header)]
+    value = header.strip(" \t")
+    if value == "*":
+        return [(False, None)]
+    if not _LIST.fullmatch(value):  # RFC 9110: "*" or a comma-separated list of entity-tags
+        return []
+    return [(bool(m.group(1)), m.group(2)) for m in _ENTITY.finditer(value)]
 
 
 def if_match_passes(header: str | None, current: str) -> bool:
diff --git a/app/core/rate_limit.py b/app/core/rate_limit.py
index a25f4a4..143abac 100644
--- a/app/core/rate_limit.py
+++ b/app/core/rate_limit.py
@@ -214,6 +214,10 @@ def user_id_from_token(request: Request) -> str | None:
 # because INCR and PEXPIRE must not be separable: a key that got its INCR
 # but never its expiry would count, and block, forever.
 _COUNT_LUA = """
+-- Redis keeps a key alive AT its expiry instant (PTTL 0); the window is over.
+if redis.call('PTTL', KEYS[1]) == 0 then
+    redis.call('DEL', KEYS[1])
+end
 local count = redis.call('INCR', KEYS[1])
 local ttl = redis.call('PTTL', KEYS[1])
 if ttl < 0 then
```

## Appendix B — `fixes_3b.patch`
```diff
diff --git a/app/services/payment/stripe_provider.py b/app/services/payment/stripe_provider.py
index 2ff0912..27fe9e5 100644
--- a/app/services/payment/stripe_provider.py
+++ b/app/services/payment/stripe_provider.py
@@ -18,6 +18,7 @@ usage: https://docs.stripe.com/testing
 """
 import hashlib
 import hmac
+import threading
 import time
 import uuid
 from typing import Any, ClassVar
@@ -218,6 +219,10 @@ class StripePaymentProvider(PaymentProvider):
     _sessions_by_id: ClassVar[dict[str, CheckoutSession]] = {}
     _refunds_by_key: ClassVar[dict[str, Refund]] = {}
     _refunds_by_id: ClassVar[dict[str, Refund]] = {}
+    # Real Stripe checks an Idempotency-Key and records its result as one
+    # step; the routes call this mock from a thread pool, so concurrent
+    # requests with one key both missed the cache and both refunded.
+    _refund_lock: ClassVar[threading.Lock] = threading.Lock()
 
     @classmethod
     def reset_mock_state(cls) -> None:
@@ -463,6 +468,19 @@ class StripePaymentProvider(PaymentProvider):
         payment_intent_id: str,
         amount: int | None = None,
         idempotency_key: str | None = None,
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
+        amount: int | None = None,
+        idempotency_key: str | None = None,
     ) -> Refund:
         if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
             raise InvalidRequestError(
diff --git a/app/services/redis_async.py b/app/services/redis_async.py
index 3309e8d..de5a044 100644
--- a/app/services/redis_async.py
+++ b/app/services/redis_async.py
@@ -416,13 +416,27 @@ class AsyncRedisService:
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
diff --git a/app/services/orders.py b/app/services/orders.py
index 40a67e8..98c49aa 100644
--- a/app/services/orders.py
+++ b/app/services/orders.py
@@ -9,12 +9,13 @@ cancellation, a customer cancelling their own unpaid order, and the expiry
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
@@ -77,20 +78,39 @@ def lock_stock_rows(db: Session, lines) -> None:
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
@@ -120,14 +140,14 @@ def cancel_order_and_restock(db: Session, order: Order) -> bool:
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
diff --git a/app/services/stripe_events.py b/app/services/stripe_events.py
index 71a03aa..2237c6a 100644
--- a/app/services/stripe_events.py
+++ b/app/services/stripe_events.py
@@ -42,6 +42,7 @@ from app.models.payment import (
     WebhookEvent,
 )
 from app.models.payment_ledger import DISPUTE_CLOSED_STATUSES, PaymentDispute, PaymentRefund
+from app.models.user import User
 
 logger = logging.getLogger(__name__)
 
@@ -252,6 +253,14 @@ def _apply_refund_object(db: Session, payment_row: Payment, refund_object: dict)
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
@@ -566,6 +575,7 @@ def process_stored_event(db: Session, event_id: str) -> EventOutcome:
     after_commit = _AfterCommit()
     try:
         data = json.loads(row.payload).get("data")
+        _redact_if_owner_erased(db, row)
         row.status = WEBHOOK_EVENT_PROCESSED
         row.processed_at = utc_now()
         result = apply_event(db, row.event_type, data, after_commit)
@@ -707,6 +717,48 @@ def _event_references(payload: dict) -> set[str]:
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
+    # An order id outside the column's range names no order of ours (and
+    # would overflow the query parameter), as in _order_from_metadata.
+    order_ids = [
+        int(r[6:]) for r in refs
+        if r.startswith("order:") and r[6:].isdigit() and 0 < int(r[6:]) <= 2**31 - 1
+    ]
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
diff --git a/app/api/v1/orders.py b/app/api/v1/orders.py
index 3789916..5d2d4d0 100644
--- a/app/api/v1/orders.py
+++ b/app/api/v1/orders.py
@@ -77,6 +77,7 @@ from app.services.payment.exceptions import (
     CardError,
     IdempotencyError,
     InvalidRequestError,
+    PaymentProviderError,
     PaymentProviderTimeoutError,
     SignatureVerificationError,
 )
@@ -210,6 +211,13 @@ async def create_order(
 
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
@@ -1609,12 +1617,30 @@ async def refund_payment(
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
```
