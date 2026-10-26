# Phase 2a - Stateful property-based fuzzing (Hypothesis)

Module: `tests/torture_suite/test_stateful_invariants.py` (ruff clean). Scratch output
(full pytest logs, per-run request/transition counters): `.cloud_torture/scratch_2a/`.

## What is modelled

### Model 1 - `CartWorld` (RuleBasedStateMachine)
World per example: 3 products (random Decimal prices 0.00-999.99, random stock 0-9),
product #3 has 2 variants (random `price_override` or none) -> 5 cart lines
(2 plain, 1 plain line of a variant product, 2 variant lines); one verified buyer;
one admin; one guest (https TestClient cookie jar). `CART_MAX_LINES` patched to 4 so
adds and merges meet the cap. Pure-Python model of catalog + both carts (ordered dicts).

Rules: `add` (qty 1-2), `add_edge` (qty = fill-to-stock, stock+1, 0, -1, INT32_MAX,
INT32_MAX+1), `update` (incl. 0/-1/over/INT32_MAX+1, item absent), `remove`, `clear`
(guest and user each), `login_merges_guest_cart`, `validate` (guest/user),
`checkout`, `guest_cannot_checkout` (401), `cancel_order` (restock), admin
`admin_sets_stock` (0-8 or INT32_MAX), `admin_sets_price` (product price / variant
override / clear override), `admin_toggles_variant`, `admin_deletes_product`
(soft-delete if any order line references it, hard delete + cascade otherwise).
Every response is predicted exactly: status code, the problem `detail`
(stock vs cart-full 409, product vs variant 404), the failing product in a 409
checkout, the order's lines and Decimal total, the issues list of /validate.

Invariant after every step (GET /cart for both carts + DB): lines and quantities equal
the model *in order*; unavailable lines (`product_unavailable`/`variant_unavailable`)
have no price and are not in the total; `unit_price`, `line_total`, `total_amount` exact
Decimal (2 places) = sum(price*qty) at *current* catalog prices; <= CART_MAX_LINES lines;
qty >= 1; DB stock == model stock >= 0; hard-deleted products/variants are gone.

Merge rule (documented from `Cart.merge`, app/schemas/common.py:471, via
`redis_codec.plan_merge_into_account`): guest lines in guest order; overlap -> quantities
**summed** (no clamp to stock, no clamp to any max: /validate reports, checkout refuses);
new line -> **appended** while the account cart has < CART_MAX_LINES lines, else
**dropped** (logged); guest key deleted, cookie cleared. Verified by the machine and by
`test_merge_at_login_...` (300 examples).

### Model 2 - `PaymentWorld` (RuleBasedStateMachine, `Bundle("orders")`)
World per example: product A (stock 12), product B (stock 10) with variant Bv (stock 6),
random positive prices/override; 2 buyers; admin; mock provider reset.
Rules: `place_order` (1-3 lines, multi-unit, target=orders), `replay_order_key`,
`pay` (pm_card_visa, ...chargeDeclined, authenticationRequired, pendingRefund,
refundFail, threeDSecureRequiredChargeDeclined; 1/10 by the wrong buyer -> 404),
`pay_with_a_good_card`, `replay_pay` (same key/body), `open_checkout_session`,
`customer_cancels`, `customer_finishes_3ds` (authenticate / abandon -> intent canceled),
`checkout_session_outcome` (completed, expired, completed-unpaid + async_payment_failed),
`deliver_one_event` (any outbox index = out of order), `redeliver_an_event` (same event id:
must be `already_processed` and change nothing), `drain_outbox_in_any_order` (random
permutation, then convergence check DB == provider), `dashboard_refund`,
`stripe_settles_a_refund` (mock `settle_refund`: pending->succeeded / succeeded->failed),
`dispute` (created/updated/closed won|lost|warning_closed), `admin_refunds` (partial/full),
`admin_replays_a_refund`, `admin_sets_status`, `unpaid_order_expires` (created_at moved
back past UNPAID_ORDER_EXPIRE_MINUTES, `tasks.cancel_expired_unpaid_orders()` run
directly), `admin_sets_stock`.
Every Stripe-side change appends the event Stripe would send (snapshot at that moment)
to an **outbox**; events are delivered later in arbitrary order, some twice.
Expected status codes are predicted from the DB/provider state for pay, checkout-session,
cancel, admin status, refunds (409 not paid / disputed, 400 over provider-refundable).

