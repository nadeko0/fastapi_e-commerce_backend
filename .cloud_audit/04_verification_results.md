# 04 — Verification tests for the confirmed audit findings

Author: Verification Test Author. Target: v4.0.3. Date: 2026-10-26.
Inputs: `04_validation_phase2.md` (canonical IDs), `02a`–`02d` reports, scratch repros in `.cloud_audit/repro/`.

## Files created (nothing under `app/`, `migrations/`, existing tests or conftest was touched; no commit)

- `tests/audit_verification/test_cloud_audit.py`: 29 tests, run on SQLite and PostgreSQL.
- `tests/audit_verification/test_postgres_cloud_audit.py`: 4 tests. They are PostgreSQL-only through `requires_postgres` from `tests/conftest.py`. The name matches `test_postgres_*.py`, so conftest marks them `db_sensitive`, and CI's `test-postgres` job picks them up.
- No `__init__.py`. The suite has none anywhere: rootdir/prepend import with `pythonpath = ["."]`. The integration helpers (`payment_support`, `test_admin`) are imported by bare name after `tests/integration` is put on `sys.path`, which mirrors how the integration tests import them.

## How the tests are built

- Every test asserts the **current, faulty** behaviour, so a PASS means the defect has been reproduced.
- Each test has a docstring giving the ID, severity, file:line, scenario, and "When fixed, invert the assertion: …".
- No new markers were added.
- Races are forced deterministically:
  - SQLite: monkeypatched provider, bcrypt and `is_token_superseded` hooks.
  - PostgreSQL: an `after_cursor_execute` hook on `async_engine.sync_engine` holds transaction A until `pg_stat_activity` shows B waiting on a lock, or until the server has ended the backend. There is no sleep. Polling has a 30 s fail-safe.
- The PostgreSQL tests were re-run 3 more times: 4 passed each time (about 3.1 s).

## Environment for the PostgreSQL run

- Throwaway PostgreSQL 16.15 cluster: `initdb` as user `postgres` in `scratchpad/pgv`, served on 127.0.0.1:55442, auth=trust, no Unix socket because the scratchpad path is longer than 107 bytes.
- The sandbox kept resetting `/tmp/claude-0` to 0700. On the first attempt this crashed the checkpointer with a PANIC at shutdown time; all tests had already passed. The data dir (still physically `scratchpad/pgv`) was therefore served through a bind mount at `/tmp/pgv_bind`.
- The cluster was stopped (`pg_ctl stop -m fast`), unmounted and deleted (`rm -rf scratchpad/pgv`) afterwards.

## 1. SQLite run (default harness)

