# Phase 2d — Boundary analysis & manual mutation testing

Date: 2026-10-26 · Project 4.0.3 · Python 3.13 / uv · SQLite (default) and PostgreSQL 16.15

Deliverable: `tests/torture_suite/test_boundary_mutants.py` (79 test functions, **298 test cases**).
Scratch: `.cloud_torture/scratch_2d/` (`mutate.py` mutation runner, `kill_matrix.json`, `run_sqlite.txt`, `run_pg.txt`).
Mutation runs were made only on a copy (`/tmp/claude-mut-2d`); the real `app/` was never edited.

## Method

* Time windows: each boundary crossed at **T − 1 µs, T, T + 1 µs** with the clock frozen (freezegun, ignore
  list emptied so TestClient's worker thread is frozen too) and row timestamps written **with
  microseconds**, so the SQL comparison itself (SQLite text compare / PG `timestamptz`) is exercised.
  Redis TTL windows (fakeredis, ms resolution) at **±1 ms**. JWT `exp` at ±1 µs against PyJWT.
* Counts/quantities: **limit − 1, limit, limit + 1** (stock, cart lines, rate counts, login failures,
  guest carts, page sizes, key/password lengths, refund cents, reconciliation caps, sweep batch).
* Expected value AT the boundary taken from the documented intent (config.py comments, docstrings,
  docs/API.md, docs/OPERATIONS.md, RFC 7519 §4.1.4 "on or after" exp ⇒ expired, RFC 9110 Retry-After).
  Where the documentation is genuinely ambiguous the code's behaviour is asserted and listed below.
* Driven through the real HTTP API wherever a route exists; the service function (task bodies,
  `reconcile_payments`, `enqueue_stuck_events`, `_attempt_still_storing_its_id`) otherwise.

## Boundary inventory

| # | file:line | operator | documented intent (source) | inclusive at T? | test(s) | verdict |
|---|---|---|---|---|---|---|
| 1 | app/core/rate_limit.py:251 | `count <= rate.count` | "300/minute" = 300 admitted (config RATE_LIMIT_*) | N admitted, N+1 refused | test_default_limit_admits_exactly_n_requests, test_wait_* | correct |
| 2 | app/core/rate_limit.py:253 | `max(1, ceil(ttl/1000))` | Retry-After = seconds until window ends (module doc, API.md) | ceil, never 0 | test_retry_after_is_the_ceiled_rest_of_the_window, test_wait_* | correct |
| 3 | app/core/rate_limit.py:219-221 (Lua) + :253 | PEXPIRE W ms; key alive at `now == expireat` | window ends after W | client waiting exactly Retry-After admitted | test_bug_rate_limit_client_waiting_exactly_retry_after_is_refused_again | **BUG-2** |
| 4 | app/core/rate_limit.py:269 | `max(0, q - count)` | RateLimit r = remaining (draft-11) | r=0 at limit | test_ratelimit_remaining_* | correct |
| 5 | app/core/rate_limit.py:106-114 (+ config.py:67) | RATE_LIMIT_PATTERN `[1-9]\d*` | count ≥ 1, multiple ≥ 1 | — | test_parse_rate_* | correct |
| 6 | app/core/rate_limit.py:424-427 | login pair count (10/15 min) | "After this many in the period … cannot log in" | 10 failures OK, 11th attempt 429 | test_login_lock_*, test_login_n_minus_one_* | correct |
| 7 | app/core/rate_limit.py:428-431 | per-email backstop (100/hour) | failures from anywhere | n−1 OK, n ⇒ 429 | test_login_backstop_counts_failures_from_every_address | correct |
| 8 | app/api/v1/cart.py:321-339 | RATE_LIMIT_GUEST_CART | new carts per address | N new carts, N+1 ⇒ 429 | test_an_address_opens_exactly_n_guest_carts | correct |
| 9 | app/core/security.py:168 | `user_id <= 2**31-1` | INTEGER id column | 2147483647 accepted, +1 refused | test_user_id_from_subject_at_the_int32_edge | correct |
| 10 | app/core/security.py:216 + PyJWT `exp <= now` | exp = int(iat)+lifetime | RFC 7519: on/after exp ⇒ invalid | refused at T | test_access_token_is_refused_on_and_after_its_exp, test_refresh_token_* | correct (lifetime ≤ setting, see A5) |
| 11 | app/core/security.py:678 | reset exp = now + 60 min | PASSWORD_RESET_TOKEN_EXPIRE_MINUTES | refused at T | test_password_reset_link_works_for_exactly_its_lifetime | correct |
| 12 | app/core/security.py:706 | verify exp = now + 48 h | EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS | refused at T | test_email_verification_link_* | correct |
| 13 | app/core/security.py:625 | `max(int(exp - now), 1)` | blacklist "for its remaining lifetime" | blacklisted until exp | test_bug_logout_blacklist_expires_before_the_access_token | **BUG-1** |
| 14 | app/services/redis_async.py:177 | `iat < watermark` | "token issued before now is refused" | same-instant token accepted | test_sign_out_everywhere_refuses_tokens_issued_strictly_before_it | correct |
| 15 | app/services/redis_codec.py:224 | `len >= CART_MAX_LINES` | "most distinct lines a cart can hold" | exactly N lines | test_cart_holds_exactly_max_lines_*, test_guest_cart_holds_* | correct |
| 16 | app/services/redis_codec.py:227 / :248 | `stock < current+qty` / `stock < qty` | quantities bounded by stock | qty == stock OK | test_add_up_to_exactly_the_stock, test_adding_more_*, test_update_quantity_*, test_variant_stock_* | correct |
| 17 | app/schemas/common.py:496 | `len(self.items) < max_lines` | merge "while this cart has fewer than max_lines" | stops at N | test_cart_merge_*, test_login_merge_fills_* | correct |
| 18 | app/services/redis_codec.py:308 | `line.quantity <= ordered` | remove fully ordered lines | removed at equality | test_ordering_a_line_removes_it_* | correct |
| 19 | app/api/v1/cart.py:652 | `available < item.quantity` | valid when stock covers quantity | valid at equality | test_cart_validate_* | correct |
| 20 | app/api/v1/orders.py:335/344 | SQL `stock >= quantity` | atomic decrement to 0 | stock→0 allowed | test_order_takes_stock_down_to_exactly_zero | correct |
| 21 | app/schemas/cart.py:22 | `ge=1, le=INT32_MAX` | quantity ≥ 1, int32 | 0/−1 422, 2³¹−1 OK, 2³¹ 422 | test_cart_quantity_bounds | correct |
| 22 | app/services/redis_codec.py:81-89, app/api/v1/cart.py:175 | EX = TTL days; cookie Max-Age | key and cookie "expire together" | ±1 ms | test_account_cart_lives_*, test_guest_cart_and_its_cookie_* | correct |
| 23 | app/tasks.py:485 + :496 | `created_at < cutoff` (+ Python re-check `>= cutoff`) | expire orders placed MORE than 30 min ago | not at T | test_unpaid_order_expires_only_when_older_than_the_window | correct (A1) |
| 24 | app/tasks.py:728 | `created_at < cutoff` | key honoured for 24 h, "never shorter" | kept at T | test_idempotency_key_expires_*, test_order_key_is_replayed_* (API) | correct |
| 25 | app/tasks.py:340 | `deletion_date <= cutoff` | delete "once the grace period has passed" | purged at T | test_erasure_runs_once_the_grace_period_has_passed | correct |
| 26 | app/tasks.py:447 | `now - last_seen > days` | retention N days | not at T | test_retention_purge_* | code asserted (A3) |
| 27 | app/services/gdpr.py:180-181 | `(now-created).days <= N` | "within the retention period" | age ≤ N days | test_data_retention_status_inside_the_period, test_bug_data_retention_status_* | **BUG-4** |
| 28 | app/services/reconciliation.py:273-274 | `>= now-7d`, `<= now-60min` | "last 7 days, except younger than 60 min" | both ends inclusive | test_reconciliation_window_edges_to_the_microsecond | correct (A4) |
| 29 | app/services/reconciliation.py:280 | `len(ids) > limit` | at most MAX_PAYMENTS checked | truncated only beyond | test_reconciliation_cap_* | correct |
| 30 | app/services/reconciliation.py:305 | `consecutive >= MAX` | "stops after N failures in a row" | stops at N | test_reconciliation_stops_after_exactly_n_errors_in_a_row | correct (A7) |
| 31 | app/api/v1/orders.py:925 | `created_at > now-5min` blocks | "in flight for LONGER than … no longer blocks" | exactly 5 min still blocks | test_an_in_flight_attempt_*, test_bug_attempt_exactly_stale_* | **BUG-3a** |
| 32 | app/services/stripe_events.py:87 | same | "for longer than … does not hold events back" | exactly 5 min still holds | test_an_event_waits_*, test_bug_event_held_back_* | **BUG-3b** |
| 33 | app/services/stripe_events.py:75 | `0 < order_id < 2**31` | ids the column can hold | 1 and 2³¹−1 looked up; 0, 2³¹ never queried | test_event_order_id_* (statement counter) | correct |
| 34 | app/services/stripe_events.py:629 | `updated_at < now-20min` | "not changed for this long" | not at T | test_sweeper_publishes_events_older_than_the_sweep_age | code asserted (A2) |
| 35 | app/services/stripe_events.py:632 | `.limit(SWEEP_BATCH)` | at most N per run | N | test_sweeper_batch_is_capped_* | correct |
| 36 | app/services/stripe_events.py:490-493 | `min(base*2**(n-1), max)` | doubling up to cap | cap inclusive | test_retry_delay_* | correct |
| 37 | app/services/payment/stripe_provider.py:672 | `abs(now-ts) > 300` | Stripe: refused when ts < now − tolerance | age 300 accepted, 301 refused; future symmetric | test_webhook_signature_* | correct (A8) |
| 38 | app/api/v1/admin.py:439/441 | `>= start`, `<= end` | date filter (any offset) | inclusive, µs exact on both DBs | test_admin_order_date_filters_are_inclusive_to_the_microsecond | correct (A6) |
| 39 | app/api/v1/admin.py:511 | `created_at >= now-period` | stats for 24h/7d/30d | inclusive | test_admin_stats_* | correct (A6) |
| 40 | app/api/v1/orders.py:1803 | `returned >= amount` ⇒ REFUNDED | refunded once refunds cover the charge | at equality | test_refunds_cover_the_charge_exactly_at_its_amount | correct |
| 41 | mock provider refund cap | refund ≤ remaining | — | +1 cent ⇒ 400 | same | correct |
| 42 | app/api/v1/orders.py:171 | `min_length=8, max_length=255` | API.md "between 8 and 255", else 400 | 8 and 255 OK | test_idempotency_key_length_bounds | correct |
| 43 | app/schemas/user.py:102 + passwords | ≥ 8 chars, ≤ 72 UTF-8 bytes | field description | 72 bytes OK (incl. 2-byte char), 73 ⇒ 422 | test_password_length_bounds | correct |
| 44 | app/schemas/common.py:158-159, product.py:147-167 | page ≥ 1 ≤ INT32_MAX, size 1..100, query 3..100 | — | bounds inclusive | test_pagination_bounds, test_search_query_length_bounds | correct |
| 45 | app/api/v1/products.py:174/316/348 | `total > page*size` | has_more | false at equality | test_has_more_*, test_second_page_* | correct |
| 46 | app/api/v1/products.py:260/262/264 | `>= min`, `<= max`, `stock > 0` | price filter, in_stock | inclusive to the cent; stock 1 | test_price_filters_*, test_in_stock_* | correct |
| 47 | app/api/v1/orders.py:366-368 | Σ price×qty → Numeric(10,2) | "no amount … has more digits than that" (schemas/order.py:23-27; test_catalog_money) | 99 999 999.99 OK; +0.01 refused cleanly | test_order_total_up_to_*, test_bug_order_total_past_numeric_10_2_* | **BUG-5** |

## Test inventory (298 cases)

Sections in the file: A pure arithmetic (parse_rate, \_wait, RateLimit fields, user_id_from_subject,
retry backoff, plan_remove_ordered, Cart.merge) · B tokens (access/refresh/reset/verify exp, logout
blacklist, sign-out-everywhere watermark) · C rate limits via API (default, RateLimit countdown, window
end ±1 ms, Retry-After ceil table, login lock pair/backstop/window, guest carts, checkout) · D cart via
API (stock, merge, update, variant, quantity schema, max lines account/guest, login merge, validate,
order stock to zero, cart/guest TTL & cookie) · E SQL time windows ±1 µs (unpaid expiry, idempotency
sweep + API replay, reconciliation window/cap/abort, stale attempt /pay + event hold, int32 order id,
sweeper age/batch, erasure grace, retention purge, data-retention status, admin date filters in 3
offsets, admin stats 24h/7d/30d, webhook tolerance past/future/sub-second) · F request bounds
(Idempotency-Key length, password chars/bytes, pagination, search length, price filters, in_stock,
has_more/page 2, order total vs Numeric(10,2), refunds ±1 cent).

## pytest summaries

SQLite (`uv run pytest tests/torture_suite/test_boundary_mutants.py -p no:cacheprovider -v`):

```
======================== 12 failed, 286 passed in 8.12s ========================
```

PostgreSQL 16 (`TEST_DATABASE_URL=postgresql+psycopg://torture:torture@localhost:5432/postgres …`):

```
======================= 12 failed, 286 passed in 17.13s ========================
```

Also stable under `-n 4` (12 failed, 286 passed). The 12 failures are exactly the `test_bug_*`
reproducers, identical on both back ends:

```
test_bug_logout_blacklist_expires_before_the_access_token[1ms|500ms|999ms]   ([whole-second] passes)
test_bug_rate_limit_client_waiting_exactly_retry_after_is_refused_again
test_bug_attempt_exactly_stale_after_minutes_old_no_longer_blocks
test_bug_event_held_back_by_attempt_exactly_stale_after_minutes_old
test_bug_data_retention_status_truncates_age_to_whole_days[1us|1h|23h59m]
test_bug_order_total_past_numeric_10_2_is_not_refused_cleanly[qty-x-price|two-lines|far-past]
```

No microsecond-precision divergence between SQLite and PostgreSQL was found: every ±1 µs SQL window
(and the admin date filter given in +00:00/+05:30/−08:00) answers the same on both, because
UTCDateTime normalises to UTC and SQLAlchemy's SQLite format always writes 6 fraction digits.

## Mutation kill matrix (manual, on /tmp/claude-mut-2d, SQLite)

Baseline: 12 failed (the bug tests), 286 passed. "Killed" = at least one non-bug test turned red.
"Fixes" = bug tests that turned green under the mutant.

| id | file | mutation | result (killing tests) | first killer | fixes |
|---|---|---|---|---|---|
| M01 | app/core/rate_limit.py | `if count <= rate.count:` → `if count < rate.count:` | KILLED (28) | test_an_address_opens_exactly_n_guest_carts[1] | 0 |
| M02 | app/core/rate_limit.py | `return max(1, math.ceil(ttl_ms / 1000))` → `return max(1, math.floor(ttl_ms / 1000))` | KILLED (7) | test_retry_after_is_the_ceiled_rest_of_the_window[0.001-60] | 0 |
| M03 | app/core/rate_limit.py | `remaining = max(0, rate.count - count)` → `remaining = max(0, rate.count - count + 1)` | KILLED (4) | test_ratelimit_remaining_counts_down_to_zero_at_the_limit | 0 |
| M04 | app/services/redis_codec.py | `if stock_quantity < current_quantity + quantity:` → `if stock_quantity <= current_quantity + quantity:` | KILLED (4) | test_add_up_to_exactly_the_stock[0-200] | 0 |
| M05 | app/services/redis_codec.py | `if stock_quantity < quantity:` → `if stock_quantity <= quantity:` | KILLED (1) | test_update_quantity_up_to_exactly_the_stock[0-200] | 0 |
| M06 | app/services/redis_codec.py | `len(cart.items) >= settings.CART_MAX_LINES` → `len(cart.items) > settings.CART_MAX_LINES` | KILLED (4) | test_cart_holds_exactly_max_lines_distinct_items[1] | 0 |
| M07 | app/schemas/common.py | `elif len(self.items) < max_lines:` → `elif len(self.items) <= max_lines:` | KILLED (5) | test_cart_merge_takes_new_lines_while_below_the_cap[0-4-3-3-1] | 0 |
| M08 | app/services/redis_codec.py | `if line.quantity <= ordered_item.quantity:` → `if line.quantity < ordered_item.quantity:` | KILLED (24) | test_an_in_flight_attempt_blocks_another_until_it_is_stale[T+1us] | 0 |
| M09 | app/api/v1/cart.py | `if available < item.quantity:` → `if available <= item.quantity:` | KILLED (1) | test_cart_validate_is_valid_exactly_when_stock_covers_the_quantity[0-T | 0 |
| M10 | app/api/v1/orders.py | `Product.stock_quantity >= item.quantity,` → `Product.stock_quantity > item.quantity,` | KILLED (1) | test_order_takes_stock_down_to_exactly_zero[0-201] | 0 |
| M11 | app/tasks.py | `Order.created_at < cutoff,` → `Order.created_at <= cutoff,` | survived (0) | - | 0 |
| M12 | app/tasks.py | `or order.created_at >= cutoff:` → `or order.created_at > cutoff:` | survived (0) | - | 0 |
| M13 | app/tasks.py | `Order.created_at < cutoff,` → `Order.created_at <= cutoff,` | KILLED (2) | test_idempotency_key_expires_only_when_older_than_the_ttl[T] | 0 |
| M14 | app/tasks.py | `User.data_deletion_date <= grace_cutoff,` → `User.data_deletion_date < grace_cutoff,` | KILLED (1) | test_erasure_runs_once_the_grace_period_has_passed[T] | 0 |
| M15 | app/tasks.py | `return now - last_seen > timedelta(days=days)` → `return now - last_seen >= timedelta(days=days)` | KILLED (1) | test_retention_purge_needs_more_than_the_retention_period[T] | 0 |
| M16 | app/services/reconciliation.py | `Payment.created_at >= now - timedelta(days=` → `Payment.created_at > now - timedelta(days=` | KILLED (1) | test_reconciliation_window_edges_to_the_microsecond[T-oldest] | 0 |
| M17 | app/services/reconciliation.py | `Payment.created_at <= now - timedelta(minutes=` → `Payment.created_at < now - timedelta(minutes=` | KILLED (1) | test_reconciliation_window_edges_to_the_microsecond[T-youngest] | 0 |
| M18 | app/services/reconciliation.py | `truncated=len(ids) > limit` → `truncated=len(ids) >= limit` | KILLED (1) | test_reconciliation_cap_truncates_only_beyond_it[3-False] | 0 |
| M19 | app/services/reconciliation.py | `if consecutive_errors >= settings.RECONCILIATION_MAX_CONSECU` → `if consecutive_errors > settings.RECONCILIATION_MAX_CONSECUT` | KILLED (2) | test_reconciliation_stops_after_exactly_n_errors_in_a_row[3-True] | 0 |
| M20 | app/api/v1/orders.py | `Payment.created_at > started_after,` → `Payment.created_at >= started_after,` | survived (0) | - | 1 |
| M21 | app/services/stripe_events.py | `Payment.created_at > started_after,` → `Payment.created_at >= started_after,` | survived (0) | - | 1 |
| M22 | app/services/stripe_events.py | `WebhookEvent.updated_at < now - timedelta(` → `WebhookEvent.updated_at <= now - timedelta(` | KILLED (1) | test_sweeper_publishes_events_older_than_the_sweep_age[T] | 0 |
| M23 | app/services/stripe_events.py | `if not 0 < order_id < 2 ** 31:` → `if not 0 < order_id <= 2 ** 31:` | KILLED (1) | test_event_order_id_outside_int32_is_never_looked_up[2147483648] | 0 |
| M24 | app/core/security.py | `return user_id if user_id <= _MAX_USER_ID else None` → `return user_id if user_id < _MAX_USER_ID else None` | KILLED (1) | test_user_id_from_subject_at_the_int32_edge[2147483647-2147483647] | 0 |
| M25 | app/services/redis_async.py | `or issued_at < changed_at` → `or issued_at <= changed_at` | KILLED (1) | test_sign_out_everywhere_refuses_tokens_issued_strictly_before_it[T] | 0 |
| M26 | app/core/security.py | `return issued_at, int(issued_at) + int(lifetime.total_second` → `return issued_at, int(issued_at) + int(lifetime.total_second` | KILLED (9) | test_access_token_exp_is_whole_seconds_so_lifetime_is_at_most_the_sett | 0 |
| M27 | app/core/security.py | `expire = utc_now() + timedelta(minutes=settings.PASSWORD_RES` → `expire = utc_now() + timedelta(minutes=settings.PASSWORD_RES` | KILLED (3) | test_password_reset_link_works_for_exactly_its_lifetime[T+1us] | 0 |
| M28 | app/core/security.py | `expire = utc_now() + timedelta(hours=settings.EMAIL_VERIFICA` → `expire = utc_now() + timedelta(hours=settings.EMAIL_VERIFICA` | KILLED (1) | test_email_verification_link_works_for_exactly_its_lifetime[T-1us] | 0 |
| M29 | app/api/v1/admin.py | `filters.append(Order.created_at >= as_utc(start_date))` → `filters.append(Order.created_at > as_utc(start_date))` | KILLED (3) | test_admin_order_date_filters_are_inclusive_to_the_microsecond[start_d | 0 |
| M30 | app/api/v1/admin.py | `filters.append(Order.created_at <= as_utc(end_date))` → `filters.append(Order.created_at < as_utc(end_date))` | KILLED (3) | test_admin_order_date_filters_are_inclusive_to_the_microsecond[end_dat | 0 |
| M31 | app/api/v1/admin.py | `Order.created_at >= start_date,` → `Order.created_at > start_date,` | KILLED (3) | test_admin_stats_24h_window_edge[T] | 0 |
| M32 | app/services/gdpr.py | `return age_in_days <= retention_days` → `return age_in_days < retention_days` | KILLED (1) | test_data_retention_status_inside_the_period[age1-True] | 3 |
| M33 | app/api/v1/products.py | `stmt = stmt.where(Product.price >= filter_params.min_price)` → `stmt = stmt.where(Product.price > filter_params.min_price)` | KILLED (1) | test_price_filters_are_inclusive_to_the_cent[min_price-10.00-True] | 0 |
| M34 | app/api/v1/products.py | `stmt = stmt.where(Product.price <= filter_params.max_price)` → `stmt = stmt.where(Product.price < filter_params.max_price)` | KILLED (1) | test_price_filters_are_inclusive_to_the_cent[max_price-10.00-True] | 0 |
| M35 | app/api/v1/products.py | `stmt = stmt.where(Product.stock_quantity > 0)` → `stmt = stmt.where(Product.stock_quantity >= 0)` | KILLED (1) | test_in_stock_filter_starts_at_one_unit[0-False] | 0 |
| M36 | app/services/payment/stripe_provider.py | `if abs(time.time() - timestamp) > tolerance_seconds:` → `if abs(time.time() - timestamp) >= tolerance_seconds:` | KILLED (3) | test_webhook_signature_from_the_future_is_bounded_by_the_same_toleranc | 0 |
| M37 | app/api/v1/orders.py | `PaymentStatus.REFUNDED if returned >= payment_row.amount els` → `PaymentStatus.REFUNDED if returned > payment_row.amount else` | KILLED (3) | test_refunds_cover_the_charge_exactly_at_its_amount[amounts1-codes1-re | 0 |
| M38 | app/services/stripe_events.py | `settings.STRIPE_EVENT_RETRY_BASE_SECONDS * 2 ** (attempts - ` → `settings.STRIPE_EVENT_RETRY_BASE_SECONDS * 2 ** attempts,` | KILLED (7) | test_retry_delay_cap_is_inclusive[480-4-240] | 0 |
| M39 | app/core/security.py | `expires_in = max(int(payload["exp"] - time.time()), 1)` → `expires_in = max(math.ceil(payload["exp"] - time.time()), 1)` | survived (0) | - | 3 |
| M40 | app/core/rate_limit.py | `if ttl < 0 then` → `if ttl < -1 then` | KILLED (9) | test_fixed_window_ends_after_its_length[W+1ms] | 0 |
| M41 | app/core/rate_limit.py | `return max(1, math.ceil(ttl_ms / 1000))` → `return max(0, math.ceil(ttl_ms / 1000))` | KILLED (2) | test_wait_is_zero_up_to_the_limit_and_the_ceiled_rest_of_the_window_af | 1 |
| M42 | app/api/v1/products.py | `has_more=total > filter_params.page * filter_params.size` → `has_more=total >= filter_params.page * filter_params.size` | KILLED (2) | test_has_more_is_true_only_beyond_the_page[3-False] | 0 |
| M43 | app/schemas/cart.py | `CartQuantity = Annotated[int, Field(ge=1, le=INT32_MAX)]` → `CartQuantity = Annotated[int, Field(ge=0, le=INT32_MAX)]` | KILLED (1) | test_cart_quantity_bounds[0-10-422] | 0 |
| M44 | app/services/redis_codec.py | `return timedelta(days=settings.REDIS_GUEST_CART_TTL_DAYS)` → `return timedelta(days=settings.REDIS_GUEST_CART_TTL_DAYS, mi` | KILLED (1) | test_cart_ttl_comes_from_the_settings | 0 |
| M45 | app/api/v1/orders.py | `alias="Idempotency-Key", min_length=8, max_length=255,` → `alias="Idempotency-Key", min_length=7, max_length=256,` | KILLED (2) | test_idempotency_key_length_bounds[256-400] | 0 |
| M46 | app/tasks.py | `cutoff = utc_now() - timedelta(minutes=settings.UNPAID_ORDER` → `cutoff = utc_now() - timedelta(minutes=settings.UNPAID_ORDER` | KILLED (1) | test_unpaid_order_expires_only_when_older_than_the_window[T] | 0 |
| M11+M12 | M11+M12 | `` → `` | KILLED (1) | test_unpaid_order_expires_only_when_older_than_the_window[T] | 0 |
| M20+M21 | M20+M21 | `` → `` | survived (0) | - | 2 |

**Score: 41 of 46 single mutants killed. The 5 survivors are 2 equivalent mutants (M11, M12 — killed when combined, M11+M12) and 3 mutants that are the proposed bug fixes (M20, M21, M39). Every non-equivalent, non-fix mutant is killed (41/41).**

* M11 / M12 survive individually: they are **equivalent mutants**. `expire_unpaid_orders` filters with
  `created_at < cutoff` in SQL and re-checks `created_at >= cutoff` under the lock; flipping either
  guard alone is masked by the other. Flipping both (M11+M12) is killed by the T case.
* M20 / M21 / M20+M21 (`>` → `>=` on the stale-attempt window) and M39 (`int` → `math.ceil` for the
  blacklist TTL) break no other test: they are the **proposed fixes** for BUG-3 and BUG-1 and make those
  reproducers pass.
* M32 (gdpr `<=` → `<`) turns the bug tests green but is killed by the "exactly 365 days" case, so it is
  not a correct fix; the real fix compares timedeltas (see BUG-4).
* M41 (`max(1, …)` → `max(0, …)`) is killed by the unit table; it also turns BUG-2 green, which confirms
  that BUG-2 is the PTTL = 0 instant being refused.

## Suspected bugs

### BUG-1 — a logged-out access token works again before its exp (blacklist TTL truncated)
* Test: `test_bug_logout_blacklist_expires_before_the_access_token[1ms|500ms|999ms]`
* Production: `app/core/security.py:625` — `expires_in = max(int(payload["exp"] - time.time()), 1)`
* Input: login at T0 (whole second, exp = T0+900); `POST /users/logout` **with** its refresh token at
  T0+0.5 s; `GET /users/me` with the old access token at exp − 1 µs.
* Observed 200 (blacklist key, EX 899, lapsed at T0+899.5 s). Expected 401.
* Intent: `logout()` "Blacklist the current access token"; `blacklist_token` "Revoke an access token
  for expires_in seconds"; existing `test_logout_blacklists_the_token_for_its_remaining_lifetime`;
  RFC 7519 exp. Logout without a refresh token is not affected (watermark covers it —
  `test_logout_everywhere_has_no_gap_before_exp` passes).
* Not a test bug: the gap closes exactly when the TTL is rounded up (mutant M39), and the
  whole-second logout case passes. Window: up to ~1 s per logout, deterministic.
* Fix:
```diff
--- a/app/core/security.py
+++ b/app/core/security.py
@@
-        expires_in = max(int(payload["exp"] - time.time()), 1)
+        expires_in = max(math.ceil(payload["exp"] - time.time()), 1)
```
  (plus `import math`).

### BUG-2 — a client that waits exactly Retry-After seconds is refused again (1 ms)
* Test: `test_bug_rate_limit_client_waiting_exactly_retry_after_is_refused_again`
* Production: `app/core/rate_limit.py:219-221` (PEXPIRE W ms) and `:251-253` (`_wait`).
* Input: RATE_LIMIT_DEFAULT=2/minute; requests at T0 → 200, 200, 429 `Retry-After: 60`; request at
  exactly T0+60.000 → observed **429 (Retry-After: 1)**, expected 200.
* Cause: Redis (and fakeredis) expire a key only when `now > expireat`; at the instant the window ends
  PTTL is 0 and `_wait` answers `max(1, ceil(0)) = 1`. The window is effectively W + 1 ms.
* Intent: module docstring "429 with Retry-After = seconds until the window ends"; docs/API.md
  "Retry-After matching the remaining window seconds"; RFC 9110 §10.2.3.
* Severity: low (1 ms; clients add latency). Recorded because it is a clean off-by-one.
* Fix (admit at the window's last instant; never emits Retry-After: 0):
```diff
 def _wait(count: int, ttl_ms: int, rate: Rate) -> int:
-    if count <= rate.count:
+    if count <= rate.count or ttl_ms <= 0:
         return 0
     return max(1, math.ceil(ttl_ms / 1000))
```
  (alternatively PEXPIRE `ARGV[1] - 1` in the Lua script).

### BUG-3 — a payment attempt exactly PAYMENT_ATTEMPT_STALE_AFTER_MINUTES old is already "stale" (two sites)
* Tests: `test_bug_attempt_exactly_stale_after_minutes_old_no_longer_blocks` (via `POST /orders/{id}/pay`),
  `test_bug_event_held_back_by_attempt_exactly_stale_after_minutes_old` (service).
* Production: `app/api/v1/orders.py:925` and `app/services/stripe_events.py:87` —
  `Payment.created_at > started_after`.
* Input: an in-flight attempt (requires_action / processing+pending id) with created_at = now − 5 min
  exactly (µs rows, both DBs). Observed: /pay answers 200 (a second attempt starts) and the event is
  not held back. Expected: 409 / held back.
* Intent: `_guard_no_attempt_in_flight` "An attempt in flight for **longer than**
  PAYMENT_ATTEMPT_STALE_AFTER_MINUTES no longer blocks"; `_attempt_still_storing_its_id` "an attempt
  in flight for **longer than** … does not hold events back". Exactly 5 min is not longer than 5 min.
  T−1 µs/T+1 µs cases pass, so the harness is right; only the T instant differs.
* Severity: low (1 µs window, consistency/µs-precise contract).
* Fix (M20/M21, verified no regression):
```diff
-        Payment.created_at > started_after,
+        Payment.created_at >= started_after,
```
  in both files.

### BUG-4 — GET /legal/data-retention reports "within retention" for up to a day past it
* Test: `test_bug_data_retention_status_truncates_age_to_whole_days[1us|1h|23h59m]`
* Production: `app/services/gdpr.py:180-181` (`(utc_now() - user.created_at).days <= retention_days`),
  served by `app/api/v1/legal.py:376-387`.
* Input: account created 365 d + 1 µs / + 1 h / + 23 h 59 m ago, retention 365 → observed
  `within_retention_period: true`, expected false. (Exactly 365 d and 365 d − 1 µs → true, passing.)
* Intent: route doc "Check if user data is within the retention period"; the purge
  (`app/tasks.py:447`) treats `now - last_seen > timedelta(days=N)` as expired — the two disagree for
  ~24 h. `.days` floors the age.
* Fix:
```diff
-        age_in_days = (utc_now() - user.created_at).days
-        return age_in_days <= retention_days
+        return utc_now() - user.created_at <= timedelta(days=retention_days)
```
  (Separately, the status measures from created_at while the purge measures from the last activity;
  out of scope for a boundary finding, noted for the reviewer.)

### BUG-5 — an order whose total passes Numeric(10,2) is a 500 on PostgreSQL and stored on SQLite
* Test: `test_bug_order_total_past_numeric_10_2_is_not_refused_cleanly[qty-x-price|two-lines|far-past]`
* Production: `app/api/v1/orders.py:366-368` (`total_amount += price_at_time * item.quantity`;
  `order.total_amount = total_amount`, never bounded).
* Input: cart = 2 × 50 000 000.00 (or 99 999 999.99 + 0.01) → total 100 000 000.00, one cent past the
  largest Numeric(10,2). Observed: **PostgreSQL 500** "A database error occurred" (numeric field
  overflow); **SQLite 201** with total "100000000.00" stored — an order no payment request can name
  (Money max_digits=10). Expected: a 4xx refusal, no order, no stock taken, the same on both DBs.
  99 999 999.99 exactly → 201 on both (`test_order_total_up_to_the_largest_numeric_10_2` passes).
* Intent: `app/schemas/order.py:23-27` "Order.total_amount is Numeric(10, 2), so no amount paid or
  refunded for an order has more digits than that"; `tests/integration/test_catalog_money.py`
  docstring: an amount that does not fit "used to answer 500 (... the database refuses the value)".
* Fix:
```diff
         total_amount += price_at_time * item.quantity
 
+    if total_amount >= Decimal(10) ** (ORDER_AMOUNT_MAX_DIGITS - ORDER_AMOUNT_DECIMAL_PLACES):
+        await db.rollback()
+        raise Problem(
+            status.HTTP_422_UNPROCESSABLE_CONTENT,
+            detail="The order total is larger than an order can hold",
+        )
     order.total_amount = total_amount
```

## Ambiguities (code behaviour asserted, not reported as bugs)

* A1 Unpaid-order expiry at exactly 30 min: kept (`<`). config says "how long an order may stay
  unpaid", tasks.py "still unpaid 30 minutes after it was placed"; the existing
  test_order_expiry_time pins "more than", so strict is the project's choice.
* A2 Stripe-event sweeper at exactly 20 min: not swept (`<`). "have not changed for this long" reads
  inclusive, but the phrasing matches A1, which the project treats as strict.
* A3 Retention purge at exactly N days: not purged (`>`). Privacy policy "keep for N days" (exclusive)
  vs OPERATIONS.md "inactive for N days" (inclusive).
* A4 Reconciliation window: both ends inclusive (7 d exactly and 60 min exactly are checked).
* A5 Access/refresh lifetime: `exp = int(iat) + lifetime` makes a token issued at X.f live
  lifetime − f s (documented in `_issued_at_and_expiry`: "exp stays whole seconds"); conservative.
* A6 Admin order date filters and admin stats windows are inclusive (undocumented; conventional).
* A7 Reconciliation reports `aborted=True` when exactly MAX_CONSECUTIVE_ERRORS payments failed even
  if none was left to check.
* A8 Mock webhook tolerance is symmetric (future timestamps > 300 s refused); Stripe's SDK has no
  future check. Deliberate (tests/unit/test_webhook_signature.py), not a boundary defect.

## Boundaries proven correct

Every row of the inventory marked "correct": rate counts/headers/Retry-After ceil table/window ±1 ms,
login lock (pair, backstop, window), guest-cart cap, JWT exp at T (RFC 7519) for access, refresh,
reset and verification tokens, watermark strict-before, int32 subject/order id, cart stock/merge/
max-lines/validate/remove-ordered/TTL & cookie, order stock to zero, quantity bounds, idempotency-key
TTL (task and API replay) and header length, erasure grace (inclusive), reconciliation window/cap/abort,
sweeper batch, retry backoff cap, webhook tolerance (past/future/sub-second), admin filters in three
offsets, refunds ±1 cent, pagination/has_more/page 2, search length, price/in_stock filters, password
8 chars / 72 bytes (incl. a 2-byte character straddling the limit).
