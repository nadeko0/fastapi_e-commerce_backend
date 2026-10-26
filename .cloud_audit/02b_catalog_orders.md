# 02B — Catalog, cart, orders: locking, stock, ETag, merge-patch, state machine

Auditor: Subagent 2B. Target: v4.0.3. Method: read every in-scope file line by line, then ran reproductions:
on a local **PostgreSQL 16** (raw driver script and full HTTP path with the repo's own `conftest.py`, `TEST_DATABASE_URL`)
and on the default SQLite suite harness. Nothing under `app/`, `migrations/` or `tests/` was modified. Scratch
reproductions are in
`/tmp/claude-0/-home-user-fastapi-e-commerce-backend/d6bb2a49-b210-591b-b7be-8a1e71874dff/scratchpad/`
(`deadlock_fk.py`, `repro/tests/integration/test_postgres_2b_fk_deadlock.py`, `repro/tests/integration/test_2b_repro.py`).

## 1. Scope and files read

| ✔ | File | Notes |
|---|---|---|
| ✔ | app/api/v1/products.py (404) | all |
| ✔ | app/api/v1/cart.py (679) | all |
| ✔ | app/api/v1/orders.py (1920) | all; create/cancel/status/pay/checkout-session/refund/webhook |
| ✔ | app/api/v1/admin.py (1071) | all, including the locking after line 865 (webhook replay) |
| ✔ | app/api/v1/users.py | login and merge path (297-409), user orders (889-925) |
| ✔ | app/services/orders.py (133) | all |
| ✔ | app/services/redis.py | cart, merge and remove-ordered section (130-420) |
| ✔ | app/services/redis_async.py (529) | all |
| ✔ | app/services/redis_codec.py (326) | all |
| ✔ | app/tasks.py | expiry (461-534), idempotency-key sweep (685-752), beat schedule, email tasks |
| ✔ | app/services/stripe_events.py | every lock site (105-166, 183-217, 270-345, 531-605, 613-649) |
| ✔ | app/services/reconciliation.py | writes only (discrepancy rows; no order, payment or stock locks) |
| ✔ | app/models/product.py, category.py, order.py, order_items.py, enums.py | all |
| ✔ | app/models/base.py | type map: Decimal -> Numeric(10,2), datetime -> UTCDateTime |
| ✔ | app/schemas/product.py, category.py, cart.py, common.py, order.py | all |
| ✔ | app/api/merge_patch.py, app/core/etag.py, sorting.py, head.py, location.py | all |
| ✔ | app/core/database.py | error mapping 120-205 (relevant to how a deadlock surfaces) |
| ✔ | app/services/payment/live_stripe_provider.py | create_checkout_session (336-380) only |
| ✔ | migrations 5fe510d5d45e (variants), b6d7b96c6d17 (variant_id), 76d14f9f12e5 (CHECKs) | all |
| ✔ | tests/conftest.py, tests/integration/test_postgres_only.py (lock tests), test_etag.py, test_orders.py (status) | for reproduction and coverage gaps |

Every `with_for_update` / `FOR UPDATE` / `skip_locked` / `nowait` in `app/` (grep) is analysed in §2. No `nowait` exists.

## 2. Lock-order matrix (PostgreSQL; SQLite does not emit these locks)

"FU" = `FOR UPDATE`; "KS" = the `FOR KEY SHARE` that PostgreSQL's RI trigger takes on the referenced row for each FK
INSERT/UPDATE (`SELECT 1 FROM ONLY products x WHERE id=$1 FOR KEY SHARE OF x`); "NKU" = the `FOR NO KEY UPDATE` an
ordinary UPDATE takes.

| # | Path | file:line | Rows locked, in acquisition order | Deterministic order? |
|---|---|---|---|---|
| L1 | Checkout `POST /orders` | orders.py:239-263, 284-286, 331-348, 357-369; services/orders.py:83-91 | INSERT orders (KS users, addresses) -> **products FU** (plain lines, `ORDER BY id`) -> **variants FU** (`ORDER BY id`) -> UPDATE stock (already held) -> at commit (autoflush off, database.py:84/109) INSERT order_items: **KS on products of *every* line, including variant lines whose product was NOT pre-locked**, KS variants | Sorted within each table, but the commit-time KS on products comes *after* the variant FU locks: **products -> variants -> products**. See 2B-01 |
| L2 | Customer cancel `POST /orders/{id}/cancel` | orders.py:545-550, 563; services/orders.py:117-132 | orders FU (`of=Order`, 1 row) -> products FU sorted -> variants FU sorted -> UPDATE (NKU, held) | Yes |
| L3 | Admin `PUT /orders/{id}/status` -> cancelled | orders.py:461-464, 496 | same as L2 | Yes |
| L4 | Admin status -> other | orders.py:461-464, 498 | orders FU only | n/a (1 row) |
| L5 | Expiry job | tasks.py:493-499 | per order, own txn: orders FU SKIP LOCKED -> products FU -> variants FU | Yes (one order per txn) |
| L6 | Admin `PUT/PATCH /admin/products/{id}` | admin.py:253-258, 291 | with If-Match: products FU (`of=Product`, joined category not locked) -> UPDATE; without: UPDATE only (NKU) | n/a (1 row) |
| L7 | Admin `PUT/PATCH .../variants/{id}` | admin.py:801-811, 838 | with If-Match: variants FU -> UPDATE (KS products if product_id changed: not updatable) | n/a |
| L8 | Admin `DELETE /admin/products/{id}` | admin.py:329-349 | soft: UPDATE product; hard: ORM cascade DELETE variants then DELETE product | variants -> product, see 2B-08 |
| L9 | Admin create variant | admin.py:664-672 | INSERT variant (KS product) | n/a |
| L10 | `/pay`, `/checkout-session` | orders.py:1043-1048, 1354-1359 | orders FU (no joins) -> INSERT/UPDATE payments (KS orders, own row) | Yes |
| L11 | Payment outcome (`_lock_order_and_reload`) | orders.py:805-807 | orders FU (`refresh(with_for_update=True)`, no eager joins) -> payments UPDATE | Yes |
| L12 | Admin refund | orders.py:1531-1587 (no lock), 1617-1636 | orders FU -> payments UPDATE -> payment_refunds INSERT (KS payments) | Yes |
| L13 | Stripe event worker | stripe_events.py:531-541, 341, 131, 209 | webhook_events FU SKIP LOCKED -> orders FU -> payments UPDATE / refunds, disputes INSERT | Yes |
| L14 | Event failure / admin replay | stripe_events.py:585; admin.py:1046-1051 | webhook_events FU (1 row) | n/a |
| L15 | Stripe sweeper | stripe_events.py:625-648 | bulk UPDATE webhook_events (NKU, IN list, no ORDER BY) | No, but nothing else holds two event rows (the worker uses SKIP LOCKED) |
| L16 | Idempotency-key sweep | tasks.py:719-740 | bulk UPDATE orders (NKU, up to 100, IN list) | No, but no other path holds two order rows |
| L17 | Login | users.py:378-384 | UPDATE users (NKU) | n/a; Redis merge takes no DB locks |

Cross-table order: every path that holds an existing order locks **order -> stock** (L2, L3, L5) or **order -> payments**
(L10-L13). No path locks stock or payments before an existing order. Checkout (L1) never locks an *existing* order (its
own is a new row). The joined eager loads under `with_for_update(of=Order)` are restricted to `orders`; the
`selectinload(Order.items)` query is **not** locked (verified on PG by echo: `FOR UPDATE OF orders` on the main query,
plain SELECT for the items). The single inconsistency is the commit-time FK KEY SHARE in L1 (2B-01), and the
cascade order in L8 (2B-08, which leads to a 500, not a deadlock).

## 3. Summary

| ID | Title | Severity | Confidence |
|---|---|---|---|
| 2B-01 | `lock_stock_rows` takes `FOR UPDATE`, which conflicts with the FK KEY SHARE taken at commit by `order_items` INSERTs: checkout/checkout and checkout/cancel/expiry deadlock (shows up as a 503 "database unavailable") | **Medium** | Confirmed (PG 16, raw and full HTTP) |
| 2B-02 | The PostgreSQL lock-order regression tests attach their hold to the sync engine; the async routes never fire it, so those tests pass without the interleaving they claim to force | **Medium** (test gap) | Confirmed (hook saw 0 statements) |
| 2B-03 | Unpaid-order expiry and customer cancel ignore in-flight payment attempts, and open Checkout Sessions are never expired (Stripe default 24 h): customers are charged for orders already cancelled and restocked | **High** (business/money) | Confirmed by reading |
| 2B-04 | PATCH is advertised as RFC 7396 but nested objects (`characteristics`, `attributes`) are replaced, not merged; `null` members are 422 (characteristics) or stored as `null` (variant attributes) | Medium | Confirmed (test) |
| 2B-05 | Admin stock is an absolute set with optional If-Match; the ETag covers `stock_quantity`/`updated_at`, which every sale changes. Under live traffic If-Match nearly always fails, so admins fall back to unconditional writes that overwrite concurrent decrements (lost update -> oversell) | Medium | Confirmed by reading (design) |
| 2B-06 | `SENT -> CANCELLED` restocks goods that have physically left; a paid order cancelled by an admin is neither refunded nor flagged | Medium (business) | Confirmed by reading (pinned by test_orders.py:678-682) |
| 2B-07 | Order total can exceed `Numeric(10,2)`: a legitimate large order answers 500 | Low | Confirmed by reading |
| 2B-08 | Hard `DELETE /admin/products/{id}` races checkout: count-then-delete answers 500 (FK violation) | Low | Confirmed by reading |
| 2B-09 | Guest cart merge silently discards lines over `CART_MAX_LINES` (guest key deleted, user not told) | Low | Confirmed by reading |
| 2B-10 | Refund: open-dispute check runs before the provider call and outside the order lock | Low | Confirmed by reading |
| 2B-11 | One deadlock or DB error aborts the whole expiry sweep (no per-order isolation, no retry) | Low | Confirmed by reading |
| 2B-12 | Variant-line checkout reads the parent product's `is_active`/`price` without a lock | Low | Confirmed by reading |
| 2B-13 | Informational items (product-level vs variant stock semantics, cache-aside staleness, live product name on old orders, PUT that is not a full replacement, CHECK migration lock) | Info | — |

---

## 4. Detailed findings

### 2B-01 — FOR UPDATE stock locks vs FK KEY SHARE: deadlock between checkouts, and between a checkout and a cancel or expiry (Medium, Confirmed)

**Code**

`app/services/orders.py:81-91`
```python
product_ids = sorted({product_id for product_id, variant_id in lines if variant_id is None})
variant_ids = sorted({variant_id for _, variant_id in lines if variant_id is not None})
db.execute(
    select(Product.id).where(Product.id.in_(product_ids)).order_by(Product.id).with_for_update()
).all()
db.execute(
    select(ProductVariant.id) ... .order_by(ProductVariant.id).with_for_update()
).all()
```
`app/api/v1/orders.py:357-369`: the `OrderItem(product_id=product.id, variant_id=...)` rows are only `db.add`-ed; with
`autoflush=False` (`app/core/database.py:84,109`) they are INSERTed by `await db.commit()`, **after** all the stock locks.

**Mechanism.** `SELECT ... FOR UPDATE` takes the strongest row lock, which conflicts with `FOR KEY SHARE`. Each
`order_items` INSERT runs PostgreSQL's RI check (`SELECT 1 FROM ONLY products x WHERE id=$1 FOR KEY SHARE OF x`) on the
line's **product**, and that includes variant lines. A variant line's product is never in `product_ids`, so
transaction A takes that KEY SHARE *after* it already holds its variant FU locks. The documented "products, then
variants" order is therefore really "products, then variants, then products".

The cart allows a plain line and a variant line of the same product (`cart.py:455-478` never refuses a plain line for
a product that has variants; reproduced in `test_2b_repro.py::test_plain_line_allowed_for_product_with_variants`).

**Interleavings, reproduced on PostgreSQL 16:**

1. Checkout vs checkout. A's cart is `[variant V of P]`; B's cart is `[plain P, variant V]`.
   - A: `lock_stock_rows` takes V FU.
   - B: takes P FU, then waits on V.
   - A commits: the INSERT of `order_items(product_id=P)` needs KS on P, which B holds FU.
   - Result: `DeadlockDetected`.
2. Checkout vs cancel (also the expiry job). An existing order has lines `[plain P, variant V]`; the cancel locks the
   order, then P FU, then waits on V. A (cart `[V]`) holds V FU and needs KS on P at commit. **Deadlock.**
3. More generally, any checkout holding variant `Vx` and any cancel/checkout holding `P = product(Vy)` FU while waiting
   on `Vx` deadlock. This needs a shop where some product is sold both as a plain line and with variants. That happens
   in practice: products sold plain before variants were added still have plain NEW orders. A shop that sells only
   variants, or only plain products, is not affected.

Full HTTP run (`repro/tests/integration/test_postgres_2b_fk_deadlock.py`, hook on `async_engine.sync_engine`):
```
psycopg.errors.DeadlockDetected: deadlock detected
CONTEXT: while locking tuple (0,1) in relation "products"
SQL statement "SELECT 1 FROM ONLY "public"."products" x WHERE "id" OPERATOR(pg_catalog.=) $1 FOR KEY SHARE OF x"
[SQL: INSERT INTO order_items (...)]  ->  HTTP/1.1 503 Service Unavailable
```
Both scenarios failed: `POST /orders` -> 503, and `POST /orders/{id}/cancel` -> 503.

**Impact.** One transaction is rolled back. Psycopg's `DeadlockDetected` is an `OperationalError`, so
`is_database_unavailable_error` (`database.py:140`) turns it into a **503 "database temporarily unavailable"** and
logs it as an outage, which is a misleading ops signal. A retry is safe: the idempotency key or the status re-check
covers it. In the expiry task the exception aborts the whole sweep (2B-11). There is no data corruption, but this
breaks the design promise in `lock_stock_rows`'s docstring and the incident looks like a DB outage.

**Disproved alternatives.** The CHECKs and the conditional UPDATE are not involved. SQLite does not reproduce this
(no row locks). The existing PG tests do not catch it: they cover plain products only, and their hold hook never
fires (2B-02).

**Fix (minimal, verified).** Take `FOR NO KEY UPDATE`, which still serialises stock writers but does not conflict
with FK KEY SHARE. The stock UPDATEs never change a key. Re-running scenario 1 with this change gave
`{'A': 'committed', 'B': 'committed'}`.
```diff
-        select(Product.id).where(Product.id.in_(product_ids)).order_by(Product.id).with_for_update()
+        select(Product.id).where(Product.id.in_(product_ids)).order_by(Product.id)
+        .with_for_update(key_share=True)          # FOR NO KEY UPDATE
 ...
-        .with_for_update()
+        .with_for_update(key_share=True)
```
Apply the same to `admin.py:257` and `admin.py:810` (`with_for_update(of=Product, key_share=True)`). Their FU
currently blocks every concurrent checkout's FK check on that product for the length of the admin request.
Alternative: also lock the products of variant lines (`FOR KEY SHARE`, sorted, in the products phase).

**Pytest sketch** (PG-only; put it in `tests/integration/test_postgres_only.py` style and hook the **async** engine):
```python
from conftest import async_engine, requires_postgres
def test_variant_checkout_vs_plain_plus_variant_checkout(client, db_session):
    shop = Shop(client, db_session); p = shop.product(stock=5); v = shop.variant(p, stock=5)
    a, b = shop.buyer(), shop.buyer()
    shop.add_to_cart(a, p, variant=v)                       # A: [V]
    shop.add_to_cart(b, p); shop.add_to_cart(b, p, variant=v)   # B: [P, V]
    # hold A for 1.5 s right after its first "UPDATE product_variants SET stock_quantity"
    # via event.listen(async_engine.sync_engine, "after_cursor_execute", hold); start B 0.3 s after A
    results = _run([lambda c: Shop(c, db_session).place_order(a),
                    lambda c: Shop(c, db_session).place_order(b)])
    assert sorted(r.status_code for r in results.values()) == [201, 201]   # today: [201, 503]
```

### 2B-02 — PostgreSQL lock-order regression tests force no interleaving (Medium, Confirmed)

`tests/integration/test_postgres_only.py:148-181`, in `_run_holding_after_first_stock_update`:
```python
event.listen(engine, "after_cursor_execute", hold)
```
`engine` here is the **sync** engine (`tests/conftest.py:220`). The routes that take stock locks (`create_order`,
`cancel_own_order`, `update_order_status`) run on `AsyncSession` over `async_engine` (`conftest.py:223`,
`AsyncSessionLocal` at 270). I attached the same spy to `engine` around a `place_order` on PG and it saw **0**
`UPDATE products SET stock_quantity` statements (`test_existing_harness_hook_fires`). The barrier therefore never
engages, the two requests run with whatever timing the threads happen to have, and
`test_orders_with_the_same_products_in_opposite_order_do_not_deadlock` /
`test_a_cancellation_and_an_order_..._do_not_deadlock` would very likely pass with `lock_stock_rows` deleted.

**Fix:** `event.listen(async_engine.sync_engine, ...)`, the target that actually worked in the 2B-01 reproduction.
Also add a variant scenario like the one in 2B-01. Optionally assert that the hook fired.

### 2B-03 — Orders expire or are cancelled while a payment is in flight; Checkout Sessions outlive the order (High, Confirmed by reading)

- `tasks.py:480-499`: candidates are `status == NEW and payment_status in (pending, failed) and created_at < cutoff`.
  Nothing checks for attempts in `processing`/`requires_action`/`unknown`. The `_guard_no_attempt_in_flight`
  (`orders.py:898`) used by `/pay` is not consulted.
- `orders.py:557`: `cancel_own_order` uses only `is_unpaid_and_new`, the same gap.
- `live_stripe_provider.py:347-372`: `create_checkout_session` sends no `expires_at`, so Stripe's default of **24 h**
  applies. `UNPAID_ORDER_EXPIRE_MINUTES` is 30 (`config.py:414`).
- `expire_checkout_session` is defined (`base.py:138`, `live_stripe_provider.py:399`) but **never called** from app
  code (grep). No PaymentIntent is cancelled either.

**Scenario.** At t0 the order is placed. At t0+25 the customer opens the hosted checkout. The sweep at t0+30..35
cancels the order and restocks; the stock may then sell to someone else. At t0+40 the customer pays. The
`checkout.session.completed` webhook -> `_apply_payment_success` (`orders.py:765-767`) records the charge with
`requires_manual_review` and leaves the order cancelled. **The customer has been charged for a cancelled order whose
goods may no longer exist.** Getting the money back is a manual job. 3-D Secure on `/pay` straddling the cut-off
does the same. `test_order_expiry.py:327` pins the "flag, don't apply" outcome, so this is known as a fallback, but
nothing prevents it.

**Fix (minimal):**
1. Have the sweep and `cancel_own_order` skip or refuse an order with a non-stale in-flight attempt; reuse the
   `_guard_no_attempt_in_flight` query.
2. Pass `expires_at = now + max(30 min, remaining order lifetime)` to the Checkout Session; Stripe's minimum is
   30 min.
3. On every cancellation, call `expire_checkout_session` (after the commit) for open sessions and cancel
   PaymentIntents in `requires_action`.

**Pytest sketch:** create an order, open `/checkout-session`, age the order past the window (`monkeypatch`
`utc_now`), run `Shop.expire_unpaid(order_id)`, then assert the order is **not** cancelled while the session row is
`processing`, and assert that `expire_checkout_session` was called on the mock when the order is cancelled.

### 2B-04 — JSON Merge Patch is not RFC 7396 for nested objects (Medium, Confirmed)

`admin.py:286-287` (`setattr(db_product, field, value)` for each top-level key) and `admin.py:831-832` (the same for
variants). The schema types are `characteristics: dict[str, ProductCharacteristicInput] | None`
(`schemas/product.py:86`) and `attributes: dict[str, Any] | None` (`schemas/product.py:191`). The PATCH description
(`merge_patch.py:77-81`) says "JSON Merge Patch (RFC 7396)". RFC 7396 §2 merges object members recursively and
removes a member whose patch value is `null`.

Reproduced (`repro/tests/integration/test_2b_repro.py`, SQLite harness, 3 passed):
- A product with characteristics `{color, size}`, patched with `{"characteristics": {"color": {...}}}`: the result
  has keys `['color']`. **`size` is silently deleted.**
- `{"characteristics": {"color": null}}` answers **422** (RFC: remove `color`).
- A variant with attributes `{"size": "M", "color": "red"}`, patched with `{"attributes": {"size": null}}`: 200,
  attributes = `{"size": null}`. `color` is lost and `null` is stored as a value.

Arrays (`images`) being replaced is correct. Unknown fields -> 422 (`RequestBody extra="forbid"`) and top-level null
on a NOT NULL field -> 422 (`reject_null`) are both sound. Media types `application/merge-patch+json` and
`application/json` are both accepted on PUT and PATCH; anything else -> 415.

**Fix:** on the PATCH route only, apply an RFC 7396 merge to dict-typed fields before the setattr:
```python
def _merge(target, patch):
    if not isinstance(patch, dict): return patch
    out = dict(target or {})
    for k, v in patch.items():
        if v is None: out.pop(k, None)
        else: out[k] = _merge(out.get(k), v)
    return out
```
This needs the raw body, because pydantic drops `null` members inside `dict[str, Model]` validation. If that is not
wanted, document that PATCH is "top-level merge only". Either way, PUT and PATCH sharing one handler is the root
cause.

### 2B-05 — Absolute stock writes and an ETag that changes on every sale (Medium, Confirmed by reading, design)

`admin.py:253-258, 271, 286-287`: without `If-Match` there is no lock and `stock_quantity` is written as an absolute
value. The ETag (`etag.py:24-26`) hashes the whole `ProductResponse`, which includes `stock_quantity` and
`updated_at`. A checkout's `UPDATE products ...` also bumps `updated_at` through Core `onupdate`. So on a product that
is selling, any If-Match taken from a GET goes stale almost immediately (412, `admin.py:269`). The practical way out
is an unconditional PUT/PATCH with the stale stock number.

**Interleaving.** The admin GETs (stock 10). Two checkouts commit (stock 8). The admin PATCHes
`{"stock_quantity": 10, "price": ...}` without If-Match. The write is absolute, so stock is 10: the two units already
sold are back in stock and are oversold. The variant route `admin.py:801-811, 831-832` has the same problem.

**Fix:** add a relative stock-adjustment operation (`stock_delta`, applied as `stock = stock + :d` with
`WHERE stock + :d >= 0`), or exclude volatile stock from the validator used for If-Match on non-stock edits. At
minimum, document that a stock write must be conditional.

### 2B-06 — State machine: `SENT -> CANCELLED` restocks shipped goods; cancelling a paid order neither refunds nor flags it (Medium, Confirmed by reading)

`services/orders.py:21-26` lists every allowed transition:

| From | Allowed to |
|---|---|
| `new` | `confirmed` (paid only), `cancelled` |
| `confirmed` | `processing` (paid only), `cancelled` |
| `processing` | `sent` (paid only), `cancelled` |
| `sent` | `delivered` (paid only), `cancelled` |
| `delivered` | nothing |
| `cancelled` | nothing |

`orders.py:493-496` restocks on **every** cancel, including from `sent`. `tests/integration/test_orders.py:678-682`
asserts that stock goes back to 10 after `sent -> cancelled`, so this is intended. It is still wrong for physical
goods: a parcel in transit is counted as available and sold again. A paid order (`confirmed`/`processing`/`sent`)
cancelled by an admin keeps `payment_status = paid`. No refund is started, nothing is flagged, and the
admin stats then exclude it (`admin.py:512`). The money is kept silently unless someone also calls `/refund`.

Illegal transitions checked: `cancelled -> *` and `delivered -> *` are rejected. A double cancel is serialised by the
order FU plus the status re-check (`services/orders.py:109`), and `test_order_expiry.py:135,184,310` cover it.
`cancelled -> paid` is prevented: a late success is flagged (`orders.py:765`), and `/pay` refuses (`orders.py:869`).
Admin overrides: there is no "force" path; admins go through the same table plus the PAID_ONLY rule.

**Fix:** do not restock when cancelling from `sent` (or make it an explicit `restock` flag). When cancelling an order
with `payment_status == paid`, either require a refund first (409) or mark the order for manual review.

### 2B-07 — Order total overflows `Numeric(10,2)` -> 500 (Low)

`models/base.py:36` gives `Decimal -> Numeric(10, 2)`. A price may be up to 99,999,999.99
(`NonNegativeMoney max_digits=10`) and a quantity up to the stock (int32). In `orders.py:366-368`,
`total_amount += price_at_time * item.quantity` is not bounded. For example, 100,001 units at 1,000.00 gives
100,001,000.00, so the commit raises `NumericValueOutOfRange` (DataError, not OperationalError) -> **500**, with the
stock decrement rolled back. Stripe's per-charge maximum is lower still.
**Fix:** before the commit, check `total_amount <= Decimal("99999999.99")` (and the provider maximum) and answer
409/422.

### 2B-08 — Hard product delete races checkout -> 500 (Low)

`admin.py:339-349` counts `order_items` and then deletes. A concurrent checkout that has locked the product (plain
line) or its variant has not yet inserted `order_items`, because those are inserted at commit. So the count is 0, the
admin's `DELETE` waits on the lock, and when it resumes after the checkout commits, the FK
`order_items.product_id/variant_id` makes it fail with IntegrityError -> generic **500**. No corruption: the FK
holds. **Fix:** lock the product (`FOR UPDATE`) before the count, or catch the IntegrityError and fall back to the
soft delete.

### 2B-09 — Guest-cart merge drops lines silently (Low)

`schemas/common.py:496-499` (`left_out += 1`); `redis_codec.py:288-293` only logs a warning;
`redis_async.py:339-341` then `pipe.delete(guest_key)`. Lines that do not fit `CART_MAX_LINES` (100) are lost, and
neither the login response nor a cookie tells the user. **Fix:** keep the unmerged remainder in the guest key, or
report `left_out` (for example with a header or a flag on the next `GET /cart`).

### 2B-10 — Refund dispute check is outside the lock (Low)

`orders.py:1571-1587`: `_has_open_dispute` and `payment_status == PAID` are read, the transaction is committed,
then the provider is called. Only afterwards is the order locked (`orders.py:1617`). A `charge.dispute.created` that
is processed in that window is not seen. Stripe then refunds a disputed charge, which is the "money returned twice"
case that the comment at 1568-1570 says it prevents. The window is about one provider call. **Fix:** re-check
`_has_open_dispute` under `_lock_order_and_reload` and flag `requires_manual_review` if it changed. Avoiding the
refund entirely is impossible once Stripe has executed it.

### 2B-11 — Expiry sweep aborts on the first error (Low)

`tasks.py:492-526` has no per-order `try`. A deadlock (2B-01) or a lost connection on order k raises out of
`expire_unpaid_orders`, and the task has no `autoretry_for` (`tasks.py:528-534`). Orders after k wait for the next
beat, 5 minutes later. The orders before k have already been committed. **Fix:** wrap each order in
`try/except OperationalError: db.rollback(); continue`.

### 2B-12 — Variant line reads the parent product unlocked (Low)

`orders.py:290-301` reads `Product.is_active` and `.price` for a variant line, but only the variant is locked. An
admin soft delete (`admin.py:343`) that commits between this read and the checkout commit leaves an order for a
product that was withdrawn mid-checkout. This is benign once the 2B-01 fix is in. Adding `FOR KEY SHARE` on the
product read would close it.

### 2B-13 — Informational

- **Product vs variant stock** are independent counters (by design, `models/product.py:78-92`). The consequences:
  - `GET /products?in_stock=true` (`products.py:263-264`) looks only at `Product.stock_quantity`, so a product sold
    through variants with product-level stock 0 is hidden.
  - A plain line and variant lines of the same product can coexist in one cart (`cart.py:455-478`).
- **Cache-aside staleness** (`products.py:379-402`): a reader that misses the cache, reads the DB, and writes the
  cache after a concurrent commit and invalidation re-caches the old row. This lasts up to `PRODUCT_CACHE_TTL`
  (5 min, `redis_codec.py:40`). An admin then gets a 412 for a stale ETag and refetches the same stale copy; using
  the ETag returned in the 412 response works. A soft-deleted product can also be served from the cache within this
  window (normally invalidated at `admin.py:352`).
- **Old orders show live product data:** `OrderItemResponse.product_name/product_image` (`schemas/order.py:166-192`)
  come from the live product, so a renamed product changes historical orders. The price is snapshotted
  (`price_at_time`).
- **PUT is not a full replacement:** PUT has merge semantics (`exclude_unset`), documented in `merge_patch.py`.
- **Migration 76d14f9f12e5** adds validated CHECKs in one transaction (ACCESS EXCLUSIVE plus a full scan for each
  table). On large tables, consider `NOT VALID` followed by `VALIDATE CONSTRAINT`.
- **Price changes at checkout:** `create_order` charges the live price with no "expected total" in `OrderCreate`.
  Cart responses flag `price_changed`, and the client sees the order total before `/pay` (which requires the exact
  amount). Acceptable, but a price increase is applied without explicit consent.

---

## 5. Answers to the brief's questions

**Q1. AB-BA deadlocks.** See the matrix in §2. Within-table order is deterministic everywhere (`ORDER BY id` for
products and variants; every other path locks a single row). Cross-table order is consistent: order -> stock and
[event] -> order -> payment. No path locks stock or payment before an existing order. The one real cycle comes from
the **implicit FK KEY SHARE** of the `order_items` INSERT against the explicit `FOR UPDATE` (2B-01, confirmed on
PG 16). `with_for_update(of=Order)` correctly limits the lock to `orders`; the selectin item query is unlocked
(verified). Stripe-event, refund and reconciliation paths contain no restock; reconciliation writes only discrepancy
rows.

**Q2. Stock.**
- Oversell is prevented by the conditional `UPDATE ... WHERE stock >= n` (`orders.py:331-355`) plus the CHECKs
  (`models/product.py:70-71,115`).
- Double restock is prevented: every cancel path holds the order FU and re-checks the status
  (`services/orders.py:109`); expiry uses SKIP LOCKED and re-checks (`tasks.py:494-498`); refund never restocks.
- Restocking a deleted variant is impossible: there is no variant delete endpoint, and the `order_items.variant_id`
  FK blocks deleting a referenced variant.
- Product and variant stock are independent counters (2B-13).
- Residual risks: admin absolute writes (2B-05), restock from `sent` (2B-06), and deadlocks (2B-01).

**Q3. Guest-cart merge.**
- It is Redis-only. There are no DB row locks: login commits `last_login` first and then merges
  (`users.py:378-403`).
- The merge is atomic: both keys are WATCHed and one MULTI/EXEC does SET account plus DEL guest
  (`redis_async.py:319-348`; sync twin `redis.py:328-374`), retried on WatchError.
- Concurrent logins with the same cookie merge once, because the second sees the guest key gone. A guest add during
  the merge either lands before it (and is merged) or finds no cart and starts a new guest cart under a new cookie;
  nothing is lost.
- Login racing checkout is safe: checkout subtracts only the quantities it ordered (`redis_codec.py:299-317`), so
  quantities added by the merge survive.
- The merge is idempotent, and every write re-arms the TTL (`plan_merge_into_account` sets the account TTL; the
  guest cookie is cleared).
- Over-limit lines are dropped silently (2B-09).
- fakeredis vs real Redis: only WATCH/MULTI/EXEC/SET EX/DEL are used, with no Lua. These behave the same in
  fakeredis; `test_real_redis_parity.py` exists for the real server. On Redis Cluster both keys would need a hash
  tag (documented at `redis.py:351-353`).

**Q4. ETag.**
- The ETag is a strong SHA-256 of the full response model. It covers price, stock, category_name, currency and
  updated_at. Variants are not part of the product representation, so they correctly do not affect it.
- The DB and cache paths give identical tags (`test_etag.py`).
- If-Match is atomic with the write: it is checked under `FOR UPDATE` (`admin.py:257`, `admin.py:810`), with a
  strong comparison, and a weak tag never matches (`etag.py:34-40`).
- 304 uses weak comparison and carries the ETag. HEAD runs as GET with the body dropped (`head.py`).
- Weaknesses: the volatility of stock in the ETag (2B-05), and FU instead of NKU on the admin locks (2B-01 fix).

**Q5. JSON Merge Patch.**
- Top level is correct: a null on a NOT NULL field -> 422 (no IntegrityError); `price_override: null` clears the
  override; unknown fields -> 422; arrays are replaced; the content type is enforced (415).
- Nested objects violate RFC 7396 (2B-04).

**Q6. State machine.** The table is in 2B-06. No illegal transition is reachable. A double cancel restocks once.
`cancelled -> paid` is blocked or flagged. The problems are restocking from `sent`, paid cancellations with no refund
and no flag (2B-06), and expiry or customer cancel during an in-flight payment (2B-03).

**Q7. Money.**
- Amounts are Decimal end to end. Input is a decimal string with at most 2 fraction digits
  (`schemas/order.py:57-121`), and columns are `Numeric(10,2)`.
- The cart and the order use the same `cart_unit_price` rule (`schemas/common.py:243-250`, `orders.py:318-322`), so
  their totals agree. `price*int` is exact.
- Cents conversion is exact because `STORE_CURRENCY` is restricted to 2-decimal currencies (`config.py:479-498`).
- There is no currency mixing within an order: `Order.currency` is fixed at placement and `/pay` checks it
  (`orders.py:940-951`).
- A price change between cart and checkout is charged at the live price and reported as `price_changed` (2B-13).
- Overflow: 2B-07.

## 6. Checked and found sound

- **Oversell.** The atomic conditional decrement (`orders.py:331-355`) and the DB CHECKs together cover it; a
  rollback happens on `rowcount == 0`.
- **Checkout idempotency race.** The unique index plus the replay after IntegrityError cover it
  (`orders.py:262-278`, `models/order.py:233-238`).
- **Double restock.** Covered by the order FU, the status re-check and SKIP LOCKED with a re-check
  (`services/orders.py:109`, `orders.py:463,549`, `tasks.py:494-498`).
- **`with_for_update(of=Order)` with outer-joined eager loads** locks only `orders`; the selectinload query is
  unlocked (verified on PG with echo).
- **Payment lock order.** [event] -> order -> payment is consistent across `/pay`, the webhook, refunds and disputes
  (§2 L10-L13). The two bulk UPDATE sweeps (L15, L16) cannot form a cycle, since no other path holds two rows of
  those tables.
- **Late payment for a cancelled order** is flagged, not applied (`orders.py:765-767`). The "already paid" guard is
  rechecked under the lock (`orders.py:761-772`).
- **Cart mutations** are all WATCH/MULTI with the stock check inside the transaction body (`redis_codec.py:210-252`).
  A guest identifier is never accepted from the client to create a cart (`cart.py:483-499`, `must_exist`).
- **Removing ordered items** subtracts only what was ordered, so concurrent adds are kept (`redis_codec.py:299-317`).
- **Variant ownership.** The cart and checkout both require `variant.product_id == product_id`
  (`cart.py:260-266`, `orders.py:305-310`).
- **Status filters and sorting.** The `sort_by` values are whitelisted (`admin.py:360-373`, `core/sorting.py`,
  `products.py:146`).
- **Merge-patch top-level nulls.** `reject_null` validators produce a 422, not an IntegrityError
  (`schemas/product.py:95-102,201-208`).
- **Stored cart owner.** A stored cart whose owner does not match its key is ignored (`redis_codec.py:143-153`).
- **ETag parsing.** It follows RFC 9110 (`etag.py`). `*` passes If-Match only for an existing resource, which holds
  because the 404 is raised before the check.
- **CHECK migration.** It pre-validates existing rows and lists the violators (76d14f9f12e5).
- **`variant_id` migration.** It names its FK for the downgrade.

## 7. Open questions

1. Is selling a product both as a plain line and as variants intended? If not, refusing a plain line for a product
   with active variants also removes most of 2B-01's trigger. The NKU fix is still recommended.
2. Should `sent -> cancelled` exist at all (2B-06), and should cancelling a paid order require a refund first?
3. 2B-03: what is the intended customer experience for a payment that completes after expiry? Automatic refund,
   or reviving the order when stock allows?
4. Is the 503 "database unavailable" mapping for `DeadlockDetected` and serialization failures intended? A
   dedicated 409/503 "retry" problem type, or one transparent retry of the transaction, would give a clearer signal
   for operations.