```
$ cd /home/user/fastapi_e-commerce_backend && unset UV_NATIVE_TLS; uv run pytest tests/audit_verification/ -v -p no:cacheprovider
============================= test session starts ==============================
platform linux -- Python 3.13.17, pytest-9.1.1, pluggy-1.7.0 -- /home/user/fastapi_e-commerce_backend/.venv/bin/python
hypothesis profile 'dev'
rootdir: /home/user/fastapi_e-commerce_backend
configfile: pyproject.toml
plugins: hypothesis-6.170.5, xdist-3.8.0, anyio-4.15.1, cov-7.1.0
collecting ... collected 33 items

tests/audit_verification/test_cloud_audit.py::test_PAY01_late_3ds_charge_on_expired_order_is_unrefundable_and_unreported PASSED [  3%]
tests/audit_verification/test_cloud_audit.py::test_PAY01_customer_cancel_ignores_in_flight_3ds_attempt PASSED [  6%]
tests/audit_verification/test_cloud_audit.py::test_PAY01_second_checkout_session_after_stale_window_double_charges PASSED [  9%]
tests/audit_verification/test_cloud_audit.py::test_PAY02_checkout_session_without_webhook_is_skipped_by_reconciliation PASSED [ 12%]
tests/audit_verification/test_cloud_audit.py::test_PAY03_stale_pending_refund_event_regresses_refunded_order_to_paid PASSED [ 15%]
tests/audit_verification/test_cloud_audit.py::test_GDPR01_webhook_event_after_erasure_keeps_pii_forever PASSED [ 18%]
tests/audit_verification/test_cloud_audit.py::test_GDPR02_purge_hashes_bcrypt_inside_one_open_transaction PASSED [ 21%]
tests/audit_verification/test_cloud_audit.py::test_GDPR03_patch_me_changes_marketing_consent_without_history PASSED [ 24%]
tests/audit_verification/test_cloud_audit.py::test_GDPR04_erasure_redacts_address_of_paid_unshipped_order PASSED [ 27%]
tests/audit_verification/test_cloud_audit.py::test_GDPR05_data_export_contains_no_personal_data PASSED [ 30%]
tests/audit_verification/test_cloud_audit.py::test_AUTH01_login_in_flight_during_password_reset_survives_it PASSED [ 33%]
tests/audit_verification/test_cloud_audit.py::test_AUTH01_refresh_rotation_in_flight_during_reset_survives_it PASSED [ 36%]
tests/audit_verification/test_cloud_audit.py::test_AUTH02_deletion_reauth_is_an_unthrottled_password_oracle PASSED [ 39%]
tests/audit_verification/test_cloud_audit.py::test_REG01_registration_without_full_name_never_sends_verification_mail PASSED [ 42%]
tests/audit_verification/test_cloud_audit.py::test_DATA01_address_edit_rewrites_delivered_order PASSED [ 45%]
tests/audit_verification/test_cloud_audit.py::test_2A05_pending_rows_consume_the_reconciliation_cap PASSED [ 48%]
tests/audit_verification/test_cloud_audit.py::test_2A06_pay_answers_paid_when_success_was_not_applied PASSED [ 51%]
tests/audit_verification/test_cloud_audit.py::test_2A07_orphaned_processing_attempt_answers_409_forever PASSED [ 54%]
tests/audit_verification/test_cloud_audit.py::test_2B02_sync_engine_hook_never_sees_route_stock_updates PASSED [ 57%]
tests/audit_verification/test_cloud_audit.py::test_2B04_merge_patch_replaces_nested_characteristics PASSED [ 60%]
tests/audit_verification/test_cloud_audit.py::test_2B04_merge_patch_stores_null_in_variant_attributes PASSED [ 63%]
tests/audit_verification/test_cloud_audit.py::test_2B07_order_total_is_not_bounded_by_its_numeric_column PASSED [ 66%]
tests/audit_verification/test_cloud_audit.py::test_2B09_guest_cart_merge_drops_lines_silently PASSED [ 69%]
tests/audit_verification/test_cloud_audit.py::test_2B11_expiry_sweep_aborts_on_first_error PASSED [ 72%]
tests/audit_verification/test_cloud_audit.py::test_2C12_deletion_confirmation_claims_data_already_deleted PASSED [ 75%]
tests/audit_verification/test_cloud_audit.py::test_2D02_OBS01_importing_the_celery_app_configures_no_logging PASSED [ 78%]
tests/audit_verification/test_cloud_audit.py::test_2D03_deletion_mail_is_sent_inside_the_open_transaction PASSED [ 81%]
tests/audit_verification/test_cloud_audit.py::test_2D04_database_uri_is_not_url_encoded PASSED [ 84%]
tests/audit_verification/test_cloud_audit.py::test_2D07_order_email_celery_tasks_are_never_enqueued PASSED [ 87%]
tests/audit_verification/test_postgres_cloud_audit.py::test_DB01_variant_checkout_deadlocks_plain_plus_variant_checkout SKIPPED [ 90%]
tests/audit_verification/test_postgres_cloud_audit.py::test_DB01_variant_checkout_deadlocks_cancel_of_plain_plus_variant_order SKIPPED [ 93%]
tests/audit_verification/test_postgres_cloud_audit.py::test_2B08_hard_product_delete_racing_checkout_answers_500 SKIPPED [ 96%]
tests/audit_verification/test_postgres_cloud_audit.py::test_GDPR02_purge_dies_on_idle_in_transaction_timeout SKIPPED [100%]

=========================== short test summary info ============================
SKIPPED [1] tests/audit_verification/test_postgres_cloud_audit.py:102: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/audit_verification/test_postgres_cloud_audit.py:132: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/audit_verification/test_postgres_cloud_audit.py:160: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/audit_verification/test_postgres_cloud_audit.py:186: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
======================== 29 passed, 4 skipped in 3.62s =========================
exit=0
```

## 2. PostgreSQL 16 run

