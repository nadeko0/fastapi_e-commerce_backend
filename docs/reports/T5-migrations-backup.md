# Stage T5: Database Migrations and Backup/Restore Verification

A backup that has never been restored is merely an expression of optimism. Stage T5 validates the reversibility of Alembic schema migrations on populated databases and confirms that full backup-and-restore drills produce bit-for-bit identical tables, row counts, and checksums.

## Purpose and Exit Criterion

The purpose of Stage T5 was to verify the reliability, reversibility, and data preservation of Alembic database schema migrations, and to prove the integrity of disaster recovery procedures using PostgreSQL backup and restore tools.

Exit criteria:
- Clean forward and backward migrations on an empty database: upgrade to head, downgrade to base, and upgrade back to head without errors, producing a schema identical to live.
- Zero errors during stepwise downgrade of all 15 migration revisions on a populated database.
- Full backup (`pg_dump -Fc`) and restore (`pg_restore --exit-on-error`) fidelity: exact match of row counts, table MD5 checksums, sequence states, and role permissions.
- The application stack boots cleanly and passes smoke and E2E checks when pointed at the restored database.

## Environment

- **Host:** WSL2 staging environment (commit `3af3e68`, tag `v4.0.0`).
- **PostgreSQL Version:** 15.19 (`postgres:15-alpine` digest `sha256:f7d23353...`).
- **Backup & Restore Tooling:** `pg_dump`, `pg_restore`, and `psql` version 15.19 executed from within the container.
- **Migration Runner:** Alembic executed via the stack migration container with `DATABASE_URI` configured.
- **Database Roles:** `ecom_owner` (superuser/schema owner) and `ecommerce_app` (runtime application role; DML only, no DDL permissions).
- **Starting Live Dataset:** 63 users, 41 orders, 41 order items, 43 payments, 6 refunds, 4 disputes, 209 webhook events (total database size: 10071 kB).

## Method and Commands

1. **Schema Reversibility on Empty Database:**
   ```bash
   alembic upgrade head
   alembic downgrade base
   alembic upgrade head
   alembic check
   ```
   Schema dumps before and after were compared using automated catalog inspection scripts (`catalog.sql`).
2. **Stepwise Downgrades with Populated Data:**
   A full clone of the live database was created (`ecom_t5_data`). Stepwise `alembic downgrade -1` was executed across all 15 revisions sequentially, recording row counts and column transformations at each step.
3. **Backup and Restore Fidelity Verification:**
   ```bash
   pg_dump -Fc -f <work dir>/ecommerce-final.dump ecommerce
   pg_restore -d ecom_t5_restore --exit-on-error --no-owner <work dir>/ecommerce-final.dump
   ```
   Row counts (`counts.sql`) and whole-row MD5 checksums (`keysum.sql`) were executed across all 13 tables.
4. **Application Validation on Restored Database:**
   Reconfigured the application stack to target `POSTGRES_DB=ecom_t5_restore` via compose override and executed smoke and E2E suites.

## Results

### Step 1: Empty Database Migrations

| Migration Step | Duration | Return Code | Schema Comparison | Status |
|---|---|---|---|---|
| Upgrade head (15 revisions) | 3.68 s | 0 | 267 catalog items created | PASS |
| Downgrade base | 3.48 s | 0 | All tables, enums, sequences removed | PASS |
| Second upgrade head | 3.61 s | 0 | Identical diff to first upgrade and live database | PASS |
| Model drift check (`alembic check`) | 2.55 s | 0 | "No new upgrade operations detected" | PASS |
| Offline SQL generation (`upgrade head --sql`) | - | 1 | AttributeError in migration `3f6c2a9d1b7e` | FAIL (T5-1) |

### Step 2: Stepwise Migrations with Live Data

All 15 downgrade steps succeeded without database errors (durations ranged from 2.3 s to 5.2 s per step). Downgrades dropped specific tables and columns by design:
- Revision `d4b7e1f93a62`: dropped `payment_discrepancies` (0 rows).
- Revision `c2d8f4a6b913`: dropped `payment_disputes` (4 rows).
- Revision `a7c3e5b19d42`: dropped `payment_refunds` (6 rows).
- Revision `5fe510d5d45e`: dropped `product_variants` (4 rows).
- Revision `b6d7b96c6d17`: dropped `order_items.variant_id`.
- Revision `d4e9a1c7b265`: dropped webhook payload, status, attempt columns.
- Revision `b6f1c8d2a047`: dropped idempotency fingerprints from orders and payments.
- Revision `482750d08d54`: dropped all core tables.

Partial round trips on a restored database:
- Downgrade -1 (revision `c5e2a8f4b196`) and upgrade back: row counts and MD5 checksums across all 13 tables matched 100% identically (2.9 s down, 4.2 s up).

### Step 3: Backup and Restore Verification

| Verification Aspect | Measurement / Value | Status |
|---|---|---|
| `pg_dump -Fc` execution | 0.57 s (dump size: 121 kB) | PASS |
| `pg_restore --exit-on-error` execution | 1.50 s (0 errors, 0 warnings) | PASS |
| Table row counts (all 13 tables) | 13 of 13 tables match live source exactly | PASS |
| Whole-row MD5 checksums | 13 of 13 tables match live source exactly | PASS |
| Sequence values and incrementors | 100% match with live database | PASS |
| Database role permissions & ACLs | Application role has DML (SELECT/INSERT/UPDATE/DELETE) and 0 CREATE | PASS |

### Step 4: Application Execution on Restored Database

- **Smoke Suite:** 24 of 24 passed (initial failure on admin login was caused by the test harness targeting the live database for admin promotion; resolved upon pointing harness to restored database).
- **E2E Subset (scenarios a, b, e, f, i):** 5 passed, 0 failed (22.97 s). 15 Stripe webhooks processed with status 200.
- **Reversion to Live Database:** Stack cleanly reverted to original database; smoke suite returned 24 of 24 passed.

## Findings and Operational Notes

- **T5-1 (Low Severity / Fixed in v4.0.1): Alembic offline SQL generation failure.**
  - *Detail:* Executing `alembic upgrade head --sql` on release v4.0.0 failed because migration `3f6c2a9d1b7e` invoked `op.get_bind().execute().fetchall()` to check data, which returns `None` in offline mode.
  - *Remediation:* Replaced Python data inspection with inline PostgreSQL `DO $$ ... $$` blocks. Shipped in v4.0.1 (fix L2).
- **T5-2 (Low Severity / Operational Risk): Data loss during migration downgrades.**
  - *Detail:* Downgrades are destructive by design (tables and columns dropped without confirmation).
  - *Operational Rule:* Mandatory policy established requiring a verified `pg_dump` backup prior to executing any production schema migration.
- **T5-4 (Info / Gap): Absence of Continuous Archiving (PITR).**
  - *Detail:* The stack configuration ran with `wal_level=replica` and `archive_mode=off`, meaning only point-in-time snapshot dumps were possible.
  - *Documentation:* Documented configuration requirements for PostgreSQL WAL continuous archiving (`archive_mode=on`, `archive_command`, and `pg_basebackup`).

## Limitations

- Point-in-time recovery (PITR) was not tested because WAL archiving was disabled in the default compose deployment.

---

See also: [fixes-4.0.x.md](fixes-4.0.x.md), [final-test-campaign.md](final-test-campaign.md), [../OPERATIONS.md](../OPERATIONS.md).
