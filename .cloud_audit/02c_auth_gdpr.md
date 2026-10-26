# 02C — Auth, Tokens, Rate Limiting, RFC 9457, GDPR (Subagent 2C)

Repo: `fastapi_e-commerce_backend` v4.0.3 — audit date 2026-10-26.
Method: read every file in scope line by line, enumerated the routes and their auth dependencies programmatically, and ran scratch pytest reproductions against the project's own fixtures. Nothing under `app/`, `migrations/` or `tests/` was modified.

Scratch reproductions (all pass, i.e. each behaviour is **confirmed**):
- `/tmp/claude-0/-home-user-fastapi-e-commerce-backend/d6bb2a49-b210-591b-b7be-8a1e71874dff/scratchpad/audit2c/test_audit_2c.py` (2C-01, 2C-05, 2C-06, 2C-07, 2C-08)
- `/tmp/claude-0/-home-user-fastapi-e-commerce-backend/d6bb2a49-b210-591b-b7be-8a1e71874dff/scratchpad/audit2c/test_audit_2c_races.py` (2C-02, 2C-03, 2C-04, 2C-09)
- `/tmp/claude-0/-home-user-fastapi-e-commerce-backend/d6bb2a49-b210-591b-b7be-8a1e71874dff/scratchpad/audit2c/test_audit_2c_rfc.py` (RFC 9457 matrix)
- `/tmp/claude-0/-home-user-fastapi-e-commerce-backend/d6bb2a49-b210-591b-b7be-8a1e71874dff/scratchpad/routes.py` (route → auth dependency map)

Run with: `cd /home/user/fastapi_e-commerce_backend && unset UV_NATIVE_TLS; PYTHONPATH=. uv run pytest -p tests.conftest <file> -q -p no:cacheprovider --rootdir=.`

---

## 1. Scope and files read