Invariants checked after EVERY step:
* **Stock conservation** (mapping below), stock >= 0.
* **Money/ledger**: order total == sum(price_at_time*qty) exactly; per attempt
  `0 <= amount_refunded <= amount` and `amount_refunded == sum(refund rows not failed/canceled)`;
  refund amounts > 0, currency == charge currency; an attempt that did not succeed has no
  refunds; per order sum(returned) <= sum(captured); at most one unflagged succeeded attempt;
  paid/refunded => exactly one, with amount == cents(total); refunded <=> succeeded refunds
  cover the charge or a dispute was lost; confirmed/processing/sent/delivered => paid/refunded.
* **Transition graph** (every observed change since the previous observation, checked after
  each request/event, see below).
* **HTTP**: no 5xx ever; every 4xx is `application/problem+json`; every webhook is answered
  200 and its worker task ends `processed` (a `retrying`/`failed` task = the worker's 500).
* After a drain: refund ids/statuses, `amount_refunded`, dispute statuses equal the
  provider's; order payment_status follows the provider's succeeded refunds / lost dispute.

### Stock formula mapping
Code: `create_order` decrements `stock_quantity` (the AVAILABLE count) at order
placement whether or not it is ever paid (app/api/v1/orders.py:331-348);
`cancel_order_and_restock` (app/services/orders.py:96-131) is the only restock
(customer cancel, admin cancel, expiry). Payment, refunds, disputes never move stock.
Requested `initial == current + reserved + sold - refunded` is therefore checked as,
per stock unit (product or variant):

    initial + admin_adjustments == current
                                 + qty(non-cancelled unpaid orders)        # reserved
                                 + qty(non-cancelled paid/refunded orders) # sold
    refunded_stock == 0 by design (a refund returns money, not goods)

### Transition graph (derived from app/services/orders.py:20-27 and the payment code)
* Order.status: new->{confirmed,cancelled}, confirmed->{processing,cancelled},
  processing->{sent,cancelled}, sent->{delivered,cancelled}; delivered/cancelled terminal.
* Order.payment_status: pending->{paid,failed}, failed->{paid}, paid->{refunded},
  refunded->paid ONLY in a step where a refund of the charge went failed/canceled
  (`_apply_refund_object` refund.failed path); a cancelled unpaid order must never become paid.
* Payment.status: processing->{requires_action,succeeded,failed,unknown},
  requires_action->{succeeded,failed}, failed->{succeeded}, succeeded terminal;
  `requires_manual_review` never True->False.
* PaymentRefund.status: pending->{succeeded,failed,canceled}, succeeded->{failed,canceled},
  failed/canceled terminal. PaymentDispute: closed statuses terminal.
Observed (PG final run, PaymentWorld): new->confirmed 135, new->cancelled 79,
confirmed->processing 15, processing->sent 1, pending->paid 135, paid->refunded 12,
pending->failed 3, dispute needs_response->lost 5 (SQLite run additionally failed->paid,
confirmed->cancelled, under_review->lost). **Illegal edge the code performs:
PaymentRefund succeeded->pending and order refunded->paid** (bug 1, found by the @given
convergence property; the machine's refund-transition check would flag it too).

### @given properties (max_examples=300 each)
* `test_merge_at_login_sums_overlaps_appends_and_drops_beyond_the_cap`
* `test_checkout_never_sells_more_than_the_stock` (cart filled while stock high, stock then
  lowered; all-or-nothing, never below zero)
* `test_admin_refunds_never_exceed_the_charge` (1-5 partial/full refunds)
* `test_refund_events_in_any_order_converge_to_the_providers_state` (pendingRefund /
  refundFail charges, 1-3 refunds, settlements, refund.created + charge.refunded +
  refund.updated/failed delivered in a Hypothesis-chosen permutation with duplicates)

## Hypothesis settings and volume
* CartWorld: `max_examples=scaled_examples(100)` (200 under ci/explore),
  `stateful_step_count=30`. PaymentWorld: `scaled_examples(150)`, `stateful_step_count=50`.
  @given: 300. All: `deadline=None`, suppress `function_scoped_fixture, too_slow,
  filter_too_much, data_too_large`.
* `--hypothesis-show-statistics` prints nothing for these tests (examples run inside
  `run_state_machine_as_test` / nested @given), so volume was counted by the harness
  (`stats_*.json`): per back end ~100 CartWorld runs (~3,000 steps, ~10-18k HTTP requests
  incl. invariant reads), ~150 PaymentWorld runs (~5-7k steps, ~3.4-3.5k checked
  requests + webhook deliveries), 3 x 300 @given examples passing + the convergence
  property (fails within its first examples). Roughly 2,000+ examples and ~25k HTTP
  requests per back end.
* Runtime: SQLite 499.6 s (8:19), PostgreSQL 675.4 s (11:15) - the CartWorld time is
  inflated by shrinking its (genuine) failure; green CartWorld runs took ~110 s (SQLite).

## Results
SQLite (`uv run pytest tests/torture_suite/test_stateful_invariants.py -p no:cacheprovider -v`):

    test_cart_world_matches_its_model                                     FAILED (bug 3, SQLite form)
    test_payment_world_keeps_its_invariants                               PASSED
    test_merge_at_login_sums_overlaps_appends_and_drops_beyond_the_cap    PASSED
    test_checkout_never_sells_more_than_the_stock                         PASSED
    test_admin_refunds_never_exceed_the_charge                            PASSED
    test_refund_events_in_any_order_converge_to_the_providers_state       FAILED (bugs 1+2)
    test_bug_stale_refund_event_reverts_a_settled_refund                  FAILED (bug 1)
    test_bug_refund_after_an_unreported_refund_failure_is_a_500           FAILED (bug 2)
    test_bug_restock_past_int32_max_is_a_500_on_cancel                    FAILED (bug 3)
    test_bug_restock_past_int32_max_aborts_the_expiry_sweep               PASSED (PG-only)
    =================== 5 failed, 5 passed in 499.64s (0:08:19) ====================

PostgreSQL 16 (`TEST_DATABASE_URL=postgresql+psycopg://torture:torture@localhost:5432/postgres`):

    test_cart_world_matches_its_model                                     FAILED (bug 3)
    test_payment_world_keeps_its_invariants                               PASSED
    test_merge_at_login_sums_overlaps_appends_and_drops_beyond_the_cap    PASSED
    test_checkout_never_sells_more_than_the_stock                         PASSED
    test_admin_refunds_never_exceed_the_charge                            PASSED
    test_refund_events_in_any_order_converge_to_the_providers_state       FAILED (bugs 1+2)
    test_bug_stale_refund_event_reverts_a_settled_refund                  FAILED (bug 1)
    test_bug_refund_after_an_unreported_refund_failure_is_a_500           FAILED (bug 2)
    test_bug_restock_past_int32_max_is_a_500_on_cancel                    FAILED (bug 3)
    test_bug_restock_past_int32_max_aborts_the_expiry_sweep               FAILED (bug 3)
    =================== 6 failed, 4 passed in 675.35s (0:11:15) ====================

`HYPOTHESIS_PROFILE=explore` (SQLite, doubled budgets: CartWorld 200 runs, PaymentWorld
300 runs, @given 300): `5 failed, 5 passed in 486.20s (0:08:06)` - CartWorld PASSED this
time (did not draw the INT32_MAX path), PaymentWorld FAILED, the rest as above.
PaymentWorld under explore with fixed seeds (`--hypothesis-seed`): 11 and 44 pass;
**seed 22** shrinks to bug 2 found by the machine itself
(`pay(pm_card_refundFail)`, deliver, `dashboard_refund(1)`, deliver,
`stripe_settles_a_refund`, `admin_refunds(amount=None)` -> 500);
**seed 33** finds bug 1 by the transition-graph check
(`'refund re_... of order 231 went succeeded->pending'` on a late `refund.created`),
but Hypothesis' shrinker then reports `FlakyStrategyDefinition` (replay of the long
failing sequence is not stable); the raw counterexample was obtained with shrinking
disabled. Both are the same defects as the deterministic `test_bug_*` tests.
After the last harness change (provider-refund precondition made instance-local),
PaymentWorld was re-run with the default profile: SQLite `1 passed in 71.99s`,
PostgreSQL `1 passed in 186.77s`.

Every failure above is one of the three backend defects below; everything else passes
(the harness itself was iterated until green on non-bug paths: coverage floors, rule
weighting, expectations).

## Suspected backend bugs

### Bug 1 - out-of-order refund event rewinds a settled refund (refunded -> paid)
* Tests: `test_bug_stale_refund_event_reverts_a_settled_refund`;
  `test_refund_events_in_any_order_converge_to_the_providers_state`.
* Code: `app/services/stripe_events.py:253-255` (`_apply_refund_object`: only
  failed/canceled are treated as final; any other reported status overwrites the row),
  then `_settle_refunds` (app/api/v1/orders.py:1771) recomputes the order.
* Minimal counterexample: pay with `pm_card_pendingRefund`; admin full refund -> refund
  `pending`, Stripe emits `refund.created{status: pending}`; refund settles -> Stripe emits
  `refund.updated{status: succeeded}`. Deliver `refund.updated` first (order -> refunded),
  then `refund.created`: refund row back to `pending`, order back to `paid`. Hypothesis:
  `token='pm_card_pendingRefund', parts=[1], settle=[True,...]`, shuffled order.
  (`charge.refunded` snapshots carrying the old `pending` status do the same.)
* Why backend: Stripe explicitly does not guarantee event ordering; the mock and the
  sequence are exactly Stripe's documented flow. After all events are delivered our ledger
  disagrees with the provider forever, and a fully refunded order is `paid` again, so an
  admin can move it to processing/sent (ship goods whose money was returned).
* Fix (monotonic refund status):

```diff
--- a/app/services/stripe_events.py
+++ b/app/services/stripe_events.py
@@ def _apply_refund_object(db, payment_row, refund_object):
     if refund_row.status in _REFUND_STATUSES_NOT_RETURNED:
         return False
+    # Events arrive out of order: never move a refund back to an earlier state
+    # (a late refund.created/charge.refunded snapshot saying "pending" after
+    # refund.updated said "succeeded").
+    _RANK = {"pending": 0, "requires_action": 0, "succeeded": 1, "failed": 2, "canceled": 2}
+    if _RANK.get(status, 0) < _RANK.get(refund_row.status, 0):
+        return False
     refund_row.status = status
```

### Bug 2 - /refund answers 500 after the provider already refunded (stale failed refund)
* Tests: `test_bug_refund_after_an_unreported_refund_failure_is_a_500`;
  `test_refund_events_in_any_order_converge_to_the_providers_state`
  (`token='pm_card_refundFail', parts=[1, 1000], settle=[True, False, False]`).
* Code: `app/api/v1/orders.py:1625` -> `_record_refund` `:1767`
  (`payment_row.amount_refunded += amount`) -> CHECK `ck_payment_refunded_within_amount`
  (app/models/payment.py) -> IntegrityError -> 500 at the flush/commit (`:1636`).
* Minimal counterexample: charge 10.00 with `pm_card_refundFail`; refund 0.01 (succeeded);
  Stripe fails that refund (money refundable again; `refund.failed` webhook not yet
  processed); admin refunds 10.00 -> provider accepts and returns the money, route adds
  1000 to amount_refunded=1 -> 1001 > 1000 -> **500**, refund not recorded.
* Why backend: the provider call has succeeded (money moved) before the local write, so a
  500 is the worst answer (admin will retry; the record exists only if a later webhook
  happens to fill it in). The route trusts a cached `amount_refunded` that the provider no
  longer agrees with; webhook delay is normal.
* Fix: before recording, refresh this charge's refunds from the provider (the same code
  path as the webhook), so failed refunds stop counting:

