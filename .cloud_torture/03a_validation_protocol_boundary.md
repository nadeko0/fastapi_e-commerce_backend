# 03a Validation: protocol & boundary torture

Files: tests/torture_suite/test_protocol_rfc_torture.py, tests/torture_suite/test_boundary_mutants.py (only these edited).

## Verdicts

| Claim | Verdict | Reason |
|---|---|---|
| B1 login 401 for IDNA/fullwidth/ideographic-dot emails | REAL (medium) | login `_user_by_email` (users.py:124) uses normalize_email only; registration stores EmailStr/IDNA form (user.py:104-108) |
| B2 deep JSON (>=255) 500 + poisons catalog | REAL (high: any admin write DoSes public catalog) | no depth bound; ProductResponse serialisation fails after commit |
| B3 NaN/Infinity/1e400 in free-form JSON | REAL (medium) | PG 500 / SQLite stores null |
| B4 `*` anywhere in If-Match/If-None-Match is wildcard | REAL (medium: lost-update protection bypass) | etag.py `_TAG.finditer` |
| B5 non-ASCII digits in money | REAL (low) | `\d` on str (order.py:45,47); published OpenAPI pattern is ASCII |
| B6 PATCH replaces nested objects | POLICY | docs/API.md "Properties included in the patch replace stored values", PATCH_DESCRIPTION "Members present replace the stored value". Tests now assert replace-wholesale (note: deviates from RFC 7396 section 2 recursion) |
| B7 json-patch+json accepted | REAL (low) | docs/API.md and errors.py: only merge-patch+json / application/json; RFC 5789 2.2 -> 415 |
| B8 If-Match ignored on /users/me, addresses | POLICY | docs/API.md scopes If-Match to product/variant endpoints; users routes emit no ETag. Test asserts 200 and no ETag |
| B9 PG-only 500 on `min_price=1e-16384` | REAL (low, PG only) | unbounded Decimal filters (product.py:140-141) |
| D1 blacklist TTL floors | REAL (medium) | security.py:625; only sub-second parts fail (the whole-second param passes, now unmarked) |
| D2 refused at exactly Retry-After | REAL (low) | Redis keeps key alive AT expiry instant (PTTL 0) |
| D3 attempt exactly 5 min old counted stale | POLICY | existing test tests/integration/test_payment_webhook_mutants.py::test_an_attempt_exactly_at_the_stale_limit_does_not pins "exactly at the limit is stale"; tests converted to assert it (orders 200, events False) |
| D4 retention status floors days | REAL (low) | gdpr.py:180-181 |
| D5 order total > Numeric(10,2) | REAL (high on PG: 500; SQLite stores unpayable order) | orders.py:366-368 |

## Test changes
Protocol: `bug()` strict-xfail markers: B1 (3 params), B2 (1), B3 (10 params), B4 (table idx 15-18 + wildcard repro), B5 (4 table inputs x4 + repro), B7 (4), B9 (PG only, 4). Hypothesis tests keep known-bug inputs out unless `TORTURE_STRICT=1` (login-roundtrip domains, nesting depth >=200, If-Match malformed with `*`, non-ASCII digits in money fuzz, PG numeric-range price filters); their counterexample `@example`s are strict-only. Test bug in `_json_scalars` (NUL in generated text -> 422) fixed. B6/B8 tests renamed/rewritten to assert documented behavior.
Boundary: strict xfail D1 (3 sub-second params), D2, D4 (3), D5 (3); D3 rewritten as policy.

## Verified fixes (applied in /tmp/claude-val-3a; every bug reproducer passes under --runxfail on SQLite; B9 on PG; full existing suite excl. torture: 5235 passed, 109 skipped with fixes; D3 not applied)
Also checked: `tests -k "price or filter or product"` 488 passed.

