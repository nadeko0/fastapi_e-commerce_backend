# 03C: Staff-Level Technical Interview Defense Guide

Target: `fastapi_e-commerce_backend` v4.0.3. Author of this guide: Subagent 3C. Date: 2026-10-26.

**Inputs.** This guide uses the confirmed findings in `.cloud_audit/02a_payments.md`, `02b_catalog_orders.md`, `02c_auth_gdpr.md` and `02d_infra_and_comments.md`. Finding IDs such as 2A-01 point to those reports. I re-read every `file:line` below against HEAD. Line numbers are for v4.0.3 as checked out.

**How to use it.** Each question has five parts:
1. The question, with likely follow-ups.
2. Why the interviewer asks it.
3. A model answer (150-300 words).
4. Evidence: the `file:line` references.
5. Red flags to avoid.

The model answers are deliberately honest. A Staff panel is not testing whether the system is perfect. It tests three things:
- Do you know exactly where it is not perfect?
- Can you explain why you made each trade-off?
- Do you already have the fix in mind?

Name the weakness before they find it, and you keep control of the conversation.

---

## Q1. Idempotency: why a database reservation and not Redis, and what exactly is in your fingerprint?

**Question.** "Walk me through `Idempotency-Key` on `POST /orders` and `/pay`. Why did you reserve keys in Postgres and not with a Redis `SET NX`? What goes into the fingerprint?"
- *Follow-up 1:* "Your ARCHITECTURE.md says reuse returns 409 and that the fingerprint covers user id, URL and body. Is that what the code does?"
- *Follow-up 2:* "What happens to a key whose request crashed halfway through?"