```diff
--- a/app/api/v1/orders.py
+++ b/app/api/v1/orders.py
@@ async def refund_payment(...):
     await db.run_sync(_lock_order_and_reload, order, payment_row)
+    # A refund that failed at the provider but whose refund.failed webhook is
+    # not processed yet still counts in amount_refunded; bring the recorded
+    # refunds up to date first (as the webhook would).
+    from app.services.stripe_events import _apply_refund_object
+    known = await run_in_threadpool(provider.list_refunds, payment_intent_id)
+    for other in known:
+        if other.id != refund.id:
+            await db.run_sync(_apply_refund_object, payment_row,
+                              {"id": other.id, "status": other.status.value,
+                               "amount": other.amount})
     refund_row = await db.run_sync(
```
  (alternatively: catch the IntegrityError, record the refund with
  `requires_manual_review=True` and answer 200/202 - never 500 after money moved.)

### Bug 3 - restock past INT32_MAX: 500 on cancel/expiry (PostgreSQL), unreadable product (SQLite)
* Tests: `test_bug_restock_past_int32_max_is_a_500_on_cancel`,
  `test_bug_restock_past_int32_max_aborts_the_expiry_sweep` (PG); CartWorld on both.
* Code: `app/schemas/product.py:83/193` accept `stock_quantity` up to 2147483647;
  `app/services/orders.py:123,130` restock with an unchecked `stock + qty`; also
  `app/tasks.py:499` (expiry).
