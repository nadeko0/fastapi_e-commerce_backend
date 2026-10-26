# Release 4.0.0 Architecture Redesign and Migration Guide

Release 4.0.0 represents a major architectural redesign of the FastAPI e-commerce backend. First outlined in the API audit and announced in v3.3.0, this major release completely removes deprecated legacy interfaces, aligns response shapes and error representations with official Internet standards (RFC 9110, RFC 9457, IETF Idempotency-Key drafts), replaces custom response envelopes with direct resource representations (Google AIP-131), and enforces strict request validation to eliminate mass assignment risks and silent floating-point data corruption.

This document records the architectural drivers behind each change, the normative standards cited, the specific API audit findings addressed, and a comprehensive migration guide for client applications.

---

## Technical Rationale and Standards Alignment

### 1. Elimination of Deprecated Routes and URL Query Actions
- **Audit Findings Addressed:** F-07, F-24, F-31.
- **Change Description:**
  - Removed five legacy routes that were superseded in v3.3.0: `GET /users/verify-email/{token}`, `GET /users/password/reset/{token}`, `GET /users/data/export`, `POST /users/data/delete`, and `POST /users/consent`.
  - Removed action parameters from query strings on mutating routes: `POST /orders`, `PUT /orders/{id}/status`, `POST /cart/items`, and `PUT /cart/items/{id}` now require their full payload in the JSON request body.
  - Completely removed the `deprecation.py` middleware module and the associated `Deprecation`, `Sunset`, and `Link` headers.
  - Removed settings `PUBLIC_BASE_URL` and `GDPR_EXPORT_MAX_RECORDS`.
- **Engineering Rationale:** Under RFC 9110 §9.2.1, safe HTTP methods like GET must not produce state changes or trigger sensitive workflows; browser prefetch engines and web spiders can trigger them inadvertently. Furthermore, placing authentication tokens or password-reset tokens in URL paths exposes them to interception via web server access logs, proxy caches, browser histories, and outgoing Referer headers.
- **Standards Cited:** RFC 9110 §9.2.1 (Safe Methods), RFC 9110 §9.3.1 (GET), OWASP REST Security Cheat Sheet (Avoiding Secrets in URLs).

### 2. Direct Resource Representations Without Response Envelopes
- **Audit Findings Addressed:** F-16, F-31.
- **Change Description:**
  - Removed the custom response envelope `{success, data, error, timestamp, request_id}` from all successful operations. Successful endpoints return the domain resource model directly.
  - Collection endpoints return standardized page objects:
    ```json
    {
      "items": [...],
      "total": 42,
      "page": 1,
      "size": 20,
      "has_more": true
    }
    ```
  - `GET /users/orders` now returns a fully paginated page object. `GET /orders/{id}/payments` returns `{"items": [...]}`. Confirmation-only routes return `{"message": "..."}`.
  - The request correlation identifier is now carried exclusively in the standard `X-Request-ID` HTTP response header.