```
$ TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55442/postgres uv run pytest tests/audit_verification/ -v -p no:cacheprovider
============================= test session starts ==============================
platform linux -- Python 3.13.17, pytest-9.1.1, pluggy-1.7.0 -- /home/user/fastapi_e-commerce_backend/.venv/bin/python
hypothesis profile 'dev'
rootdir: /home/user/fastapi_e-commerce_backend
configfile: pyproject.toml
plugins: hypothesis-6.170.5, xdist-3.8.0, anyio-4.15.1, cov-7.1.0
collecting ... collected 33 items

tests/audit_verification/test_cloud_audit.py::test_PAY01_late_3ds_charge_on_expired_order_is_unrefundable_and_unreported PASSED [  3%]
tests/audit_verification/test_cloud_audit.py::test_PAY01_customer_cancel_ignores_in_flight_3ds_attempt PASSED [  6%]
tests/audit_verification/test_cloud_audit.py::test_PAY01_second_checkout_session_after_stale_window_double_charges PASSED [  9%]
tests/audit_verification/test_cloud_audit.py::test_PAY02_checkout_session_without_webhook_is_skipped_by_reconciliation PASSED [ 12%]
tests/audit_verification/test_cloud_audit.py::test_PAY03_stale_pending_refund_event_regresses_refunded_order_to_paid PASSED [ 15%]
tests/audit_verification/test_cloud_audit.py::test_GDPR01_webhook_event_after_erasure_keeps_pii_forever PASSED [ 18%]
tests/audit_verification/test_cloud_audit.py::test_GDPR02_purge_hashes_bcrypt_inside_one_open_transaction PASSED [ 21%]
tests/audit_verification/test_cloud_audit.py::test_GDPR03_patch_me_changes_marketing_consent_without_history PASSED [ 24%]
tests/audit_verification/test_cloud_audit.py::test_GDPR04_erasure_redacts_address_of_paid_unshipped_order PASSED [ 27%]
tests/audit_verification/test_cloud_audit.py::test_GDPR05_data_export_contains_no_personal_data PASSED [ 30%]
tests/audit_verification/test_cloud_audit.py::test_AUTH01_login_in_flight_during_password_reset_survives_it PASSED [ 33%]
tests/audit_verification/test_cloud_audit.py::test_AUTH01_refresh_rotation_in_flight_during_reset_survives_it PASSED [ 36%]
tests/audit_verification/test_cloud_audit.py::test_AUTH02_deletion_reauth_is_an_unthrottled_password_oracle PASSED [ 39%]
tests/audit_verification/test_cloud_audit.py::test_REG01_registration_without_full_name_never_sends_verification_mail PASSED [ 42%]
tests/audit_verification/test_cloud_audit.py::test_DATA01_address_edit_rewrites_delivered_order PASSED [ 45%]
tests/audit_verification/test_cloud_audit.py::test_2A05_pending_rows_consume_the_reconciliation_cap PASSED [ 48%]
tests/audit_verification/test_cloud_audit.py::test_2A06_pay_answers_paid_when_success_was_not_applied PASSED [ 51%]
tests/audit_verification/test_cloud_audit.py::test_2A07_orphaned_processing_attempt_answers_409_forever PASSED [ 54%]
tests/audit_verification/test_cloud_audit.py::test_2B02_sync_engine_hook_never_sees_route_stock_updates PASSED [ 57%]
tests/audit_verification/test_cloud_audit.py::test_2B04_merge_patch_replaces_nested_characteristics PASSED [ 60%]
tests/audit_verification/test_cloud_audit.py::test_2B04_merge_patch_stores_null_in_variant_attributes PASSED [ 63%]
tests/audit_verification/test_cloud_audit.py::test_2B07_order_total_is_not_bounded_by_its_numeric_column PASSED [ 66%]
tests/audit_verification/test_cloud_audit.py::test_2B09_guest_cart_merge_drops_lines_silently PASSED [ 69%]
tests/audit_verification/test_cloud_audit.py::test_2B11_expiry_sweep_aborts_on_first_error PASSED [ 72%]
tests/audit_verification/test_cloud_audit.py::test_2C12_deletion_confirmation_claims_data_already_deleted PASSED [ 75%]
tests/audit_verification/test_cloud_audit.py::test_2D02_OBS01_importing_the_celery_app_configures_no_logging PASSED [ 78%]
tests/audit_verification/test_cloud_audit.py::test_2D03_deletion_mail_is_sent_inside_the_open_transaction PASSED [ 81%]
tests/audit_verification/test_cloud_audit.py::test_2D04_database_uri_is_not_url_encoded PASSED [ 84%]
tests/audit_verification/test_cloud_audit.py::test_2D07_order_email_celery_tasks_are_never_enqueued PASSED [ 87%]
tests/audit_verification/test_postgres_cloud_audit.py::test_DB01_variant_checkout_deadlocks_plain_plus_variant_checkout PASSED [ 90%]
tests/audit_verification/test_postgres_cloud_audit.py::test_DB01_variant_checkout_deadlocks_cancel_of_plain_plus_variant_order PASSED [ 93%]
tests/audit_verification/test_postgres_cloud_audit.py::test_2B08_hard_product_delete_racing_checkout_answers_500 PASSED [ 96%]
tests/audit_verification/test_postgres_cloud_audit.py::test_GDPR02_purge_dies_on_idle_in_transaction_timeout PASSED [100%]

============================== 33 passed in 7.37s ==============================
exit=0
```