```diff
diff -u -r '--exclude=__pycache__' /home/user/fastapi_e-commerce_backend/app/api/v1/orders.py /tmp/claude-val-3a/app/api/v1/orders.py
--- /home/user/fastapi_e-commerce_backend/app/api/v1/orders.py	2026-10-26 20:07:46.206925433 +0000
+++ /tmp/claude-val-3a/app/api/v1/orders.py	2026-10-31 13:29:37.309658526 +0000
@@ -365,6 +365,13 @@
 
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
diff -u -r '--exclude=__pycache__' /home/user/fastapi_e-commerce_backend/app/api/v1/users.py /tmp/claude-val-3a/app/api/v1/users.py
--- /home/user/fastapi_e-commerce_backend/app/api/v1/users.py	2026-10-26 20:07:46.206925433 +0000
+++ /tmp/claude-val-3a/app/api/v1/users.py	2026-10-31 13:27:04.460084578 +0000
@@ -1,6 +1,7 @@
 import logging
 from typing import Annotated
 
+from email_validator import EmailNotValidError, validate_email
 from fastapi import (
     APIRouter,
     BackgroundTasks,
@@ -120,8 +121,13 @@
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
 
diff -u -r '--exclude=__pycache__' /home/user/fastapi_e-commerce_backend/app/core/etag.py /tmp/claude-val-3a/app/core/etag.py
--- /home/user/fastapi_e-commerce_backend/app/core/etag.py	2026-10-26 20:07:46.210224855 +0000
+++ /tmp/claude-val-3a/app/core/etag.py	2026-10-31 13:27:04.458961972 +0000
@@ -18,7 +18,8 @@
 IfMatch = Annotated[str | None, Header(description="Entity tag(s) the current resource must match, or *")]
 IfNoneMatch = Annotated[str | None, Header(description="Entity tag(s) the client already holds, or *")]
 
-_TAG = re.compile(r'(W/)?"([^"]*)"|\*')
+_ENTITY = re.compile(r'(W/)?"([^"]*)"')
+_LIST = re.compile(r'[ \t,]*(?:W/)?"[^"]*"(?:[ \t]*,[ \t,]*(?:W/)?"[^"]*")*[ \t,]*')
 
 
 def etag_for(model: BaseModel) -> str:
@@ -28,7 +29,12 @@
 
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
diff -u -r '--exclude=__pycache__' /home/user/fastapi_e-commerce_backend/app/core/rate_limit.py /tmp/claude-val-3a/app/core/rate_limit.py
--- /home/user/fastapi_e-commerce_backend/app/core/rate_limit.py	2026-10-26 20:07:46.210224855 +0000
+++ /tmp/claude-val-3a/app/core/rate_limit.py	2026-10-31 13:25:38.047536743 +0000
@@ -214,6 +214,10 @@
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
diff -u -r '--exclude=__pycache__' /home/user/fastapi_e-commerce_backend/app/core/security.py /tmp/claude-val-3a/app/core/security.py
--- /home/user/fastapi_e-commerce_backend/app/core/security.py	2026-10-26 20:07:46.210736635 +0000
+++ /tmp/claude-val-3a/app/core/security.py	2026-10-31 13:25:10.270544678 +0000
@@ -1,6 +1,7 @@
 import asyncio
 import hashlib
 import hmac
+import math
 import os
 import re
 import time
@@ -622,7 +623,7 @@
         )
         # time.time(), not a naive UTC datetime's timestamp(): the latter reads a
         # naive UTC value as local time and is off by the host's UTC offset.
-        expires_in = max(int(payload["exp"] - time.time()), 1)
+        expires_in = max(math.ceil(payload["exp"] - time.time()), 1)
     except jwt.PyJWTError:
         expires_in = 1
     # The access token was authenticated by the route a moment ago, so it
diff -u -r '--exclude=__pycache__' /home/user/fastapi_e-commerce_backend/app/core/text_input.py /tmp/claude-val-3a/app/core/text_input.py
--- /home/user/fastapi_e-commerce_backend/app/core/text_input.py	2026-10-26 20:07:46.210736635 +0000
+++ /tmp/claude-val-3a/app/core/text_input.py	2026-10-31 13:27:31.112959231 +0000
@@ -28,6 +28,7 @@
   where that JSON is parsed.
 """
 
+import math
 import re
 from collections.abc import Iterable
 from typing import Any
@@ -41,6 +42,7 @@
 SURROGATE_TYPE = "invalid_unicode"
 SURROGATE_MESSAGE = "String must be valid Unicode"
 
+MAX_JSON_DEPTH = 64
 _NUL = re.compile("\x00")
 _SURROGATE = re.compile("[\ud800-\udfff]")
 
@@ -69,6 +71,10 @@
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
@@ -104,6 +110,10 @@
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
diff -u -r '--exclude=__pycache__' /home/user/fastapi_e-commerce_backend/app/schemas/order.py /tmp/claude-val-3a/app/schemas/order.py
--- /home/user/fastapi_e-commerce_backend/app/schemas/order.py	2026-10-26 20:07:46.214504056 +0000
+++ /tmp/claude-val-3a/app/schemas/order.py	2026-10-31 13:27:04.457908268 +0000
@@ -42,7 +42,7 @@
 # digits after a point (trailing zeros do not count: 10.500 is 10.50, as
 # ORDER_AMOUNT_DECIMAL_PLACES says). A sign is let through so that a negative
 # amount is refused by its bound (with the message of the bound).
-_MONEY_UNSIGNED = r"\d+(\.\d{1,2}0*)?"
+_MONEY_UNSIGNED = r"[0-9]+(\.[0-9]{1,2}0*)?"
 _MONEY_PATTERN = "-?" + _MONEY_UNSIGNED
 _MONEY_INPUT = re.compile(_MONEY_PATTERN)
 # The same, for the OpenAPI document only, with the whole-part budget of the
diff -u -r '--exclude=__pycache__' /home/user/fastapi_e-commerce_backend/app/schemas/product.py /tmp/claude-val-3a/app/schemas/product.py
--- /home/user/fastapi_e-commerce_backend/app/schemas/product.py	2026-10-26 20:07:46.214504056 +0000
+++ /tmp/claude-val-3a/app/schemas/product.py	2026-10-31 13:32:43.328656096 +0000
@@ -137,10 +137,16 @@
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
diff -u -r '--exclude=__pycache__' /home/user/fastapi_e-commerce_backend/app/services/gdpr.py /tmp/claude-val-3a/app/services/gdpr.py
--- /home/user/fastapi_e-commerce_backend/app/services/gdpr.py	2026-10-26 20:07:46.215794779 +0000
+++ /tmp/claude-val-3a/app/services/gdpr.py	2026-10-31 13:25:10.271266188 +0000
@@ -177,5 +177,4 @@
             return True
 
         retention_days = user.data_retention_period or settings.USER_DATA_RETENTION_DAYS
-        age_in_days = (utc_now() - user.created_at).days
-        return age_in_days <= retention_days
+        return utc_now() - user.created_at <= timedelta(days=retention_days)
```

Note D2: the first attempt (ceil((ttl_ms+1)/1000)) broke the exact-second Retry-After ceil tests; the Lua variant above keeps them green (rate_limit tests: 236 passed).

## Final runs (dev profile unless stated)
| File | SQLite run1 | SQLite run2 | PG run1 | PG run2 | CI profile -n 4 SQLite |
|---|---|---|---|---|---|
| protocol | 646 passed, 46 xfailed (59s) | same (58s) | 642 passed, 50 xfailed (127s) | same (130s) | 646 passed, 46 xfailed (36s) |
| boundary | 288 passed, 10 xfailed (11.5s) | same | 288 passed, 10 xfailed (24.5s) | same (23s) | 288 passed, 10 xfailed (8.8s) |

0 failed, 0 XPASS in all runs. `uv run ruff check tests/torture_suite/` passes.