* Minimal counterexample (Hypothesis, PG): `setup(stocks=[2,...])`, add 1, login,
  checkout, `admin_sets_stock(index=0, value=2_147_483_647)`, `cancel_order` ->
  `psycopg.errors.NumericValueOutOfRange: integer out of range` -> 500. The customer can
  never cancel; the expiry task raises and aborts the whole sweep, so other expired orders
  in the same run keep their stock. On SQLite the row becomes 2147483648 and the next admin
  PUT on that product raises `ResponseValidationError` (ProductInDB le=INT32_MAX) -> 500.
* Why backend: every input is accepted by the API's own validation; the arithmetic
  overflow is in the restock path.
* Fix: clamp (or refuse admin values that leave no headroom):

```diff
--- a/app/services/orders.py
+++ b/app/services/orders.py
-                .values({Product.stock_quantity: Product.stock_quantity + item.quantity})
+                .values({Product.stock_quantity: func.least(
+                    Product.stock_quantity + cast(item.quantity, BigInteger), INT32_MAX)})
```
  (same for ProductVariant at :123; `func.min` on SQLite / a dialect-neutral CASE;
  or validate `stock_quantity <= INT32_MAX - reserved` in the admin routes.)

## Observations (not asserted as bugs)
* Replay of a refund whose first request refunded everything answers 409 ("Only a paid
  order can be refunded") instead of the first 200 - known/accepted
  (tests/integration/test_refund_lifecycle.py:366 deliberately uses a pending refund);
  `admin_replays_a_refund` asserts the documented behaviour.
* A merged cart line may exceed stock (and in principle INT32_MAX: two INT32_MAX adds)
  - documented; /validate reports, checkout 409s; no 500 seen.
* Products may be priced 0.00, but the provider refuses a 0-cent charge: a free order can
  never be paid and simply expires. PaymentWorld uses positive prices for that reason.

## Invariants that held (both back ends, all runs)
Cart: exact model equality incl. line order, merge semantics, CART_MAX_LINES, Decimal
totals, live pricing, unavailable lines unpriced, validate issues, checkout all-or-nothing,
no oversell, cookie lifecycle, guest 401 at checkout, 4xx problem+json.
Orders/payments (outside bugs 1-3): stock conservation formula, stock >= 0, idempotent
order and /pay replays (no new rows, no stock change), duplicate webhook deliveries are
no-ops, exactly-one-charge for paid orders, amount_refunded == ledger <= amount,
returned <= captured, order/payment/attempt/dispute transitions inside the graph,
cancelled orders never marked paid (late successes flagged for manual review), paid-only
statuses only for paid/refunded orders, open disputes hold refunds/fulfilment, no webhook
task left retrying/failed.