| File | Read |
|---|---|
| app/core/security.py | ✔ all 721 lines |
| app/core/passwords.py | ✔ |
| app/core/rate_limit.py | ✔ |
| app/core/errors.py | ✔ |
| app/core/forwarded.py | ✔ |
| app/core/security_headers.py | ✔ |
| app/core/body_limit.py | ✔ |
| app/core/overload.py | ✔ |
| app/core/head.py (on the error path) | ✔ |
| app/core/probes.py (on the error path) | ✔ |
| app/core/text_input.py | ✔ |
| app/core/dependencies.py | ✔ |
| app/core/request_context.py | ✔ |
| app/core/openapi.py | ✔ (documentation only; no runtime security effect) |
| app/core/config.py | ✔ all 570 lines |
| app/core/clock.py | ✔ |
| app/main.py | ✔ (middleware order, handlers) |
| app/api/v1/users.py | ✔ all 1083 lines |
| app/api/v1/legal.py | ✔ |
| app/api/v1/admin.py | ✔ auth on every route (checked by script), plus the webhook-event list and replay |
| app/api/v1/cart.py | ✔ cookie section (L76-232) |
| app/api/v1/orders.py | ✔ ownership filters, webhook storage (L1841-1915) |
| app/services/gdpr.py | ✔ |
| app/services/redis_async.py | ✔ |
| app/services/redis.py | ✔ token, blacklist and session parts (L382-629) |
| app/services/email/*.py | ✔ (`__init__`, base, logging, smtp, messages) |
| app/services/stripe_events.py | ✔ all 759 lines |
| app/models/user.py, address.py, payment.py, order.py, base.py | ✔ |
| app/schemas/user.py, legal.py, admin.py, address.py, webhook_event.py, common.RequestBody | ✔ |
| app/tasks.py | ✔ all 752 lines |
| migrations (grep for ON DELETE) | ✔ none present |

**Route → auth map** (from `routes.py`): every `/admin/*` route, plus `PUT /orders/{id}/status`, `POST /orders/{id}/refund` and `GET /orders/{id}/payments`, resolves `get_current_admin_user`. Every `/users/*` route that needs a user, `/legal/consent|data-request|consent-status|data-retention` and the customer order routes resolve `get_current_active_user`. The cart routes use `get_optional_active_user`. The public routes are register, login, refresh, verify-email, the password-reset routes, the catalog, the legal documents, the probes and the Stripe webhook (which is signature-verified). No admin route is missing its guard.

---

## 2. Summary table

| ID | Title | Severity | Confidence |
|---|---|---|---|
| 2C-01 | Stripe events stored **after** erasure keep raw PII forever: no purge ever redacts them again | **Medium** | Confirmed (test) |
| 2C-02 | Password reset does not evict an attacker who knows the old password: a login still inside bcrypt mints tokens newer than the watermark | **Medium** | Confirmed (interleaving test) |
| 2C-03 | Refresh rotation racing a reset survives it. Reset relies on the watermark only (families are not revoked), and there is no absolute session lifetime | Low | Confirmed (interleaving test) |
| 2C-04 | `/legal/data-request` (deletion) is an unthrottled password-guessing oracle that bypasses the login lockout | **Medium** | Confirmed (test + reading) |
| 2C-05 | Registering without `full_name` crashes the welcome-email task: no verification link is sent, and checkout requires verification | **Medium** (functional) | Confirmed (test) |
| 2C-06 | Editing an address rewrites the shipping address of existing, paid or delivered orders (no snapshot) | **Medium** | Confirmed (test) |
| 2C-07 | `PATCH/PUT /users/me` changes `marketing_consent` with no `consent_history` entry and no date (Art. 7(1)) | **Medium** (compliance) | Confirmed (test) |
| 2C-08 | Rejected CORS preflights answer `400 text/plain`, not problem+json | Low | Confirmed (test) |
| 2C-09 | Reuse detection logs out the legitimate client on a benign double refresh (two tabs, retry after a timeout) | Low | Confirmed (test) |
| 2C-10 | Erasure ignores in-flight, paid-unshipped, disputed and refunding orders: the address is redacted after 1 day, which breaks fulfilment and chargeback defence | **Medium** | Confirmed (reading) |
| 2C-11 | The Art. 15/20 export is not implemented; the email says "here is your personal data export" with no data in it | **Medium** (compliance) | Confirmed (reading) |
| 2C-12 | The deletion confirmation is sent at request time and claims "personal data has been deleted… Order history" | Low | Confirmed (reading) |
| 2C-13 | The retention purge is effectively a no-op (only erasure ever deactivates an account); `INACTIVE_ACCOUNT_DELETE_DAYS` is unused | Low | Confirmed (reading) |
| 2C-14 | Email amplification: each export request does 2 synchronous SMTP sends at 300/min per user; account-exists and reset mails are throttled per IP only | Low | Plausible |
| 2C-15 | Per-email lockout DoS is cheap over IPv6 (100 distinct /64s) | Low | Confirmed (reading; documented trade-off) |
| 2C-16 | The shed 503 and the unhandled-exception 500 carry no CORS headers; the logout and reset 503s lack the `retry_after` member | Info | Confirmed (reading) |
| 2C-17 | JWT hygiene: no `iss`/`aud`/`kid` and no key rotation; deployment check does not enforce `RATE_LIMIT_ENABLED` or `GUEST_CART_COOKIE_SECURE`; no bcrypt rehash on cost change | Info | Confirmed (reading) |
| 2C-18 | Latent hazard: ORM `cascade="all, delete-orphan"` on `User.orders`; dead sync token code with different failure semantics | Info | Confirmed (reading) |
| 2C-19 | `ForwardedClientMiddleware`: a client that puts the proxy or peer address in XFF becomes `client=None` ("unknown" bucket), even behind a trusted proxy | Info | Confirmed (reading) |

---

## 3. Detailed findings

### 2C-01 — Webhook events stored after erasure keep raw PII indefinitely (Medium, Confirmed)

**Where**
- `app/tasks.py:332-342`: the erasure purge excludes accounts that are already anonymised:
  ```python
  anonymised_email = (literal("deleted-user-") + cast(User.id, String) + literal("@deleted.invalid"))
  pending = select(User)...where(..., User.email != anonymised_email,)
  ```
- `app/tasks.py:426-433`: the retention purge excludes them the same way.
- `app/tasks.py:363` `stripe_events.redact_users_events(db, [a.id for a in erased])` is the only caller of the redaction, and it runs once per account.
- `app/api/v1/orders.py:1903-1909` stores the full `event.data` for every new event:
  ```python
  json.dumps({"id": event.id, "type": event.type, "created": event.created, "data": event.data})
  ```

**Scenario.** A user requests erasure and is anonymised on day 2. Over the next weeks Stripe delivers `charge.refunded`, `refund.updated`, `charge.dispute.created/updated/closed` (disputes can arrive up to 120 days later) or a late `checkout.session.completed`. Each carries `billing_details` (name, email, address), `customer_details`, `shipping` or dispute `evidence` (customer_name, customer_email_address, billing_address). These rows are stored un-redacted, and no later run ever revisits the user. The Art. 17 erasure is silently undone for every post-erasure event. The scratch test `test_2c01_event_arriving_after_erasure_keeps_pii_forever` confirms that `jane@example.com` remains in the payload after both purges run.

**Fix (minimal).** Redact at ingest when the referenced order belongs to an anonymised user. Alternatively, redact again in the worker after applying the event, which is the more robust option because the event must still be applied:
```diff
 # app/services/stripe_events.py, process_stored_event, after apply_event(...)
-        result = apply_event(db, row.event_type, data, after_commit)
+        result = apply_event(db, row.event_type, data, after_commit)
+        _redact_if_owner_erased(db, row)   # payload -> redact_event_payload(...) when the
+                                           # order's user has data_deletion_requested (or the
+                                           # anonymised email); same transaction
         db.commit()
```
A daily sweep (`redact_users_events` over every anonymised user's payment ids, limited to events created after `data_deletion_date`) also closes the gap for events that are never processed (foreign or failed).

**Pytest sketch.** See `test_2c01_…` in the scratch file. Store `charge.refunded` with `billing_details.email` after `purge_expired_deletion_requests`, then assert the stored payload no longer contains the email once the fix is in.

---

### 2C-02 — An in-flight login with the old password survives a password reset (Medium, Confirmed)

**Where**
- `app/api/v1/users.py:332-337`: the hash is read, then the session is closed.
- `app/api/v1/users.py:357-359`: bcrypt runs, about 250 ms at cost 12, plus semaphore and threadpool queueing.
- `app/api/v1/users.py:386-388`: tokens are minted with **no re-check that the hash is still the one verified**:
  ```python
  access_token = create_access_token(user_id)
  refresh_token = await issue_refresh_token(user_id)
  ```
- `app/api/v1/users.py:1055-1072`: reset writes the watermark, then updates the hash.

**Scenario.** The attacker knows the victim's password (phishing or reuse) and holds sessions. The victim resets the password to evict them. The attacker keeps one `POST /users/login` permanently inside bcrypt: one request every ~250 ms is 240/min. `RATE_LIMIT_AUTH` (20/min) is per IPv4 address or per IPv6 /64, so 12 addresses are enough, and successful logins clear the failure counters. Whichever login is inside bcrypt when the reset lands mints an access token and a refresh chain whose `iat` is later than the `pwd_changed` watermark. Both stay valid. Refresh rotation is sliding with no absolute lifetime (2C-03), so the attacker keeps access indefinitely, until the victim explicitly signs out everywhere. The comment at L1052-1054 handles the reset's own bcrypt, not the login's.

Scratch test `test_2c02_…` makes `verify_password_async` perform the reset (watermark plus hash update) before returning. `/users/me` then answers 200 and `/users/refresh` works.

**Fix**
```diff
 # users.py login(), after the tokens are created:
+    current = await db.scalar(select(User.hashed_password).where(User.id == user_id))
+    if current != hashed_password:
+        raise HTTPException(401, "Incorrect email or password", headers={"WWW-Authenticate": "Bearer"})
 # users.py reset_password(), after `await db.commit()` and updated == 1:
+    await invalidate_tokens_issued_before_now(user_id)          # 2nd watermark (best effort; log on failure)
+    await async_redis_service.revoke_user_refresh_families(user_id)
```
Together these make the interleaving safe. If login's re-read happens before the reset commits, then the token's `iat` is before the second watermark and the token is refused. If the re-read happens after the commit, it sees the new hash and refuses.

---

### 2C-03 — Refresh rotation racing a reset survives it; no absolute session lifetime (Low, Confirmed)

**Where.** `app/core/security.py:527-539`: the watermark check is made before the new token is minted, and nothing re-checks it after.
```python
if await is_token_superseded(user_id, issued_at): ...
new_refresh_token = await issue_refresh_token(user_id, family=family)   # iat = time.time() now
consumed = await async_redis_service.consume_refresh_token(jti)
```
`reset_password` (users.py:1055) only moves the watermark and never calls `revoke_user_refresh_families`. Each rotation also issues a fresh 7-day `exp` (`security.py:233-250`), so a chain never ends.

**Scenario.** A watermark written between the GET at L527 and `time.time()` at L215 produces a new token with `iat` > watermark, minted from a pre-reset token. The window is about one Redis RTT plus event-loop latency. The attacker can retry serially at a high rate, and this compounds with 2C-02. Scratch test `test_2c03_…` confirms the chain keeps rotating after the reset. Clock skew between replicas, where `iat` and the watermark come from different hosts' `time.time()`, widens the window in the same direction.

**Fix.** Have reset also revoke the families (see 2C-02). Do the check-and-register atomically in one Lua script that uses Redis `TIME` both for the watermark (`invalidate_tokens_issued_before_now`) and for the new token's registration time. Add an absolute family lifetime: store `family_started_at` at login and refuse rotation past N days.

---

### 2C-04 — Deletion re-authentication is an unthrottled password oracle (Medium, Confirmed)

**Where.** `app/api/v1/legal.py:338-343`:
```python
if not await verify_password_async(request_data.password, current_user.hashed_password):
    raise HTTPException(status_code=401, detail="Incorrect password", ...)
```
`/legal/data-request` is not in `_ROUTE_LIMITS` (`app/core/rate_limit.py:75-87`), so it falls under `RATE_LIMIT_DEFAULT` = 300/minute per user (`config.py:281`). It never calls `check_login_attempt`.

**Scenario.** Someone holding a stolen access token, or refresh token (which renews indefinitely), gets 300 guesses/min, about 432k/day, against the account password. The login route allows 10 per 15 minutes. Each guess costs one bcrypt for the server, a CPU drain as well. A correct guess both reveals the password (for reuse on other sites) and erases the account irreversibly. Scratch test `test_2c04_…` sends 30 wrong guesses and gets 30 × 401 with no lockout.

**Fix.** Before verifying, call `check_login_attempt(current_user.email, request)`. On success call `clear_login_failures`. Alternatively give the route its own tight limit in `_ROUTE_LIMITS`, e.g. `("POST","/legal/data-request"): "RATE_LIMIT_AUTH"`. Even so, the per-user failure count is what actually stops the guessing.

---

### 2C-05 — Welcome/verification email crashes when `full_name` is omitted (Medium functional, Confirmed)

**Where.** `app/services/email/messages.py:207`: `safe_full_name = html.escape(full_name)`. `full_name` is optional (`app/schemas/user.py:92`, default None). The call site is `app/api/v1/users.py:212-217`.

**Effect.** `html.escape(None)` raises `AttributeError`, and the background task dies after the 202 is sent. The user never receives the verification link. Order creation refuses unverified users (`app/api/v1/orders.py:186`), so they cannot check out unless they find `/users/verify-email/resend`. Confirmed by `test_2c05_…`.

**Fix.** `safe_full_name = html.escape(full_name or "customer")`, or use a greeting without the name.

---

### 2C-06 — Address edits rewrite historical and paid orders (Medium, Confirmed)

**Where.**
- `app/models/order.py:39` `shipping_address_id: Mapped[int] = mapped_column(ForeignKey("addresses.id"))`: orders have no snapshot of the address.
- `app/api/v1/users.py:777-780` mutates the row in place:
  ```python
  for field, value in address_in.model_dump(exclude_unset=True).items():
      setattr(address, field, value)
  ```

**Scenario.** After payment and fraud checks, a customer patches the address used by a `paid` order, for example to another country. This changes the destination of an order already accepted, and it can also change the tax jurisdiction. Shipped and delivered orders and their invoices change retroactively, which is an accounting and record-keeping integrity problem. `test_2c06_…` shows `GET /orders/{id}` returning the new street and country for a delivered order. The same mechanism makes the GDPR redaction (`tasks.py:390-397`) wipe the address of unshipped orders (2C-10).

**Fix.** Copy-on-write: if the address is referenced by any order, deactivate it and insert a new row with the edits, returning the new id. The better long-term fix is to snapshot the shipping fields onto `orders` at checkout.

---

### 2C-07 — Marketing consent changes via `/users/me` leave no consent record (Medium compliance, Confirmed)

**Where.** `app/schemas/user.py:159` `UserUpdate.marketing_consent`, and `app/api/v1/users.py:541-542` `setattr(user, field, new_value)`. Compare `app/services/gdpr.py:40-49`, which appends history and sets `marketing_consent_date`.

**Effect.** Consent granted or withdrawn through the profile route has no timestamp, IP or UA entry, and `marketing_consent_date` stays stale or null. Art. 7(1) requires the controller to be able to demonstrate consent, and that is no longer possible. `test_2c07_…`: after PATCH, `marketing_consent=True`, `marketing_consent_date is None`, and the last history entry still says `granted: False`.

**Fix.** Remove `marketing_consent` from `UserUpdate` (consent belongs in `/legal/consent`), or route it through `GDPRService.update_user_consent`.

---

### 2C-08 — Rejected CORS preflight is `text/plain` (Low, Confirmed)

`CORSMiddleware` (`app/main.py:94-105`) answers a disallowed origin, method or header with `PlainTextResponse("Disallowed CORS origin|headers|method", 400)`. This is the one error path not covered by problem+json. Confirmed in `test_audit_2c_rfc.py`.
Fix: subclass `CORSMiddleware` and override `preflight_response` so that a non-200 result is returned as `problem_response(scope, Problem(400, detail=...))`, keeping the headers.

---

### 2C-09 — Benign double refresh revokes the winner's new token (Low, Confirmed)

`app/core/security.py:539-544`: a second presentation of the same jti calls `revoke_refresh_family(family)`. That revokes the token just handed to the first caller, because it was registered before that caller's consume (L537). Two tabs, or a mobile client retrying after a response lost to a network timeout, therefore force a re-login. Confirmed by `test_2c09_…`; the behaviour is intended reuse detection, but the cost is real for clients.
Mitigation used by OAuth providers: accept the immediately-previous jti for a short grace period (e.g. 10-30 s), returning the same successor, before treating it as a replay.

---

### 2C-10 — Erasure ignores open commercial and legal state (Medium, Confirmed by reading)

**Where.** `app/services/gdpr.py:119-147` deactivates with no checks. `app/tasks.py:315-342,386-397` redacts every address (`REDACTED`), the email and the name after `DATA_DELETION_GRACE_PERIOD_DAYS = 1` (`config.py:343`), whatever the state of the user's orders, payments or disputes. Art. 17(3)(b) and (e) (legal obligation, legal claims) are not applied. See the collision matrix in §4 for each case.
**Fix.** In `process_data_deletion`, refuse (409) or defer erasure while orders are `confirmed/processing/sent` with `payment_status=paid`, a `PaymentDispute` is open or a refund is pending. In the purge, add `NOT EXISTS` (such orders or open disputes) to `pending`, or postpone `data_deletion_date`. Keep the shipping and billing snapshot of retained invoices; see 2C-06.

---

### 2C-11 — Data export (Art. 15/20) not implemented, yet claimed (Medium compliance, Confirmed)

`app/services/gdpr.py:100-109`: "In production, the export payload would be assembled here". The method only emails `request_id/request_date/expires_at`. `messages.py:325-331` says "here is your personal data export". `User.data_export_requests` is never appended to. The export does not cover users, addresses, orders, order_items, payments, refunds, disputes, `consent_history` or the webhook payloads that hold the user's data.
**Fix.** Assemble a JSON export of those tables (and the user's webhook events, redacted of other parties), store it behind a signed, expiring link, log the request in `data_export_requests`, and send the link.

### 2C-12 — Deletion "confirmation" is false at send time (Low, Confirmed)
`gdpr.py:143-145` sends `send_gdpr_deletion_confirmation` immediately, before the purge. Its text (`messages.py:373-386`) says "your personal data has been deleted" and lists "Order history", which is in fact retained by design (`tasks.py:301-311`). `DataRequestResponse` promises 24h, while `send_gdpr_request_received` says "within 30 days". This is an Art. 12 transparency issue. Fix: send "scheduled" now and "completed" from the purge task, and list what is retained and why.

### 2C-13 — Retention purge never fires in practice (Low, Confirmed)
`tasks.py:429-433` only considers `is_active == False`, and the only code that sets it is `gdpr.py:137` (erasure, which is purged after 1 day anyway; admin has no user-management routes). `INACTIVE_ACCOUNT_DELETE_DAYS=730` (`config.py:339`) is referenced nowhere. Dormant active accounts are kept forever, while the privacy policy (`legal.py:71-75`) says 365 days. `GET /legal/data-retention` (`gdpr.py:172-181`) compares against `created_at`, so any customer older than a year is told `within_retention_period: false`. Fix: define the dormancy rule, e.g. `last_login` older than `INACTIVE_ACCOUNT_DELETE_DAYS` (with a notification email first), and apply it.

### 2C-14 — Email / threadpool amplification (Low, Plausible)
`gdpr.py:98,109`: each export request performs 2 synchronous SMTP sessions (~0.8 s each) in the shared anyio threadpool (~40 threads, also used for bcrypt). The route allows 300/min per user, so a handful of free accounts can occupy the pool and slow every bcrypt route. `register` (account-exists email) and `password/reset` can mail any address at 20/min per IP or IPv6 /64, with no per-recipient throttle. Fix: put `/legal/data-request` under a tight limit, queue the mails through Celery, and add a per-recipient cool-down (`mark_once(f"mail:{kind}:{sha(email)}", 600)`).

### 2C-15 — Cheap per-email lockout over IPv6 (Low, Confirmed)
`rate_limit.py:173-176` counts IPv6 per /64, and `RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL="100/hour"`. An attacker with a routed /48 (65,536 /64s) can lock any known email out of login for an hour, repeatedly. The trade-off is documented (`rate_limit.py:368-370`), but IPv6 makes "many addresses" nearly free. Consider counting failures per /48 as well, or have the per-email backstop trigger a CAPTCHA or email unlock link instead of a hard 429.

### 2C-16 — Error responses missing CORS and `retry_after` (Info)
- `InFlightLimitMiddleware` sits outside `CORSMiddleware` (`main.py:94-115`), and `ServerErrorMiddleware` is outermost. The shed 503 and the 500 therefore have no `Access-Control-Allow-Origin`, so browsers cannot read them or their Retry-After.
- `users.py:474-478` and `1056-1060` raise 503 as a plain `HTTPException(headers={"Retry-After": "5"})`, so the body has no `retry_after` member. `errors.py:30-31` says 503s that have one carry it.
- `/health` and `/readyz` 503s are `application/json` (`probes.py:77-82`). This is deliberate and acceptable.

### 2C-17 — JWT and configuration hygiene (Info)
- There is no `iss`/`aud`/`kid`, and one HMAC key signs access, refresh, reset and verify tokens. The `type` claim is checked on every path (`security.py:338,512,701,717`), so type confusion is not possible today. Rotating the key logs everyone out and invalidates all mailed links; a `kid` with two accepted keys would allow rolling rotation.
- `check_deployment_settings` (`config.py:503-549`) does not refuse `RATE_LIMIT_ENABLED=false` or `GUEST_CART_COOKIE_SECURE=false` in production.
- There is no bcrypt rehash-on-login when `BCRYPT_ROUNDS` changes. With mixed-cost hashes the dummy hash (`security.py:91`, current cost) no longer matches some real accounts' timing, which reopens a small enumeration channel.
- `PasswordUpdate` (`schemas/user.py:232`) is unused, so there is no authenticated change-password route; users must go through the reset email.

### 2C-18 — Latent cascade hazard and dead code (Info)
- `app/models/user.py:55-56`: `cascade="all, delete-orphan"` on `orders` and `addresses`. Any future `db.delete(user)` would delete the order history of users without payments, and would fail with an IntegrityError for users with payments, since `payments.order_id` has no ORM cascade and there is no `ON DELETE` in the migrations. Today no code deletes users (`tasks.py:301-311` explains the choice). Use `cascade="save-update, merge"` and `passive_deletes`/`RESTRICT` instead.
- The sync `RedisService.consume_refresh_token` returns `None` on a Redis error (`redis.py:470-473`), which a caller would read as a replay and use to burn the family. `is_blacklisted` fails to True. Neither is used (the async path is correct), but both are easy to reintroduce. Delete them.

### 2C-19 — `ForwardedClientMiddleware` self-reference rule applies behind trusted proxies (Info)
`forwarded.py:104-107`: if the socket peer's address appears anywhere in X-Forwarded-For, the client becomes `None`, and the rate limiter keys it as `ip:unknown`. This happens even when the peer is a trusted proxy and the entry was written by the client (`XFF: <proxy-ip>`, to which the proxy appends the real IP). The client only moves itself into the shared "unknown" bucket, which gives it a second bucket and no ability to hurt others, so the impact is minimal. Check `client[0] in hosts` only when the peer is **not** in `TRUSTED_PROXIES`.

---

## 4. GDPR collision matrix

| Scenario at erasure request | Current behaviour (evidence) | Risk |
|---|---|---|
| (a) Order NEW/unpaid, PaymentIntent in flight or Checkout Session open | The account is deactivated (`gdpr.py:137`); the user can no longer cancel (403). `expire_unpaid_orders` cancels the order after 30 min and emails the real address while it still exists (`tasks.py:461-526`). If the customer completes 3DS or Checkout later, the success is flagged for manual review (`orders._apply_payment_success`). The late event's `customer_details` are stored un-redacted (2C-01). | Money taken for a cancelled order with no reachable customer (email is `@deleted.invalid` after day 1); PII stored after erasure |
| (b) Paid, not shipped | Erased after 1 day: the address becomes `REDACTED` (`tasks.py:390-397`) and the order keeps referencing it. Status emails go to `deleted-user-N@deleted.invalid`. | Fulfilment impossible; contract (Art. 6(1)(b)) breached; refund handled manually (2C-10) |
| (c) Open Stripe dispute | No check. PII erased, so evidence (name, address, delivery proof) cannot be assembled. Later `charge.dispute.*` events with `evidence` PII are stored un-redacted (2C-01). | Chargeback defence lost; Art. 17(3)(e) not applied; PII reintroduced |
| (d) Refund in progress | Refund and dispute handlers find the Payment by `payment_intent_id` (`stripe_events.py:196-221`), so they still apply after erasure. `charge.refunded` stores `billing_details` un-redacted (2C-01). `refund.failed` after erasure flags review, but the customer cannot be contacted. | Functional; PII reintroduced; failed refund cannot be resolved with the customer |
| (e) PII in stored webhook JSON and Stripe metadata | Metadata holds `order_id` only (`orders.py:1166,1451`): good. Payloads that already exist are allow-list redacted at purge time (`stripe_events.py:663-759`), matched by PI, session or `order_id`, and idempotent: good. **Later events are never redacted (2C-01).** `last_error` is kept (normally holds no PII). | Medium |
| (f) Logs | Log lines use user ids and `mask_email` (`email/base.py:10-18`); profile updates log field names only (`users.py:518-530`). Uvicorn access logs hold client IPs and paths; tokens are sent in bodies, not URLs (`TokenBody`). | Low; IP retention is an infrastructure policy matter |
| Financial records retained, PII pseudonymised? | Yes: the User row is kept and scrubbed (email, name, phone, consent IP/UA, export list); orders, items, payments, refunds and disputes are kept (`tasks.py:301-311,370-397`). But order addresses are lost (no snapshot), and invoices can be altered by address edits before erasure (2C-06). | Partial |
| FK cascade breaking accounting? | No DB `ON DELETE` (migrations grep); there is no hard delete in code. The ORM cascade is a latent hazard only (2C-18). | Info |
| Later webhook crash or PII re-creation? | No crash: handlers do not need user PII, and `order.user.email` resolves to the anonymised address. PII **is** re-created in `webhook_events.payload` (2C-01). | Medium |
| Export complete? | No: there is no export at all (2C-11). | Medium |
| Retention job: timezone and batching | Aware UTC throughout (`clock.py`, `UTCDateTime`); keyset batches of 100 with a commit per batch; idempotent through the anonymised-email marker. Sound, but it never selects anything in practice (2C-13). | Low |

---

## 5. RFC 9457 coverage matrix (probed with TestClient, `test_audit_2c_rfc.py`)

| Error source | Status | Content-Type | Handled? |
|---|---|---|---|
| Unknown route | 404 | application/problem+json | ✔ (`errors.http_exception_handler`) |
| Wrong method | 405 | problem+json, Allow covers all routes on the path plus HEAD | ✔ |
| HEAD on unknown route | 404 | problem+json header, empty body | ✔ (`head.py`) |
| Request validation | 422 | problem+json, `errors` without `input` | ✔ |
| Malformed JSON / undecodable body | 400 | problem+json (malformed-request) | ✔ |
| Wrong Content-Type | 415 | problem+json + `Accept` | ✔ |
| Missing / invalid bearer (incl. OAuth2PasswordBearer in a dependency) | 401 | problem+json + WWW-Authenticate | ✔ |
| HTTPException in a dependency (inactive 403, admin 403) | 403 | problem+json | ✔ |
| Body over limit, Content-Length declared (middleware) | 413 | problem+json + `Connection: close` | ✔ (`body_limit.py:160-176`) |
| Body over limit, chunked (middleware) | 413 | problem+json | ✔ |
| Rate limiter | 429 | problem+json + Retry-After + RateLimit headers | ✔ (Problem raised in a dependency, `rate_limit.py:331-334`) |
| Login count unavailable / token check unavailable | 503 | problem+json + Retry-After + `retry_after` | ✔ |
| Logout / reset revocation unavailable | 503 | problem+json, Retry-After header but **no `retry_after` member** | ~ (2C-16) |
| In-flight shed (middleware) | 503 | problem+json + Retry-After | ✔ (no CORS headers, 2C-16) |
| Unhandled exception / ResponseValidationError | 500 | problem+json, no detail, security headers, X-Request-ID | ✔ (`security_headers.server_error_handler`; no CORS headers) |
| DB unavailable | 503 | problem+json | ✔ (`server_error_handler` → `database_unavailable`) |
| **CORS preflight rejected** | **400** | **text/plain** "Disallowed CORS origin/headers/method" | **✘ (2C-08)** |
| OPTIONS without preflight headers | 405 | problem+json | ✔ |
| /health, /readyz degraded | 503 | application/json | deliberate (probe format) |
| uvicorn/h11 protocol errors (malformed HTTP) | 400 | text/plain from the server | outside the app (Info) |

---

## 6. Answers to the brief's questions

**Q1 GDPR:** see §4 and 2C-01, 2C-06, 2C-07, 2C-10 to 2C-13.

**Q2 Refresh tokens:**
- Simultaneous reuse: only one request wins. `consume_refresh_token` is GET+DEL inside a MULTI/EXEC pipeline (`redis_async.py:411-419`; redis-py async `pipeline()` defaults to `transaction=True`). The loser revokes the family, the winner's new token included (2C-09).
- Family revocation reads the members, then deletes them. It is not atomic against a concurrent `register_refresh_token`, but only a token being minted at that moment could slip through, and the reuse path revokes its own pre-registered token.
- Logout blacklists the access token by `jti` (immune to padding variants, `security.py:446-455`) and revokes the refresh family. Logout without a refresh token revokes all families and sets the watermark. Both fail closed (503).
- Password-change watermark: a float `iat` compared with a float watermark, so tokens issued in the same second are handled (`security.py:205-216`, `redis_async.py:177`). The gaps are the in-flight login (2C-02) and in-flight rotation (2C-03).
- Algorithm pinned to `algorithms=[settings.JWT_ALGORITHM]` (HS256/384/512 only); key length enforced in deployments; `exp`, `sub`, `type` and `jti` required on access tokens; no `aud`/`iss`/`kid` (2C-17); `jti` is a uuid4.
- Redis down: blacklist and watermark checks fail **closed** with 503 (`security.py:347-354`); rotation fails closed with 503 (`users.py:437-440`); the general rate limit fails **open**; the login failure count fails **closed** with 503.

**Q3 Passwords and accounts:**
- Policy: NFC normalisation, minimum 8 code points, maximum 72 UTF-8 bytes enforced at set time, blocklist plus context words.
- Login with a password over 72 bytes still pays one bcrypt and returns False (`security.py:55-63`), so it does not reveal the limit.
- Unknown and inactive users run bcrypt against a dummy hash at the real cost, and the 401 is identical. Registration answers 202 in both cases and hashes before the DB is asked.
- Lockout keys: casefold, NFC and strip, hashed. Account lookup is `lower(email) = normalize_email(input)`, and no case or Unicode variant reaches the same account under a different key. The distributed case is 2C-15.
- Reset token: single use through an HMAC fingerprint of the current hash, a 60-minute expiry, a conditional UPDATE against double use, and lockout counters cleared on success.
- There is no email-change flow and no authenticated password-change route.

**Q4 Rate limiting:**
- Key: per-user for a valid access token, otherwise the client address. X-Forwarded-For is believed only from `TRUSTED_PROXIES`, walked from the right; IPv6 is counted per /64. The auth routes are always per address.
- Counting is an atomic Lua INCR/PEXPIRE. Retry-After is ceil(TTL), at least 1. RateLimit and RateLimit-Policy follow draft-11.
- Fails open on Redis outage, except login. Bypass vectors: none found beyond the documented fixed-window 2× burst, the address-rich IPv6 case (2C-15) and the `unknown` bucket quirk (2C-19).

**Q5 Admin:** all admin routes are guarded (script-verified). Mass assignment is not possible: `RequestBody` has `extra="forbid"`, and `UserUpdate` exposes only `full_name`, `phone` and `marketing_consent` (role and is_admin are not settable). There is no IDOR: every address and order query filters by `user_id == current_user.id`, and a mismatch is a 404 (`users.py:620-626,740-745,801-806,848-853`; `orders.py:226-229,420-423,546-549,1044-1047`).

**Q6 RFC 9457:** see §5. The only app-level gap is the CORS preflight (2C-08).

**Q7 Headers and cookies:**
- Guest cart cookie: `__Host-` prefix, Secure, HttpOnly, SameSite=Lax, Path=/, Max-Age equal to the cart TTL (`cart.py:147-177`). There is no HMAC, but none is needed: the id is a 256-bit server-generated capability, and unknown ids are never adopted (`cart.py:84-97`).
- There is no refresh-token cookie (tokens travel in the body).
- HSTS, nosniff, DENY, no-referrer, CORP and COOP same-origin, and `default-src 'none'` CSP on every response, the 500 included. `no-store` on users, orders, cart, admin and any authenticated request.
- CORS: explicit origins with credentials, origins normalised at startup.

---

## 7. Checked and found sound

- **Token type separation**: `security.py:338,512-513,701,717`. Access requires `type=access` and a `jti`; reset requires `type=reset` and `pwd`; verify requires `type=verify_email`. `user_id_from_subject` (`security.py:151-168`) rejects non-canonical `sub` values.
- **Surrogate and NUL hardening**: `decode_token` maps UnicodeError to DecodeError (`security.py:187-202`); `text_input.py` rejects NUL and surrogates in query, path, form and body.
- **Logout blacklist keyed by `jti`**: `security.py:446-455` defeats the base64 "=" padding variant.
- **Refresh token registered before the old one is consumed**: `security.py:530-539`, so a Redis drop never leaves the client holding a spent token.
- **Reset single use and race**: `users.py:1062-1074`, a conditional UPDATE on the old hash.
- **Reset failure is atomic**: `users.py:1055-1060`. If Redis is down the watermark is refused before the hash changes, so the request is a 503 with nothing changed.
- **Login timing**: `users.py:351-371`, bcrypt always runs and inactive accounts get the same 401. The DB connection is released before bcrypt.
- **bcrypt concurrency bounded** per loop (`security.py:119-146`).
- **Registration enumeration**: `users.py:135-225` returns an identical 202, and the account-exists email goes to the owner only.
- **Anonymised-email collision attack blocked**: EmailStr refuses `*@deleted.invalid` (verified), so nobody can pre-register `deleted-user-N@deleted.invalid` to make the purge fail on the unique index.
- **Webhook payload redaction design**: an allow-list, idempotent, matching by PI, session and order id (compact and spaced JSON). Admin listing never exposes payloads (`schemas/webhook_event.py:6-9`).
- **Stripe metadata** carries only `order_id` (`orders.py:1166,1451`). No Stripe Customer is created.
- **Body limit**: declared and streamed both answer 413 problem+json, and the app is handed a disconnect so it never processes a truncated body (`body_limit.py:169-206`).
- **Error handling**: `ServerErrorMiddleware` handler gives a problem 500 with security headers; DB-down gives 503 (`security_headers.py:101-121`).
- **X-Request-ID echo** is validated against `[A-Za-z0-9._:-]{1,64}` (`request_context.py:144`).
- **Docs and OpenAPI** are disabled in production (`main.py:37,65-67`). (Staging still publishes them: Info.)
- **Deployment guard**: placeholder or short secrets, mock payments, BCRYPT < 12, DEBUG log level and non-https FRONTEND_URL all refuse to start (`config.py:503-549`). `DEBUG` and `RELOAD` are forced off outside development.
- **Retention and erasure tasks**: aware UTC, keyset batching with a commit per batch, idempotent, and only users who requested erasure are selected for the 1-day purge (`tasks.py:292-397`).
- **Async Redis**: a per-loop client and a blocking pool, so a full pool waits instead of failing open or closed spuriously (`redis_async.py:75-98`).
- **`mark_once` fails open** for email dedup only (a duplicate email at worst).
- **Email HTML escaping**: the only user-controlled value (full_name) is escaped (see 2C-05 for the None crash). Reset and verify links URL-encode the token onto `FRONTEND_URL`, which is https in production.

---

## 8. Open questions

1. Is a fixed `DATA_DELETION_GRACE_PERIOD_DAYS=1` intentional given chargeback windows (up to 120 days)? Legal input is needed on Art. 17(3)(e) retention of dispute evidence.
2. Which tax jurisdiction and retention rules apply to invoices? This decides whether an address snapshot on orders is mandatory (2C-06, 2C-10).
3. Is there an operational procedure for admins to deactivate users (DB-level)? If not, the retention purge (2C-13) needs its own dormancy criterion.
4. Does the production edge (nginx or CDN) add per-route limits for `/legal/data-request`, `/users/refresh` and `/users/register`? That would reduce 2C-04 and 2C-14.
5. Do clients refresh concurrently (multiple tabs, mobile background refresh)? If so, 2C-09 needs a grace window.
