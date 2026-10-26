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