Server log (`pg.log`) for that run confirms the mechanisms: `ERROR: numeric field overflow` (2B-07), 2x `ERROR: deadlock detected ... while locking tuple in relation "product_variants"` (DB-01), `ERROR: update or delete on table "products" violates foreign key constraint "fk_order_items_product_id_products"` (2B-08), `FATAL: terminating connection due to idle-in-transaction timeout` (GDPR-02).

## 3. Ruff

```
$ uv run ruff check tests/audit_verification
All checks passed!
exit=0
```

## 4. Full fast suite (with the new files)

Baseline (01_baseline.md): 5214 passed, 109 skipped. Now 5243 passed (+29) and 113 skipped (+4 PostgreSQL-only).

```
$ uv run pytest tests/ -q -n 4 -m "not timing_sensitive" -p no:cacheprovider
bringing up nodes...
bringing up nodes...

........................................................................ [  1%]
.............................s.......................................... [  2%]
........................................................................ [  4%]
........................................................................ [  5%]
............................................ssss........................ [  6%]
........................................................................ [  8%]
........................................................................ [  9%]
...........................................ssssssss..................... [ 10%]
........................................................................ [ 12%]
........................................................................ [ 13%]
........................................................................ [ 14%]
........................................................................ [ 16%]
........................................................................ [ 17%]
........................................................................ [ 18%]
........................................................................ [ 20%]
........................................................................ [ 21%]
........................................................................ [ 22%]
........................................................................ [ 24%]
........................................................................ [ 25%]
...................................................................s..s. [ 26%]
s.s.s............s....s............s.................s.s.s.s.s.......... [ 28%]
........................................ssssssssssssssss.sssssssssssssss [ 29%]
........................................................................ [ 30%]
........................................................................ [ 32%]
........................................................................ [ 33%]
........................................................................ [ 34%]
..........s..............s.s.s.s..........ssssssssssssssssssssssssssssss [ 36%]
ssssssssss.............s.s.s...s...................s.s.s.s..s.s.s....... [ 37%]
........................................................................ [ 38%]
........................................................................ [ 40%]
........................................................................ [ 41%]
........................................................................ [ 43%]
........................................................................ [ 44%]
........................................................................ [ 45%]
........................................................................ [ 47%]
........................................................................ [ 48%]
........................................................................ [ 49%]
........................................................................ [ 51%]
........................................................................ [ 52%]
........................................................................ [ 53%]
........................................................................ [ 55%]
........................................................................ [ 56%]
........................................................................ [ 57%]
........................................................................ [ 59%]
........................................................................ [ 60%]
........................................................................ [ 61%]
........................................................................ [ 63%]
........................................................................ [ 64%]
........................................................................ [ 65%]
........................................................................ [ 67%]
........................................................................ [ 68%]
........................................................................ [ 69%]
........................................................................ [ 71%]
........................................................................ [ 72%]
........................................................................ [ 73%]
........................................................................ [ 75%]
........................................................................ [ 76%]
........................................................................ [ 77%]
........................................................................ [ 79%]
........................................................................ [ 80%]
........................................................................ [ 82%]
........................................................................ [ 83%]
........................................................................ [ 84%]
........................................................................ [ 86%]
........................................................................ [ 87%]
........................................................................ [ 88%]
........................................................................ [ 90%]
........................................................................ [ 91%]
........................................................................ [ 92%]
........................................................................ [ 94%]
........................................................................ [ 95%]
........................................................................ [ 96%]
........................................................................ [ 98%]
........................................................................ [ 99%]
............................                                             [100%]
=========================== short test summary info ============================
SKIPPED [1] tests/integration/test_idempotency_fingerprint.py:348: two real concurrent requests need row locks
SKIPPED [1] tests/audit_verification/test_postgres_cloud_audit.py:102: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/audit_verification/test_postgres_cloud_audit.py:132: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/audit_verification/test_postgres_cloud_audit.py:160: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/audit_verification/test_postgres_cloud_audit.py:186: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_checkout_session_live_stripe.py:84: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Checkout Session integration suite.
SKIPPED [1] tests/integration/test_checkout_session_live_stripe.py:108: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Checkout Session integration suite.
SKIPPED [1] tests/integration/test_checkout_session_live_stripe.py:124: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Checkout Session integration suite.
SKIPPED [1] tests/integration/test_checkout_session_live_stripe.py:144: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Checkout Session integration suite.
SKIPPED [1] tests/integration/test_checkout_session_live_stripe.py:157: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Checkout Session integration suite.
SKIPPED [1] tests/integration/test_checkout_session_live_stripe.py:167: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Checkout Session integration suite.
SKIPPED [1] tests/integration/test_checkout_session_live_stripe.py:190: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Checkout Session integration suite.
SKIPPED [1] tests/integration/test_checkout_session_live_stripe.py:206: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Checkout Session integration suite.
SKIPPED [2] tests/integration/test_real_redis_parity.py:356: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [2] tests/integration/test_real_redis_parity.py:379: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [2] tests/integration/test_real_redis_parity.py:411: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [2] tests/integration/test_real_redis_parity.py:423: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [2] tests/integration/test_real_redis_parity.py:434: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [2] tests/integration/test_real_redis_parity.py:447: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [2] tests/integration/test_real_redis_parity.py:466: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [1] tests/integration/test_postgres_constraint_names.py:58: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_constraint_names.py:65: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_constraint_names.py:86: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_constraint_names.py:107: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_data_migrations.py:108: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_data_migrations.py:149: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_data_migrations.py:171: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_data_migrations.py:195: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_indexes.py:24: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_indexes.py:41: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_only.py:88: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_only.py:117: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_only.py:190: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [2] tests/integration/test_postgres_only.py:210: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [6] tests/integration/test_postgres_only.py:236: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_only.py:256: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_only.py:272: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_only.py:287: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_only.py:336: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_only.py:387: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_pool_recovery.py:64: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_pool_recovery.py:71: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_pool_recovery.py:88: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_pool_recovery.py:103: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_postgres_statement_timeout.py:49: needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...
SKIPPED [1] tests/integration/test_reconciliation_live_stripe.py:66: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live reconciliation test.
SKIPPED [1] tests/integration/test_real_redis_parity.py:156: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [1] tests/integration/test_real_redis_parity.py:178: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [1] tests/integration/test_real_redis_parity.py:190: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [1] tests/integration/test_real_redis_parity.py:201: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [1] tests/integration/test_payment_live_journey.py:138: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe journey.
SKIPPED [9] tests/integration/test_payment_live_journey.py:171: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe journey.
SKIPPED [9] tests/integration/test_payment_live_journey.py:186: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe journey.
SKIPPED [4] tests/integration/test_payment_live_journey.py:205: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe journey.
SKIPPED [1] tests/integration/test_payment_live_journey.py:220: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe journey.
SKIPPED [2] tests/integration/test_payment_live_journey.py:233: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe journey.
SKIPPED [1] tests/integration/test_payment_live_journey.py:253: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe journey.
SKIPPED [1] tests/integration/test_payment_live_journey.py:272: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe journey.
SKIPPED [1] tests/integration/test_payment_live_stripe.py:95: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe integration suite.
SKIPPED [1] tests/integration/test_payment_live_stripe.py:115: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe integration suite.
SKIPPED [1] tests/integration/test_payment_live_stripe.py:135: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe integration suite.
SKIPPED [1] tests/integration/test_payment_live_stripe.py:161: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe integration suite.
SKIPPED [1] tests/integration/test_payment_live_stripe.py:176: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe integration suite.
SKIPPED [1] tests/integration/test_payment_live_stripe.py:189: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe integration suite.
SKIPPED [1] tests/integration/test_payment_live_stripe.py:203: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe integration suite.
SKIPPED [1] tests/integration/test_payment_live_stripe.py:216: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe integration suite.
SKIPPED [1] tests/integration/test_payment_live_stripe.py:261: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe integration suite.
SKIPPED [1] tests/integration/test_payment_live_stripe.py:269: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe integration suite.
SKIPPED [1] tests/integration/test_payment_live_stripe.py:278: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe integration suite.
SKIPPED [1] tests/integration/test_payment_live_stripe.py:285: No real Stripe test-mode STRIPE_SECRET_KEY in the environment (set STRIPE_SECRET_KEY or provide .env.test) - skipping the live Stripe integration suite.
SKIPPED [1] tests/integration/test_real_redis_parity.py:215: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [1] tests/integration/test_real_redis_parity.py:230: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [1] tests/integration/test_real_redis_parity.py:242: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [1] tests/integration/test_real_redis_parity.py:265: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [1] tests/integration/test_real_redis_parity.py:282: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [1] tests/integration/test_real_redis_parity.py:296: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [2] tests/integration/test_real_redis_parity.py:316: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
SKIPPED [2] tests/integration/test_real_redis_parity.py:337: set REAL_REDIS_URL=redis://host:port/db to run on a real Redis
5243 passed, 113 skipped in 148.00s (0:02:27)
{"timestamp": "2026-10-26T20:49:44.501Z", "level": "INFO", "logger": "app.core.logging_config", "message": "Starting application in development environment (Version: 4.0.3)"}
{"timestamp": "2026-10-26T20:49:44.501Z", "level": "INFO", "logger": "app.core.logging_config", "message": "Log file disabled (LOG_FILE is not set); logging to stdout only"}
{"timestamp": "2026-10-26T20:49:44.501Z", "level": "INFO", "logger": "app.main", "message": "Initializing FastAPI application"}
{"timestamp": "2026-10-26T20:49:44.501Z", "level": "INFO", "logger": "app.main", "message": "Configuring FastAPI middleware, CORS, and rate limiting"}
{"timestamp": "2026-10-26T20:49:44.501Z", "level": "INFO", "logger": "app.main", "message": "Initializing FastAPI application with rate limiting"}
{"timestamp": "2026-10-26T20:49:44.502Z", "level": "INFO", "logger": "app.main", "message": "Initializing service connections"}
{"timestamp": "2026-10-26T20:49:44.502Z", "level": "INFO", "logger": "app.main", "message": "Redis service initialized"}
exit=0
```