- **Engineering Rationale:** Envelope wrappers add overhead and duplicate standard HTTP protocol features. The HTTP status code communicates outcome, while caching proxies cannot inspect enveloped headers. Returning top-level JSON objects for collections preserves forward compatibility, allowing metadata expansion without breaking clients expecting bare arrays.
- **Standards Cited:** Google AIP-131 (Resource-Oriented APIs: "The response message must be the resource itself"), Zalando RESTful API Guidelines (#110 Top-Level JSON Object, #248 Pagination), Stripe API Reference.

### 3. Standardized Problem Details for HTTP APIs (RFC 9457)
- **Audit Findings Addressed:** F-05, F-06, F-21, F-28.
- **Change Description:**
  - Replaced five disparate error response structures (including plain text 500 responses) with RFC 9457 Problem Details (`Content-Type: application/problem+json`).
  - Standard error envelope:
    ```json
    {
      "type": "https://github.com/nadeko0/fastapi_e-commerce_backend/problems/validation-error",
      "title": "Validation Error",
      "status": 422,
      "detail": "Request validation failed",
      "instance": "/api/v1/orders",
      "request_id": "req_1234567890",
      "errors": [
        {"type": "extra_forbidden", "loc": ["body", "unexpected_field"], "msg": "Extra inputs are not permitted"}
      ]
    }
    ```
  - Standard problem slugs under `https://github.com/nadeko0/fastapi_e-commerce_backend/problems/`:
    `validation-error`, `malformed-request`, `unsupported-media-type`, `body-too-large`, `rate-limited`, `service-overloaded`, `idempotency-key-reused`, `idempotency-key-in-use`, `idempotency-key-missing`, `payment-declined`, `insufficient-stock`, and `account-inactive`. Errors that carry no additional semantics use `about:blank`.
  - Rate-limited (429) and overloaded (503) problem responses include `retry_after`.
  - Internal server errors (500) return a generic problem document without internal exception strings or tracebacks, recording error details solely in application logs.
- **Engineering Rationale:** Clients require deterministic, machine-readable error contracts to distinguish business rejections from syntax faults. Opaque 500 error bodies prevent information disclosure and infrastructure fingerprinting.
- **Standards Cited:** RFC 9457 (Problem Details for HTTP APIs), RFC 9110 §15.6.1 (500 Internal Server Error), OWASP Top 10 (A05:2021 Security Misconfiguration).

### 4. HTTP Status Code Harmonization (RFC 9110)
- **Audit Findings Addressed:** F-03, F-06, F-10, F-11, F-12, F-25, F-26, F-30.
- **Change Description:**
  - **201 Created with Location:** Returned on resource creation (`POST /orders`, `POST /users/addresses`, `POST /admin/categories`, `POST /admin/products`, `POST /admin/products/{id}/variants`). Replaying a request with the same `Idempotency-Key` returns the identical 201 response.
  - **202 Accepted:** Returned by `POST /users/register` (identical response regardless of whether the email is new or pre-existing) and manual background reconciliation endpoints.
  - **204 No Content:** Returned on successful deletion of addresses and products (empty body). A subsequent deletion of the same address returns 404 Not Found.
  - **409 Conflict:** Returned instead of 400 for conflicts with existing resource state (invalid status transitions, attempting to pay or cancel an order in an incompatible state, insufficient product inventory, unverified email, or an active payment attempt in flight).
  - **403 Forbidden (`account-inactive`):** Returned instead of 400 when an authenticated account has been deactivated.
  - **422 Unprocessable Content (`payment-declined`):** Returned instead of 402 for declined credit cards (RFC 9110 reserves 402 for digital cash schemes).
  - **400 Bad Request (`malformed-request`):** Returned instead of 422 for syntactically invalid JSON or missing `Idempotency-Key` headers.
  - **415 Unsupported Media Type:** Returned with an `Accept` header when requests supply non-JSON content types.
- **Engineering Rationale:** Adhering strictly to standard HTTP status codes ensures seamless integration with API gateways, caching proxies, and client SDKs. Client syntax bugs (400) are distinguished from semantic state collisions (409).
- **Standards Cited:** RFC 9110 §9.3.3 (POST), §15.3.2 (201 Created), §15.3.3 (202 Accepted), §15.3.5 (204 No Content), §15.5.1 (400 Bad Request), §15.5.4 (403 Forbidden), §15.5.10 (409 Conflict), §15.5.16 (415 Unsupported Media Type), RFC 6750 §3.1, draft-ietf-httpapi-idempotency-key-header-08.

### 5. Routing Additions: Address Detail and Custom Integer Path Converters
- **Change Description:**
  - Added route `GET /users/addresses/{id}` returning a single address resource (returning 404 for nonexistent, deleted, or unowned addresses).
  - Implemented custom path converter `sint` (signed integer) for address routes.
- **Engineering Rationale:** Adding `Location: /api/v1/users/addresses/{id}` on 201 creation required an explicit GET endpoint. Introducing `{id:sint}` resolved an ambiguity where `GET /users/addresses/default` incorrectly matched `{address_id}` as a parameter, yielding 422 instead of 405 Method Not Allowed (`Allow: POST`). Standard `int` converters reject negative numbers (yielding 404), whereas `sint` captures negative integers to preserve the 422 input validation contract.
- **Standards Cited:** RFC 9110 §15.5.6 (405 Method Not Allowed).

### 6. Strict Input Validation and Precision Guarantees
- **Audit Findings Addressed:** F-18, F-21.
- **Change Description:**
  - Enforced `extra="forbid"` on all 27 request body models via base class `RequestBody`. Unrecognized properties in request payloads trigger a 422 validation error.
  - Monetary fields require decimal string formatting (such as `"24.99"`). Raw JSON numbers (`24.99`), booleans (`true`), or scientific notation (`"1e2"`) are rejected with 422.
  - Date and time filters (`start_date`, `end_date`) require timezone offsets (`AwareDatetime`).
  - Administrative statistics endpoints return revenue figures as rounded decimal strings.
- **Engineering Rationale:** Unvalidated payload properties allow mass assignment attacks and mask client typos. Accepting binary floating-point numbers causes precision loss (e.g. 0.1 + 0.2 != 0.3); string parsing ensures exact `Decimal` precision throughout pricing pipelines.
- **Standards Cited:** RFC 8259 §6 (Numbers), OWASP API Security Top 10 (API6:2023 Mass Assignment), ISO 8601.

### 7. Decoupled Email Delivery and Frontend Flows
- **Change Description:**
  - Verification and password-reset emails direct recipients to the frontend application: `FRONTEND_URL/verify-email?token=...` and `FRONTEND_URL/reset-password?token=...`.
  - Added `POST /users/password/reset/confirm` accepting `{token, new_password}` in the JSON body.
- **Engineering Rationale:** Email clients always initiate GET requests upon link activation. Routing clicks through the frontend application enables client-side validation, sets `Referrer-Policy: no-referrer` to protect tokens, and submits actions via POST with body payloads.
- **Standards Cited:** OWASP Forgot Password Cheat Sheet.

### 8. Operational Hardening: Twelve-Factor Configuration and Logging
- **Change Description:**
  - `ENVIRONMENT` is required at startup and must be one of `development`, `test`, `staging`, or `production`.
  - `LOG_FILE` defaults to empty; logs stream unbuffered to standard output.
- **Engineering Rationale:** Security posture must never rely on silent defaults. In containerized cloud environments, log aggregation belongs to container orchestrators, not local filesystems.
- **Standards Cited:** The Twelve-Factor App (III. Config, XI. Logs).

---

## Client Migration Reference

The table below summarizes the breaking changes between v3.x and v4.0.0, along with the required adjustments for client applications:

| Integration Area | Release 3.x Contract | Release 4.0.0 Modern Contract | Client Remediation Required |
|---|---|---|---|
| **Response Format** | Wrapped in `{success: true, data: ...}` | Raw resource object returned directly | Remove `.data` unwrapping; inspect root response object |
| **List Responses** | Inconsistent formats; some arrays | Page object: `{items, total, page, size, has_more}` | Read items from `.items`; use `.has_more` for pagination |
| **Error Handling** | Inconsistent formats, plain text on 500 | RFC 9457 `application/problem+json` | Read `.type`, `.status`, `.detail`, and `.errors[]` |
| **Resource Creation** | 200 OK | 201 Created with `Location` header | Update assertions to expect 201; inspect `Location` header |
| **Resource Deletion** | 200 OK with body | 204 No Content (empty body) | Handle 204 responses without attempting body JSON parsing |
| **State Conflicts** | 400 Bad Request | 409 Conflict | Update error handling to catch 409 for invalid transitions |
| **Declined Cards** | 402 Payment Required | 422 Unprocessable Content (`payment-declined`) | Catch 422 with type URI `.../payment-declined` |
| **Malformed JSON** | 422 Unprocessable Entity | 400 Bad Request (`malformed-request`) | Treat invalid JSON as 400 syntax error |
| **Money Input** | Accepted JSON floats (`19.99`) | String decimal required (`"19.99"`) | Serialize all prices and amounts as two-decimal strings |
| **Unexpected Fields** | Silently ignored by server | 422 Unprocessable Content (`extra_forbidden`) | Audit client payloads; remove undeclared or misspelled fields |
| **Password Reset** | `POST /users/password/reset/{token}` (path) | `POST /users/password/reset/confirm` (body) | Send `{token, new_password}` in JSON request body |
| **Email Verification** | `GET /users/verify-email/{token}` | `POST /users/verify-email` with JSON body | Send `{token}` in JSON request body |
| **User Registration** | 200 OK or 409 Conflict | 202 Accepted (uniform response) | Expect 202; handle registration message asynchronously |
| **Idempotency Keys** | Missing key returned 422 | Missing key returns 400 (`idempotency-key-missing`) | Ensure `Idempotency-Key` header (8-255 chars) is sent |

---

See also: [Remediated Defects across Releases 4.0.x](fixes-4.0.x.md), [Final Test Campaign Overview](final-test-campaign.md), [Pre-3.0 Security Review Record](security-review-3.0.md), [3.x Load Testing and Reliability Findings](load-testing-3x.md).