**Why they ask.** Idempotency is the most common place for "we have it" to mean "we have a cache". They want to know three things: whether the key is reserved atomically with the side effect, whether the stored response can disagree with the committed state, and whether you know the edge cases (a stuck key, a key's expiry, refunds).

**Model answer.**
I reserve the key in the same database transaction as the side effect, so the key and the effect commit or roll back together.

*Checkout (`POST /orders`).* The order row carries the key, protected by a unique partial index on `(user_id, idempotency_key)`. I `flush()` that INSERT before the first stock UPDATE. A concurrent twin blocks on the index. When the first request commits, the twin replays it; when the first rolls back, the twin proceeds.

*Why not Redis.* A Redis `SET NX` lives outside the transaction. If the DB commit then fails, I need a compensating delete. On eviction or failover, the reservation is simply lost.

*Payments.* I cannot hold a transaction open across a Stripe HTTP call. So I commit a `processing` Payment row (unique `(order_id, key)`) first, then call Stripe with a deterministic provider key namespaced as `pay-order-{id}:{key}`. A replay is rebuilt from committed rows. There is no separate response cache, so a cached response can never disagree with what was committed.

*Fingerprint.* It is SHA-256 of the canonical JSON of the *validated* model, so `19.9` and `19.90` are the same request. For checkout it covers only `shipping_address_id`, because the cart is server-side state. Reusing a key with a different payload returns **422**, not 409. ARCHITECTURE.md §6 is wrong on that and on what the fingerprint contains, and I'd fix the doc.

*Gaps I know about:*
- A `processing` row left behind by a crash answers 409 "retry later" forever (2A-07). The fix is to treat stale `processing` like `unknown` and re-send with the same deterministic Stripe keys.
- Refund keys are only forwarded to Stripe, which prunes them after 24 hours (2A-08). The fix is a local `(order, key, fingerprint) -> refund_id` row.

**Evidence.**
- `app/api/v1/orders.py:203-209`: fingerprint and pre-check. `:257-278`: flush, IntegrityError, replay.
- `app/models/order.py:68-73`: unique partial index. `app/models/payment.py:76-78`: unique `(order_id, idempotency_key)`.
- `app/core/idempotency.py:36-58`: canonical fingerprint; the reuse answer is 422 at `:55`.
- `app/api/v1/orders.py:589-599`: provider key namespacing. `:1124-1149`: `processing` row committed before Stripe, plus the race replay. `:684-705`: 409 for `processing`.
- `app/tasks.py:700-743`: order keys expire after 24 h; payment keys never expire (`idempotency.py:19-22`).
- `docs/ARCHITECTURE.md:196-200` makes the inaccurate 409 and "user ID, URL" claims.
- `docs/LIMITATIONS.md:48-50` covers the refund key.

**Red flags.**
- "Redis is atomic, so SETNX is enough." This ignores transactional coupling and eviction.
- Claiming a stored response cache.
- Not knowing that the fingerprint uses validated values rather than raw bytes.
- Defending the doc's 409 claim.

---

## Q2. Webhooks: at-least-once delivery, duplicates, and out-of-order events

**Question.** "Stripe delivers at least once and in no particular order. How do you make event processing exactly-once in effect, and what happens when a stale event arrives after a newer one?"
- *Follow-up 1:* "Show me a case where event order changes the final state."
- *Follow-up 2:* "Why apply the payload snapshot instead of fetching the object from Stripe?"

**Why they ask.** Ordering is the classic webhook bug. Deduplicating on `event_id` is table stakes. What they want to know is whether your state transitions are monotonic.

**Model answer.**
*Pipeline (store, then process):*
- The route verifies the HMAC on the raw body, INSERTs the event into `webhook_events` (unique `event_id`), publishes a Celery task, and answers 200.
- If the broker is down, the publish fails softly. The event stays `received`, and a sweeper republishes events that are stuck for more than 20 minutes.
- The worker claims the row with `FOR UPDATE SKIP LOCKED`. It marks the event `processed` in the *same transaction* as the state change, so "processed" can never be committed without its effect.
- On a failure, the attempt is counted in a fresh transaction with exponential backoff. After 10 attempts the event becomes `failed`, and an admin can replay it.

That gives exactly-once *effect* on top of at-least-once delivery.

*Ordering for payments is mostly monotonic:*
- A failure that arrives after a success is ignored.
- A failure never moves PAID or REFUNDED to FAILED.
- A success on a cancelled or already-paid order is flagged for manual review rather than applied.
- An event that overtakes its own `/pay` request (the row still holds the `pending-` placeholder) raises `PaymentAttemptNotRecorded` and is retried.

*The gap: refunds are not monotonic (2A-03).* `_apply_refund_object` only treats `failed`/`canceled` as final. A stale `refund.created` with `status: pending` processed after `succeeded` overwrites it. `_settle_refunds` then flips a fully refunded order back to PAID, and it becomes fulfillable again.

*Fixes:*
- Short term: a status rank, so a refund in `succeeded` only moves to `failed`/`canceled`.
- Long term: treat `refund.*` and `charge.dispute.*` events as triggers. Re-fetch the object from Stripe and apply its live state.

I store `created` with each event but don't use it for ordering. It has only one-second resolution, so it can't be the ordering key anyway.

**Evidence.**
- `app/api/v1/orders.py:1864-1880`: verify signature. `:1903-1915`: store and enqueue.
- `app/services/stripe_events.py:438-459`: dedupe. `:462-483`: soft publish. `:531-541`: SKIP LOCKED claim. `:544-577`: processed mark committed with the effect. `:580-610`: backoff and failed state. `:613-652`: sweeper.
- `app/api/v1/orders.py:810-840`: monotonic failure. `:761-772`: flag instead of apply.
- `app/services/stripe_events.py:49-90`: the overtaking event.
- The bug: `app/services/stripe_events.py:253-257` and `app/api/v1/orders.py:1795-1804`.
- Settings: `app/core/config.py:442-451`.

**Red flags.**
- "We dedupe on event id, so ordering doesn't matter."
- Claiming Stripe guarantees order.
- Saying `created` timestamps solve ordering.
- Not knowing that the processed mark and the effect share a transaction.

---

## Q3. Order expiry versus a payment that is still live

**Question.** "An unpaid order expires after 30 minutes and its stock is released. What if the customer is on the Stripe Checkout page, or in a 3DS challenge, at minute 31?"
- *Follow-up 1:* "Who expires the Checkout Session, and when?"
- *Follow-up 2:* "Two browser tabs, each with its own key. Can I be charged twice?"

**Why they ask.** It tests whether you treat the payment provider as a *concurrent writer* with its own lifecycle, which is the hardest part of payment integration.

**Model answer.**
This is the most serious weakness, and I'd raise it myself (2A-01 / 2B-03, High).

*What happens today:*
- `expire_unpaid_orders` selects orders only by status, payment status and age. It never checks for an in-flight attempt.
- The Checkout Session is created without `expires_at`, so Stripe keeps it payable for 24 hours, while the order lives 30 minutes.
- `expire_checkout_session` exists in both providers, but nothing calls it.

So the order is cancelled, the stock is resold, and the customer can still pay. The late success is recorded but flagged, not applied. That's correct as a last-resort guard, but:
- `/refund` refuses the order, because it is not PAID and because the charge is flagged.
- Reconciliation sees "succeeded = succeeded" and records nothing.
- The only signal is a WARNING log line.

The existing test pins the "flag" outcome as intended. Nobody asserted that the money could be returned.

*The two-tab case (2A-04).* `_guard_no_attempt_in_flight` blocks a second attempt only for 5 minutes (`PAYMENT_ATTEMPT_STALE_AFTER_MINUTES`). After that, two sessions can both be paid. The second charge is flagged and, again, can't be refunded through the app.

*Fix, in order:*
1. The sweep (and customer cancel) skips orders with a non-stale attempt in flight.
2. Sessions get `expires_at` (Stripe's minimum is 30 minutes) aligned with the reservation window.
3. On any cancel, call `expire_checkout_session` and cancel `requires_action` intents. If Stripe says the session is already complete, don't cancel.
4. Auto-refund flagged "cancelled / already paid" charges with a deterministic key such as `autorefund-payment-{id}`, and add an admin queue for `requires_manual_review`.

**Evidence.**
- `app/tasks.py:479-499`: the expiry candidate query ignores attempts.
- `app/services/payment/live_stripe_provider.py:347-372`: no `expires_at`. `base.py:138` and `live_stripe_provider.py:399`: `expire_checkout_session`, never called.
- `app/api/v1/orders.py:765-772`: flag. `:973-988`: flag plus a `logger.warning` only.
- `app/api/v1/orders.py:1540-1562`: refund refuses non-PAID and flagged payments.
- `app/api/v1/orders.py:898-937` and `app/core/config.py:414,421`: the 5-minute guard and the 30-minute expiry.
- Pinned behaviour: `tests/integration/test_payment_chain.py:426` and `tests/integration/test_order_expiry.py:327`.

**Red flags.**
- "The webhook handles it."
- "Stripe sessions expire quickly." They don't by default.
- Treating "flagged for manual review" as a resolution when there is no queue and no refund path.

---

## Q4. Reconciliation: design and blind spots

**Question.** "You have a daily reconciliation job. What does it compare, what can it *not* see, and would it have caught the Q3 bug?"
- *Follow-up 1:* "What happens at 1,000 payments a day?"
- *Follow-up 2:* "Why report and not repair?"

**Why they ask.** Reconciliation is the safety net for every other bug. Its blind spots are the bugs nobody will ever find.

**Model answer.**
*Design:*
- Every day at 03:15, it takes payments in a 7-day window that are at least 60 minutes old.
- It walks them newest-first, capped at 200 per run.
- For each one it commits its read transaction before calling Stripe, so no pooled connection waits on HTTP.
- It compares status, amount, currency, refund totals and each refund individually.
- Discrepancies are idempotent rows (unique `(payment, kind, subject)`) that reopen and resolve automatically.

It deliberately only *reports*. Setting `requires_manual_review` on the charge that paid would make it unrefundable, and auto-repair against a possibly stale snapshot is riskier than a human queue.

*Blind spots (all confirmed):*
1. **Checkout payments whose completion webhook was lost are skipped (2A-02).** Until `checkout.session.completed` fills in the real PaymentIntent id, the row keeps a `pending-cs-` placeholder, and reconciliation `continue`s on any `pending-` prefix. So the flow that depends *most* on webhooks is the one reconciliation never checks. Fix: for rows with a `checkout_session_id`, call `retrieve_checkout_session`, which exists and is unused.
2. **The cap (2A-05).** It is the newest 200, the skipped rows count against it, and it runs under a 240 s soft time limit at about 2 Stripe calls per payment. Above 200/day, older payments are never checked. Fix: a persisted `reconciled_at` cursor (least recently checked first), filter out skip rows in SQL, and fan out per chunk.
3. **It only iterates local rows.** A charge with no local row is never discovered. Fix: page through Stripe's PaymentIntents and Checkout Sessions by `created`.
4. **It would not catch Q3.** Ours = succeeded, theirs = succeeded, so there's no difference.

**Evidence.**
- `app/services/reconciliation.py:1-31`: purpose, report-only, no transaction during provider calls.
- `app/services/reconciliation.py:257-316`: window, cap at `:277-285`, skip at `:289-291`, commit before Stripe at `:293-294`.
- `app/services/reconciliation.py:161-175`: status/amount compare.
- `app/core/config.py:463-466`: window and cap. `app/tasks.py:139`: soft limit. `app/tasks.py:676-679`: schedule.
- `app/services/stripe_events.py:385-389`: the placeholder is replaced only by the webhook.
- `app/services/payment/live_stripe_provider.py:384`: `retrieve_checkout_session` is unused.

**Red flags.**
- "Reconciliation catches anything the webhooks miss."
- Not knowing that it never enumerates the provider side.
- Proposing auto-repair without discussing stale reads.

---

## Q5. Stock locking, lock ordering, and the FK-induced deadlock

**Question.** "How do you prevent oversell, and how do you prevent deadlocks between concurrent checkouts and cancellations?"
- *Follow-up 1:* "Which locks does Postgres take that your code doesn't ask for?"
- *Follow-up 2:* "When a deadlock does happen, what does the client and the on-call engineer see?"

**Why they ask.** Sorted `FOR UPDATE` is the textbook answer. Staff engineers know that foreign keys take implicit `FOR KEY SHARE` locks, which breaks the textbook answer.

**Model answer.**
*Oversell* is prevented by two independent layers:
- A conditional decrement, `UPDATE ... SET stock = stock - n WHERE stock >= n`, with `rowcount == 0` meaning 409.
- DB `CHECK (stock_quantity >= 0)` as a backstop.

*Deadlocks.* I take all row locks up front in a fixed order (products by id, then variants by id) in `lock_stock_rows`. Cancel, admin cancel and expiry all lock order, then stock. Payments lock event, then order, then payment.

*Where it breaks (2B-01, reproduced on PG 16):*
- `order_items` rows are only `db.add`-ed. With `autoflush=False`, they are INSERTed at `commit()`, after the stock locks.
- Each INSERT fires Postgres's RI check, `SELECT 1 FROM products WHERE id=$1 FOR KEY SHARE`, *including for variant lines*, whose product I never pre-locked.
- `FOR KEY SHARE` conflicts with my `FOR UPDATE`. So the real order is products, then variants, then products again.
- A cart `[V]` racing a cart `[P, V]` (or a cancel of an order containing both) deadlocks.

Psycopg's `DeadlockDetected` is an `OperationalError`, which my error mapper classifies as **"database unavailable" (503)**. So the incident looks like an outage. In the expiry job it aborts the whole sweep (2B-11).

*Fix (verified to resolve the repro):*
- `with_for_update(key_share=True)`, i.e. `FOR NO KEY UPDATE`. It still serialises stock writers but doesn't conflict with FK checks.
- Apply the same change to the admin `If-Match` locks.
- Map 40P01 to a distinct, retryable problem type.

*Why the tests missed it (2B-02).* The PG lock-order tests attach their barrier hook to the *sync* engine, while the routes run on the async engine. The hook never fires, so the tests never forced the interleaving they claim to.

*Scale caveat.* One hot SKU serialises every checkout on that product row. For a flash sale I'd move to a reservation table or sharded counters.

**Evidence.**
- `app/services/orders.py:64-91`: `lock_stock_rows` with `with_for_update()`. `app/services/orders.py:117`: cancel path.
- `app/api/v1/orders.py:284-286`: locks. `:331-355`: conditional decrement. `:357-369`: items added, INSERTed at commit.
- `app/core/database.py:84,109`: `autoflush=False`. `app/core/database.py:140`: OperationalError mapped to 503.
- `app/api/v1/admin.py:257,810`: admin FU locks.
- Test gap: `tests/integration/test_postgres_only.py:172` (`event.listen(engine, ...)`) against `tests/conftest.py:220-225` (separate `async_engine`).

**Red flags.**
- "Sorted locks make deadlocks impossible."
- Not knowing that FKs take `FOR KEY SHARE`.
- "SQLite tests cover it."
- Calling a 503 on deadlock "correct".

---

## Q6. Why both a sync and an async SQLAlchemy engine?

**Question.** "Your routes are async, but your domain code is sync and runs through `run_sync`. Why? What does that cost you?"
- *Follow-up 1:* "How many Postgres connections can one API worker open?"
- *Follow-up 2:* "Where do you call blocking code from the event loop?"

**Why they ask.** Mixed sync and async code is a common source of event-loop stalls, `MissingGreenlet` errors and pool exhaustion. They want to see that it was a deliberate choice.

**Model answer.**
*The decision.* Celery workers are sync. Payment settlement, restock and event application must be *one* implementation, shared by HTTP and the worker. Duplicating it into async and sync versions guarantees divergence.

*How it works:*
- Routes use `AsyncSession` (psycopg 3 in async mode), so waiting on the DB yields to the loop.
- Shared logic is written against `Session` and called through `db.run_sync(...)`. That runs it in a greenlet on the async connection: same transaction, no extra connection.
- The Stripe SDK is blocking, so every provider call goes through `run_in_threadpool`, and always *after* a commit, so no connection idles in a transaction during HTTP.

*Costs (being honest):*
- The sync `engine` in the API process now serves only the admin reconciliation thread. Each worker still has two pools of 15, so 4 workers can open up to 120 connections; compose sets `max_connections=200`. I'd drop the API-side sync pool or set it to `pool_size=1`.
- Celery has a third engine whose `SessionLocal` lacks `expire_on_commit=False`. `process_stored_event` has to set it by hand, which is a subtle divergence.
- `overload.py` and PERFORMANCE.md still say "an authenticated request holds two sync connections, ceiling = pool/2". That stopped being true when auth moved onto the shared `DbSession`, and it makes operators under-provision by half.
- Async forces eager loading everywhere (`selectinload`), or serialisation raises `MissingGreenlet`.
- The anyio threadpool (about 40 threads) is shared between bcrypt and Stripe calls. A bcrypt-heavy burst slows payments.

**Evidence.**
- `app/core/database.py:81-115`: both engines, with the rationale at `:89-104`.
- `docs/ARCHITECTURE.md:166-170`.
- `run_sync` call sites: `app/api/v1/orders.py:284`, `:1292`, `:1903`.
- `app/api/v1/orders.py:1151-1170`: threadpool and commit before Stripe. `:1581-1587`: same for refunds.
- `app/tasks.py:152-162`: Celery engine, no `expire_on_commit=False`. `app/services/stripe_events.py:561`: set manually.
- Stale guidance: `app/core/overload.py:20-22` and `docs/PERFORMANCE.md:57`. `docker-compose.yml:170-174`: `max_connections`.

**Red flags.**
- "Async is faster."
- Not knowing that `run_sync` uses greenlets on the same connection.
- Claiming a transaction is held across Stripe calls, or not knowing whether it is.

---

## Q7. Celery: `acks_late`, at-least-once delivery, and idempotent tasks

**Question.** "You set `acks_late` and `reject_on_worker_lost`. So tasks can run twice. Show me each task is safe to run twice, and tell me which work is *not* durable."
- *Follow-up 1:* "What's your visibility timeout, and what happens to a task longer than that?"
- *Follow-up 2:* "Where do customer emails go if the API process crashes after commit?"

**Why they ask.** `acks_late` turns at-most-once into at-least-once. That is only correct if every task body is idempotent.

**Model answer.**
*Settings:*
- `task_acks_late`, `task_reject_on_worker_lost` and `prefetch_multiplier=1`.
- `worker_cancel_long_running_tasks_on_connection_loss=True`, so a redelivered copy doesn't run in parallel with a stale one.
- Redis `visibility_timeout=3600`, which is above `task_time_limit=300`, and the retry countdown is capped at 3000 s by config validation.

*Idempotency per task:*
- **Stripe events:** SKIP LOCKED claim plus status check, so a duplicate is a no-op. `_record_failure` refuses to retry an event already committed as processed.
- **Expiry:** per-order `FOR UPDATE SKIP LOCKED` with a re-check under the lock.
- **Purges:** keyset batches and an "already anonymised" marker.
- **Reconciliation:** upserted discrepancy rows.

*What isn't durable or safe:*
- **Order emails** go through FastAPI `BackgroundTasks` or `_AfterCommit`. The Celery email tasks with retry and dedup exist but are never enqueued (2D-07), so a crash after commit loses the email. The fix is a transactional outbox (row in the same transaction, relay task).
- **The expiry sweep** has no per-order `try`. One deadlock (Q5) aborts the rest until the next beat (2B-11).
- **The GDPR purge** fails with `IdleInTransactionSessionTimeout`, raised as `InternalError`, which no `autoretry_for` covers (Q10).
- **Beat** must be a singleton. Two beats double-schedule. The tasks tolerate it, but reconciliation would double the Stripe calls.

**Evidence.**
- `app/tasks.py:84-140`: Celery config. `app/core/config.py:444`: `le=3000`.
- `app/services/stripe_events.py:531-541`, `:580-592`.
- `app/tasks.py:492-499`: expiry lock and re-check, with no `try` around the loop at `:492-526`.
- `app/tasks.py:547-565`: `process_stripe_event`.
- `app/services/stripe_events.py:508-528`: `_AfterCommit`. `app/api/v1/orders.py:396-400`, `:781-785`: `BackgroundTasks` email.
- `docs/RESILIENCE.md:15`: chaos result, 50/50 events, 0 duplicates.

**Red flags.**
- "`acks_late` gives exactly-once."
- Not knowing what happens when a task outlives `visibility_timeout`.
- Claiming email delivery is durable.

---

## Q8. Refresh token rotation and reuse detection under races

**Question.** "Explain the rotation and reuse-detection protocol. What happens with two tabs refreshing at once, and what happens if a password reset lands mid-refresh?"
- *Follow-up 1:* "Is there an absolute session lifetime?"
- *Follow-up 2:* "What if Redis loses its data?"

**Why they ask.** Token families are easy to describe and hard to get right under concurrency. They're probing whether you've thought about the races, not just the happy path.

**Model answer.**
*Protocol:*
- Each refresh token has a `jti` and a `family`. State lives in Redis (`refresh_active:{jti}`, plus family and user sets).
- On rotation I check the per-user watermark (`is_token_superseded`). I *register* the new token before *consuming* the old one, so a Redis failure between the two leaves the client holding a valid token and a 503, never a spent one.
- Consuming is GET and DEL in one MULTI/EXEC, so only one caller wins. A second presentation of a spent `jti` revokes the whole family.
- Redis errors fail closed with 503, not 401, so clients don't discard good tokens.

*Races I know about:*
1. **Benign double refresh (2C-09, documented in LIMITATIONS).** Two tabs or a mobile retry look like theft, the family is revoked, and the user must log in again. The fix is the OAuth-style grace window: accept the previous `jti` for 10-30 s and return the same successor.
2. **A login in flight survives a reset (2C-02).** Login reads the hash, closes the session, runs bcrypt (about 250 ms), then mints tokens without re-checking the hash. A reset committing during that window produces tokens with `iat` later than the watermark. Fix: re-read the hash after bcrypt; on reset, also revoke all families and set a second watermark after commit.
3. **Rotation racing a reset (2C-03).** The watermark is read before minting. Fix: do the check-and-register atomically in one Lua script using Redis `TIME`.
4. **No absolute lifetime.** Each rotation issues a fresh 7-day `exp`, so a chain never ends. Fix: store `family_started_at` and refuse rotation after N days.

*Redis durability.* Compose runs `noeviction` and AOF. Losing Redis data would log everyone out, which fails safe.

**Evidence.**
- `app/core/security.py:487-552`: rotation; register-before-consume at `:530-537`, family revoke at `:539-544`.
- `app/services/redis_async.py:405-422`: MULTI/EXEC consume; errors raise `TokenStateUnavailable`.
- `app/core/security.py:205-216`: float `iat`. `:233-250`: a fresh `exp` on every rotation.
- `app/api/v1/users.py:332-359`: login reads, closes, then runs bcrypt. `:386-388`: mints with no re-check. `:1045-1074`: reset watermark, then conditional hash update.
- `app/api/v1/users.py:437-440`: 503 on Redis error.
- `docs/LIMITATIONS.md:42-44`. `docker-compose.yml:210-241`: noeviction and AOF.
- A latent hazard: the dead sync twin returns `None` on a Redis error (`app/services/redis.py:462-473`), which a caller would read as a replay. Delete it.

**Red flags.**
- "JWTs are stateless, so no races."
- "Reuse detection is always theft."
- Not knowing whether reset revokes families (it doesn't).

---

## Q9. A Redis outage: fail-open or fail-closed, per feature

**Question.** "Redis goes down at peak. Go feature by feature: what fails open, what fails closed, and why?"
- *Follow-up 1:* "Is Redis a single point of failure for revenue?"
- *Follow-up 2:* "Your broker is the same Redis. What happens to webhooks?"

**Why they ask.** They want to hear a deliberate policy rather than "we catch exceptions", and they want to know you understand the blast radius.

**Model answer.**
The rule: **security state fails closed, availability aids fail open.** Chaos test T8 confirmed it.
- **Token blacklist and watermark:** closed, 503 with `Retry-After`. Failing open would honour logged-out tokens. 503 rather than 401, so clients keep their credentials.
- **Refresh rotation:** closed (503).
- **Login failure counter:** closed (503). Failing open would give unlimited password guessing during an outage.
- **General rate limiter:** open. A limiter that can't count must not take the shop down.
- **Product cache:** falls through to Postgres.
- **Cart writes:** 503 "nothing was changed".
- **Webhooks:** stored in Postgres and acknowledged. The publish fails softly, and the sweeper republishes after recovery.
- **Email dedup marker:** open. The worst case is a duplicate email.

*Honest caveats:*
- **The cart lives only in Redis, and so does every authenticated request's token check.** Redis is effectively on the revenue path: during the outage nobody can check out (RESILIENCE: authenticated routes 503, catalog reads 200).
- `get_cart` returns `None` on a Redis error. If Redis blips *between* auth and the cart read, checkout answers **400 "Cart is empty"**, which is misleading. It should be 503.
- Broker, cache, rate limits and token state share one instance. A flood of rate-limit counters or cart keys under `noeviction` could start refusing token writes.

*What I'd change:*
- Split Redis by role: broker, cache, and auth state on a replicated or Sentinel setup.
- Make `get_cart` raise instead of returning `None`.
- Consider a short-TTL in-process cache of "not revoked" for access tokens to ride out blips. That is a conscious, bounded fail-open.

**Evidence.**
- `docs/RESILIENCE.md:11-12`: measured matrix. `:71-80`: 503 vs 401.
- `app/core/security.py:344-354`: fail closed.
- `app/core/rate_limit.py:37-40`, `:320-326`: fail open. `:414-420`: login counter fails closed.
- `app/api/v1/cart.py:308-316`: cart 503.
- `app/services/redis_async.py:218-222`: `get_cart` returns `None` on error. `app/api/v1/orders.py:211-216`: that becomes 400 "Cart is empty".
- `app/services/stripe_events.py:462-483`: soft publish.
- `app/tasks.py:72-80`: broker on the same Redis.

**Red flags.**
- "Redis is just a cache."
- A blanket "we fail open" or "we fail closed".
- Not knowing that the cart is Redis-only.

---

## Q10. GDPR erasure versus financial retention, audit logs, and webhook PII

**Question.** "A user invokes Article 17. What do you delete, what do you keep, and how do you know PII doesn't reappear later?"
- *Follow-up 1:* "Does the purge actually complete on production settings?"
- *Follow-up 2:* "What if they have an open dispute or an unshipped paid order?"

**Why they ask.** Erasure conflicts with tax retention, chargeback evidence and append-only logs. They want to see that you understand the legal trade-off and that the implementation actually runs.

**Model answer.**
*Design.* I anonymise in place and never hard-delete. Invoices must be kept (Art. 17(3)(b)), and `User.orders` has an ORM `delete-orphan` cascade that would wipe them.
- **Scrubbed:** email (replaced by `deleted-user-{id}@deleted.invalid`), name, phone, the password hash (replaced with an unusable one), consent IP and UA, and address fields. Country is kept for tax.
- **Kept:** orders, payments, refunds, disputes.
- **Stored Stripe payloads:** redacted with an allow-list, so a field Stripe adds later is dropped too.

*Confirmed problems:*
1. **The purge cannot finish on Postgres (2D-01, High).** Each batch of 100 runs 100 bcrypt hashes *inside an open transaction*, with no statement sent in between. At 12 rounds that is about 29 s, against `idle_in_transaction_session_timeout=30s`; at 13 rounds it is killed. The `InternalError` isn't classified or retried, so the same batch fails every night and no erasure completes. Fix: hash once per batch, before the transaction opens. Tests miss it because they use SQLite and `BCRYPT_ROUNDS=4`.
2. **PII reappears (2C-01).** Redaction runs once, at purge time. Events arriving afterwards (refunds; disputes up to 120 days later) are stored with `billing_details`, and the purge excludes already-anonymised users. Fix: redact at ingest or in the worker when the owner is erased.
3. **No legal-hold check (2C-10).** Erasure after one day redacts the address of paid, unshipped or disputed orders. Orders reference a mutable address row with no snapshot (2C-06). Fix: defer erasure while obligations are open, and snapshot shipping fields onto orders.
4. Smaller items:
   - SMTP is sent inside the deletion transaction (2D-03).
   - SQL parameters can reach `last_error` and logs (2D-06).
   - The Art. 15/20 export is a stub that emails no data (2C-11).

**Evidence.**
- `app/tasks.py:292-344`: purge rationale and selector; anonymised users excluded at `:332-342`.
- `app/tasks.py:286-290`: the comment's "~25 s" is wrong. `:352-372`: bcrypt in the loop. `app/core/database.py:56-63`: 30 s timeout.
- `app/services/stripe_events.py:655-672`: allow-list. `:710`: `redact_users_events`. `app/tasks.py:363`: called once.
- `app/api/v1/orders.py:1903-1909`: full payload stored.
- `app/services/gdpr.py:119-147`: no hold checks, SMTP before commit, and a stale "hard delete" comment at `:133-136`. `app/core/config.py:343`: 1-day grace.
- `app/models/order.py:39`: address FK, no snapshot. `app/services/stripe_events.py:594`: `last_error`.
- `app/services/gdpr.py:100-109`: export stub.

**Red flags.**
- "We delete the user row."
- "Logs are out of scope."
- Not knowing that the 30 s idle timeout applies to the Celery engine too.
- Claiming the export is implemented.

---

## Q11. ETag, If-Match, and JSON Merge Patch semantics

**Question.** "Your admin product API supports ETag, If-Match and `application/merge-patch+json`. What does a PATCH of a nested object do? Why is If-Match optional?"
- *Follow-up 1:* "Is the ETag useful on a product that is selling?"
- *Follow-up 2:* "How is the precondition check atomic with the write?"

**Why they ask.** Many APIs claim RFC compliance. They want to see whether you know where you deviate and what that causes in operations.

**Model answer.**
*ETag:*
- A strong validator: SHA-256 of the exact response JSON, identical whether built from the DB or the Redis copy.
- `If-None-Match` uses weak comparison and gives a 304. `If-Match` uses strong comparison, so a weak tag never matches.
- With `If-Match`, I take `SELECT ... FOR UPDATE` on the product row, then compare, then write, so check and write are atomic.

*Deviations:*
1. **Merge Patch is top-level only (2B-04).** PATCH and PUT share one handler that does `setattr` per top-level key. So `{"characteristics": {"color": ...}}` *replaces* the whole dict and silently drops `size`. A nested `null` is a 422, or is stored as a value for variant `attributes`. RFC 7396 merges recursively and deletes on null. Fix: on PATCH, apply a recursive merge to dict fields from the raw body (pydantic drops the nulls), or document "top-level merge".
2. **The ETag includes `stock_quantity` and `updated_at` (2B-05).** Every sale changes it, so an admin's `If-Match` almost always gets a 412. The practical escape is an unconditional write. Stock is written as an *absolute* value, so a stale "10" erases two units that were just sold, and they are oversold. Fixes:
   - a relative `stock_delta` operation (`stock = stock + :d WHERE stock + :d >= 0`);
   - excluding volatile fields from the validator for non-stock edits;
   - requiring `If-Match` for stock writes (428 Precondition Required).
3. The admin lock is `FOR UPDATE`, which also blocks every concurrent checkout's FK check on that product (Q5). It should be `FOR NO KEY UPDATE`.

*Why optional If-Match:* backward compatibility with 3.x clients. That's a choice I'd revisit, given 2B-05.

**Evidence.**
- `app/core/etag.py:24-60`: tag, strong vs weak, 412 with the current ETag.
- `app/api/v1/admin.py:246-269`: optional If-Match with FU. `:271`, `:286-287`: `exclude_unset` and top-level `setattr`.
- `app/api/merge_patch.py:1-21`: claims RFC 7396.
- Variant path: `app/api/v1/admin.py:810`.

**Red flags.**
- "We're RFC 7396 compliant."
- Not knowing what the ETag hashes.
- Defending absolute stock writes.

---

## Q12. Testing strategy: what 98% coverage and a 98.4% mutation score do NOT prove

**Question.** "You have 5,214 tests, 100% branch coverage on the payment layer and a 98.4% mutation score, yet the audit found a High money-loss bug and a deadlock. Explain."
- *Follow-up 1:* "Why is SQLite the default test database for a Postgres app?"
- *Follow-up 2:* "Name a test that passes but doesn't test what it says."

**Why they ask.** Mature engineers know that coverage measures execution, not correctness. They want humility and a concrete improvement plan.

**Model answer.**
Coverage and mutation testing prove that the code I wrote is *executed* and that my assertions *notice changes to it*. They can't prove that the specification is right, or that the environment matches production. The audit hit all three gaps.

1. **The specification was wrong, and the tests pinned it.** `test_expired_unpaid_order_and_a_late_payment` asserts the "flag, don't apply" outcome for Q3. Mutation testing kills mutants against that wrong spec just fine. Property tests (the `PaymentChain` state machine) check invariants I listed. "The customer's money is always returnable" was never one of them.
2. **The environment differed:**
   - SQLite has no row locks, so `FOR UPDATE` and the FK `KEY SHARE` deadlock simply cannot occur.
   - `BCRYPT_ROUNDS=4` hides the 30 s idle-in-transaction kill (2D-01).
   - The mock Stripe once accepted a reused idempotency key on `confirm` that real Stripe rejects. Only the live E2E run found that.
3. **The harness was wrong (2B-02).** The PG lock-order tests attach their barrier to the *sync* engine while the routes use the async one. The hook never fires, so the tests pass without forcing the interleaving.

*Why SQLite by default:* sub-minute, hermetic local runs. CI runs the `db_sensitive` subset on PG 15, 16 and 18, plus real Redis. That split is reasonable, but "db_sensitive" is chosen by filename. Anything touching locks, timeouts or cost-sensitive loops should be PG-only by default.

*Plan:*
- Testcontainers PG as the default for integration tests.
- Assert that hooks fired.
- Production `BCRYPT_ROUNDS` in purge tests.
- Invariant properties on money ("every succeeded charge is either the order's payment or refundable").
- A tiny "chaos at the provider boundary" suite: delayed, reordered and duplicated events.

**Evidence.**
- `docs/TESTING.md:117-141`: gates and metrics. `:59`: db_sensitive by filename.
- `docs/reports/T10-mutation.md:104`: mutation scope limited.
- `tests/integration/test_payment_chain.py:426`: pinned outcome.
- `tests/integration/test_postgres_only.py:148-182`: hook on `engine`. `tests/conftest.py:219-225`: separate `async_engine`.
- `app/core/config.py:152-154`: test bcrypt rounds.
- `app/api/v1/orders.py:1172-1180`: the confirm-key bug found only against real Stripe.
- `docs/ARCHITECTURE.md:226-230`.

**Red flags.**
- "100% coverage means no bugs."
- Dismissing SQLite-vs-PG differences.
- Treating the mutation score as proof of correctness.

---

## Q13. Observability: can you trace a payment end to end?

**Question.** "A customer says they were charged but the order shows cancelled. Using only your logs, traces and admin endpoints, find out what happened."
- *Follow-up 1:* "Do your Celery logs carry the request id?"
- *Follow-up 2:* "What alerts would have fired?"

**Why they ask.** It's an operational-maturity probe. A design that "flags for manual review" is only as good as the place the flag shows up.

**Model answer.**
*What exists:*
- API logs are JSON lines carrying `request_id`/`trace_id`.
- `X-Request-ID` is validated and echoed.
- OpenTelemetry is optional, with requests and Celery instrumentation.
- `celery_context` copies the request id into task headers and restores it in the worker.
- Admin views: `GET /admin/webhook-events` (with replay), reconciliation discrepancies, and `GET /orders/{id}/payments`.

*The honest gaps:*
1. **Celery never installs the app's logging (2D-02).** Only `app/main.py` calls `setup_logging()`. The worker uses Celery's plain-text formatter, ignores `LOG_LEVEL`, and never prints the request id that `celery_context` carefully restores. So the trail breaks exactly at the webhook worker, which is where this incident lives. Fix: connect `celery.signals.setup_logging` to `setup_logging`.
2. **Manual-review flags are only a WARNING log.** No admin list spans orders, and there is no metric or alert. Reconciliation doesn't flag the case either.
3. **Misclassified errors:**
   - A deadlock is logged as "Database unavailable" and returned as 503.
   - `IdleInTransactionSessionTimeout` is an `InternalError`, so it's a generic 500 or a failed task, not an outage signal.
4. **No `/metrics` endpoint** (documented limitation). Nobody counts webhook backlog, failed events, open discrepancies, flagged payments, or purge success.

*Alerts I'd add:*
- `webhook_events` in `failed`, or `received` older than 30 minutes.
- Any `requires_manual_review=true` payment.
- Open discrepancies above 0.
- `truncated=true` on reconciliation.
- Purge processed 0 rows while erasures are pending.
- Deadlock rate.
- In-flight shed rate.

For this incident I'd answer by joining `payments` (flagged, succeeded) against `orders` (cancelled) and the event payload in `webhook_events`. The tooling to do it without SQL doesn't exist yet.

**Evidence.**
- `app/main.py:26`: the only `setup_logging()` call. `app/tasks.py:10`: no logging signal hookup.
- `app/core/celery_context.py:1-14`: claims it restores into the JSON formatter's contextvar.
- `app/core/logging_config.py:57-88`.
- `app/api/v1/orders.py:984-987`: the flag is a log line.
- `app/core/database.py:131-147`: classification.
- `docs/LIMITATIONS.md:30-32`: no Prometheus.

**Red flags.**
- "We have structured logging" without knowing it's API-only.
- No answer to "what alert fires".
- Relying on reading Celery stdout.

---

## Q14. Scaling: what breaks first at 10x load?

**Question.** "Your load test shows a knee at about 85-100 rps on 4 cores. Take me to 10x. What breaks first, second and third?"
- *Follow-up 1:* "Would you put PgBouncer in front? What breaks if you do?"
- *Follow-up 2:* "What about a flash sale on one SKU?"

**Why they ask.** They want bottleneck reasoning grounded in this code, not generic "add Kubernetes".

**Model answer.**
The baseline, honestly stated: 4 Uvicorn workers, in-flight cap 15, pool 10+5, *mock* payments. Real Stripe adds 0.4-0.7 s per call on the threadpool. In order of what breaks:

1. **Postgres connections.**
   - Each API worker holds two pools of 15. Scaling workers 10x means 1,200 or more possible connections against `max_connections=200`.
   - Fix: drop the API-side sync pool (Q6), then use PgBouncer.
   - Caveat: in *transaction* pooling mode the per-connection startup `options` (`statement_timeout`, `idle_in_transaction_session_timeout`) aren't supported, so they move to a role-level `ALTER ROLE ... SET` (or session mode).
2. **CPU on bcrypt.** About 0.3 s of a core per login or registration, run in the shared anyio threadpool (about 40 threads) that also carries Stripe calls. Login bursts starve payments. Fix: a dedicated bounded executor per concern, or a separate auth service.
3. **Hot-row contention.** Every checkout of a SKU serialises on its `FOR UPDATE` product row, and cancels and expiry take the same lock. A flash sale is effectively single-threaded per SKU. Fix: a reservation table (insert-only, TTL) or N stock buckets, with `FOR NO KEY UPDATE` as the minimum.
4. **Redis as a single instance for four roles** (broker, cart, auth state, rate limits). Split them and add replication (Q9).
5. **Background jobs that don't scale with volume:**
   - Reconciliation caps at 200 per day and runs serially (2A-05).
   - The expiry sweep scans `orders` with only `idx_order_status`. A partial index on `(created_at) WHERE status='new'` fixes the scan.
   - One prefork worker consumes every queue, so a slow reconciliation competes with webhook processing. Give `payments` its own workers.
6. **API shape.** Offset pagination on deep pages (documented). The admin stats query aggregates over 30 days of `order_items`.

The in-flight limiter means overload sheds as 503 rather than collapsing. That's the right *failure mode*, but it isn't capacity.

**Evidence.**
- `docs/PERFORMANCE.md:9-10`, `:27-30`: benchmark setup and knee.
- `app/core/database.py:96-98`: 2 pools per worker. `docker-compose.yml:170-174`.
- `app/core/database.py:56-63`: startup options.
- `app/api/v1/orders.py:1151-1158`: Stripe latency in the threadpool. `app/api/v1/users.py:339-359`: bcrypt in the threadpool.
- `app/services/orders.py:83-91`: FU locks.
- `app/models/order.py:65-67`: indexes. `app/tasks.py:480-488`: sweep query.
- `docker-compose.yml:68`: one worker for all queues.
- `docs/LIMITATIONS.md:54-56`: offset pagination.

**Red flags.**
- "Add more pods."
- Not knowing that PgBouncer transaction mode drops startup options.
- Ignoring that load tests used mock payments.
- Not identifying the hot-row issue.

---

## Q15. If you started again, what would you change?

**Question.** "Knowing everything you know now, what would you design differently? I want architecture, not bug fixes."
- *Follow-up:* "Which of those would you do before launch, and which after?"

**Why they ask.** It tests reflection and prioritisation, and whether you can separate the architectural root causes from the symptoms the audit found.

**Model answer.**
Root causes, not symptoms:

1. **Make the payment provider a first-class state machine.** Most money bugs (2A-01, 2A-02, 2A-04) come from local order state and Stripe objects having separate lifecycles that are only loosely linked:
   - an explicit `payment_attempts` lifecycle that includes `provider_pending`;
   - `expires_at` on every session, tied to the stock reservation;
   - cancellation that *always* expires or cancels the provider object;
   - auto-refund of orphan charges.
2. **Treat webhooks as triggers, and fetch current state.** Re-fetching the PaymentIntent, Refund or Dispute removes ordering bugs (2A-03) entirely, at the cost of one API call per event.
3. **Use a transactional outbox for every side effect** (email, cache invalidation, provider expire calls), relayed by Celery. That fixes the non-durable emails (2D-07) and gives one place to observe them.
4. **Use stock reservations instead of decrement-at-order.** A reservation row with a TTL decouples hot rows, makes expiry a query rather than a sweep, and removes the FK-lock interplay.
5. **Snapshot orders immutably:** shipping address, product name and price on the order. That fixes address rewrites (2C-06) and makes GDPR redaction safe for retained invoices.
6. **Test against Postgres first.** Testcontainers by default; SQLite only for pure unit tests; production-like cost settings for anything time-bounded.
7. **One process model for observability.** Shared logging bootstrap for API, worker and beat; metrics from day one.
8. **One async stack.** An async Celery alternative (e.g. arq or Dramatiq with asyncio), or sync everywhere. Two engines per process wasn't worth it.

*Before launch (days of work):*
- Expiry respects in-flight attempts, plus `expires_at`.
- Reconciliation checks `pending-cs` sessions.
- Monotonic refund status.
- `FOR NO KEY UPDATE`.
- Purge bcrypt moved outside the transaction.
- Celery logging.
- Post-erasure redaction.

*After launch:* outbox, reservations, snapshotting, and the pooling and Redis split.

**Evidence.** These are the findings listed above. The architectural seams are at `app/services/stripe_events.py:270` (`apply_event` applies payload snapshots), `app/api/v1/orders.py:396-400` (in-process email), `app/services/orders.py:64-91` (decrement-at-order locking) and `app/models/order.py:39` (no snapshot).

**Red flags.**
- "Nothing, it's solid."
- Switching languages or frameworks as the answer.
- Listing bug fixes as if they were architecture.
- Not being able to prioritise.

---

## The 30-second elevator pitch

> "It's a FastAPI e-commerce backend built around one principle: **money and stock state live in Postgres, and every external effect is idempotent and reconcilable.**
>
> Routes are async on SQLAlchemy `AsyncSession`. The domain logic for checkout, restock and payment settlement is written once as sync code and shared with Celery through `run_sync`.
>
> Checkout reserves its `Idempotency-Key` with a unique index in the same transaction as the stock decrement. That decrement is a conditional UPDATE, with row locks taken in a fixed order and CHECK constraints behind it.
>
> Payments commit a `processing` attempt before calling Stripe, with deterministic, namespaced provider keys, and never hold a transaction across HTTP.
>
> Stripe webhooks are signature-verified, stored first, and applied by a worker under `SKIP LOCKED`. The processed mark commits with the effect, with backoff, a sweeper for lost messages, and admin replay. A daily job reconciles our ledger against Stripe.
>
> Errors are RFC 9457 throughout. Token revocation in Redis fails closed with 503, while rate limits fail open. GDPR erasure anonymises in place so invoices survive.
>
> It's tested with about 5,200 tests, Postgres and real-Redis CI, live Stripe test mode, chaos and load runs. But a pre-release audit found real gaps at the provider boundary, which I'll walk you through."

The last sentence matters: you set the agenda before they find the gaps.

---

## Known limitations to raise before they ask

Ordered by severity. Each one names the fix in one breath.

1. **Order expiry ignores live payments.** Checkout Sessions have no `expires_at` and are never expired, so a customer can pay for a cancelled order and the app can't refund it. Fix: skip orders with in-flight attempts, add `expires_at`, expire on cancel, auto-refund. (2A-01 / 2B-03)
2. **The GDPR purge can't complete on Postgres.** 100 bcrypt hashes in one transaction exceed the 30 s idle-in-transaction timeout. Fix: hash once, outside the transaction. (2D-01)
3. **Reconciliation never checks Checkout payments whose completion webhook was lost.** It also caps at the newest 200 and never enumerates Stripe-side objects. Fix: a session lookup and a persisted cursor. (2A-02, 2A-05)
4. **Out-of-order refund events can move REFUNDED back to PAID.** Fix: monotonic refund status, or refetch on event. (2A-03)
5. **A checkout can deadlock on Postgres.** The `FOR UPDATE` stock locks conflict with the `FOR KEY SHARE` that the `order_items` FK check takes at commit. The deadlock is reported as a 503 "database unavailable". The PG lock tests hook the wrong engine. Fix: `FOR NO KEY UPDATE`, plus fixing the test hook. (2B-01, 2B-02)
6. **Two payable attempts after the 5-minute stale window** can double-charge; the second charge is only flagged. (2A-04)
7. **Password reset doesn't evict an in-flight login or rotation, and sessions have no absolute lifetime.** Two concurrent refreshes revoke the family (documented). (2C-02, 2C-03, 2C-09)
8. **Post-erasure webhook payloads keep PII.** Erasure also ignores open orders and disputes, and orders reference a mutable address with no snapshot. (2C-01, 2C-10, 2C-06)
9. **Celery logs aren't JSON and lose `request_id`.** Manual-review flags are only a log line, and there's no metrics endpoint. (2D-02)
10. **Merge Patch is top-level only, and the stock-bearing ETag pushes admins into unconditional absolute stock writes.** (2B-04, 2B-05)
11. **Order emails are not durable.** They go through in-process `BackgroundTasks`; the Celery email tasks are dead code. (2D-07)
12. **Docs and code disagree.** ARCHITECTURE.md's idempotency section (409 vs 422, fingerprint contents), and the `/2` pool advice in `overload.py` and PERFORMANCE.md. (2D-C02)