## 5. Finding → test → result

SQLite = default harness. PG = the PostgreSQL 16 run. PASSED means the bug was reproduced (the test asserts the faulty behaviour). T1 = `tests/audit_verification/test_cloud_audit.py`, T2 = `tests/audit_verification/test_postgres_cloud_audit.py`.

| ID (sev) | Test | SQLite | PG |
|---|---|---|---|
| PAY-01 (High) – 2A-01 | T1::test_PAY01_late_3ds_charge_on_expired_order_is_unrefundable_and_unreported | PASSED | PASSED |
| PAY-01 (High) – 2B-03 | T1::test_PAY01_customer_cancel_ignores_in_flight_3ds_attempt | PASSED | PASSED |
| PAY-01 (High) – 2A-04 | T1::test_PAY01_second_checkout_session_after_stale_window_double_charges | PASSED | PASSED |
| PAY-02 (Med) | T1::test_PAY02_checkout_session_without_webhook_is_skipped_by_reconciliation | PASSED | PASSED |
| PAY-03 (Med) | T1::test_PAY03_stale_pending_refund_event_regresses_refunded_order_to_paid | PASSED | PASSED |
| GDPR-02 (Med) – structure | T1::test_GDPR02_purge_hashes_bcrypt_inside_one_open_transaction | PASSED | PASSED |
| GDPR-02 (Med) – failure | T2::test_GDPR02_purge_dies_on_idle_in_transaction_timeout | SKIPPED | PASSED |
| DB-01 (Med) – checkout vs checkout | T2::test_DB01_variant_checkout_deadlocks_plain_plus_variant_checkout | SKIPPED | PASSED |
| DB-01 (Med) – checkout vs cancel | T2::test_DB01_variant_checkout_deadlocks_cancel_of_plain_plus_variant_order | SKIPPED | PASSED |
| GDPR-01 (Med) | T1::test_GDPR01_webhook_event_after_erasure_keeps_pii_forever | PASSED | PASSED |
| GDPR-04 (Med) | T1::test_GDPR04_erasure_redacts_address_of_paid_unshipped_order | PASSED | PASSED |
| GDPR-05 (Med) | T1::test_GDPR05_data_export_contains_no_personal_data | PASSED | PASSED |
| GDPR-03 (Med) | T1::test_GDPR03_patch_me_changes_marketing_consent_without_history | PASSED | PASSED |
| AUTH-01 (Med) – 2C-02 | T1::test_AUTH01_login_in_flight_during_password_reset_survives_it | PASSED | PASSED |
| AUTH-01 (Med) – 2C-03 | T1::test_AUTH01_refresh_rotation_in_flight_during_reset_survives_it | PASSED | PASSED |
| AUTH-02 (Med) | T1::test_AUTH02_deletion_reauth_is_an_unthrottled_password_oracle | PASSED | PASSED |
| REG-01 (Med) | T1::test_REG01_registration_without_full_name_never_sends_verification_mail | PASSED | PASSED |
| DATA-01 (Med) | T1::test_DATA01_address_edit_rewrites_delivered_order | PASSED | PASSED |
| 2A-05 (Low) | T1::test_2A05_pending_rows_consume_the_reconciliation_cap | PASSED | PASSED |
| 2A-06 (Low) | T1::test_2A06_pay_answers_paid_when_success_was_not_applied | PASSED | PASSED |
| 2A-07 (Low) | T1::test_2A07_orphaned_processing_attempt_answers_409_forever | PASSED | PASSED |
| 2B-02 (Low) | T1::test_2B02_sync_engine_hook_never_sees_route_stock_updates | PASSED | PASSED |
| 2B-04 (Low) – nested replace | T1::test_2B04_merge_patch_replaces_nested_characteristics | PASSED | PASSED |
| 2B-04 (Low) – variant null | T1::test_2B04_merge_patch_stores_null_in_variant_attributes | PASSED | PASSED |
| 2B-07 (Low) | T1::test_2B07_order_total_is_not_bounded_by_its_numeric_column (SQLite: 201 with total > 99,999,999.99; PG: 500) | PASSED | PASSED |
| 2B-08 (Low) | T2::test_2B08_hard_product_delete_racing_checkout_answers_500 | SKIPPED | PASSED |
| 2B-09 (Low) | T1::test_2B09_guest_cart_merge_drops_lines_silently | PASSED | PASSED |
| 2B-11 (Low) | T1::test_2B11_expiry_sweep_aborts_on_first_error | PASSED | PASSED |
| 2C-12 (Low) | T1::test_2C12_deletion_confirmation_claims_data_already_deleted | PASSED | PASSED |
| OBS-01 (Low) – 2D-02 | T1::test_2D02_OBS01_importing_the_celery_app_configures_no_logging | PASSED | PASSED |
| 2D-03 (Low) | T1::test_2D03_deletion_mail_is_sent_inside_the_open_transaction | PASSED | PASSED |
| 2D-04 (Low) | T1::test_2D04_database_uri_is_not_url_encoded | PASSED | PASSED |
| 2D-07 (Low) | T1::test_2D07_order_email_celery_tasks_are_never_enqueued | PASSED | PASSED |
| 2C-14 (Low) | none | not reproduced | Plausible-impact only (SMTP amplification at 300/min). The route's missing limit is pinned by the AUTH-02 test, which asserts `("POST", "/legal/data-request") not in _ROUTE_LIMITS`. |
| 2D-05 (Low) | none | not reproduced | Needs a black-holed DB host and the kernel TCP timeout, which cannot be made deterministic without network tricks. Confirmed by reading. |
| 2D-06 (Low) | none | not reproduced | A log/`last_error` content issue. Behaviourally it would only re-assert SQLAlchemy's `str(exc)` format. Confirmed by reading. |
| DOC-01 (Low) | none | n/a | Stale comments; no runtime behaviour to test. |

The documented trade-offs (2A-08, 2B-05, 2B-06, 2C-09, 2C-13, 2C-15) and the discarded items were out of scope, as they are not confirmed defects.

## Summary

- SQLite: **29 passed, 4 skipped** (exit 0).
- PostgreSQL 16: **33 passed** (exit 0), stable over 3 extra runs.
- Ruff: **All checks passed** (exit 0).
- Full fast suite: **5243 passed, 113 skipped** in 148 s (exit 0). That is the baseline 5214/109 plus the 29/4 added here.
