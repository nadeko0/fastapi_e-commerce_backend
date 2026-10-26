# Stage T3: Contract and Fuzz Testing (Schemathesis)

An API schema is only as honest as the fuzzer checking it. Stage T3 uses Schemathesis to generate thousands of arbitrary, malformed, and adversarial inputs against live endpoints, verifying that every response conforms to OpenAPI contracts without triggering unhandled 500 errors.

## Purpose and Exit Criterion

The purpose of Stage T3 was to perform automated contract testing and property-based fuzz testing of all live API endpoints against the published OpenAPI 3.1.0 specification, covering positive, negative, and stateful multi-step interactions.

Exit criteria:
- Zero 5xx server errors across all test phases, including aggressive fuzzing.
- 100% schema conformance: status codes, headers, and response payloads must strictly match the OpenAPI specification.
- Stateful workflows must execute without unhandled exceptions or state corruptions.

## Environment

- **Target:** Staging stack running release candidate v4.0.0 (commit `3af3e68`) via Caddy HTTPS (`https://localhost:8443` with `caddy-root.crt`).
- **Tooling:** Schemathesis 4.30.1 executed via `uvx schemathesis==4.30.1` (seed 20261024, 4 workers). Python test tooling: pytest 9.1.1, requests.
- **Specification:** OpenAPI 3.1.0 document served at `/api/v1/openapi.json` (54 paths, 67 operations, version 4.0.0). No explicit `links` in spec; Schemathesis inferred 115 links autonomously; 24 explicit links were injected for stateful phase d.
- **Configuration adjustments:** Rate limits temporarily elevated in `.env` (to 100000/minute) for fuzzing runs and verified restored to production defaults immediately afterwards.

## Method and Commands

Schemathesis was run via generic wrapper scripts against the live API:

```bash
uvx schemathesis==4.30.1 run https://localhost:8443/api/v1/openapi.json \
  --tls-verify <work dir>/caddy-root.crt \
  --checks all \
  --seed 20261024 \
  --workers 4 \
  --report junit,har,ndjson \
  --report-dir <work dir>/out/<run_name> \
  --no-color <extra_args>
```

Executed test suites:
- **Run a1 (Anonymous):** `--max-examples 50` across all operations.
- **Run a2 (Catalog Fuzzing):** `--max-examples 200 --include-path-regex '^/api/v1/(products|categories)'`.
- **Run b (Customer):** Customer JWT authentication hook, `--max-examples 50`.
- **Run c (Admin):** Admin JWT authentication hook, `--max-examples 50`.
- **Run d (Stateful Links):** 24 explicit OpenAPI links injected, `--phases stateful --max-time 900`.
- **Run e (Golden Path Flow):** Deterministic 33-step contract flow verifying catalog, cart, order, payment, refund, and admin operations.

## Results

Execution results on release candidate v4.0.0:

| Run Name | Scope | Test Cases | Stateful Scenarios | Unique Failures | 5xx Errors | Outcome |
|---|---|---|---|---|---|---|
| a1-anon | Anonymous access across all endpoints | 5450 | 90 | 14 | 0 | PASS |
| a2-catalog | Deep fuzzing of product and category routes | 3076 | 464 | 2 | 28 | FAIL (F1) |
| b-customer | Authenticated customer endpoints | 5112 | 157 | 23 | 2 | FAIL (F1) |
| c-admin | Authenticated admin management endpoints | 5469 | 280 | 32 | 6 | FAIL (F1, F2) |
| d-stateful-links | Multi-step stateful workflows (15 min run) | 11575 | 2516 | 2 | 50 | FAIL (F1) |
| e-flow | Deterministic 33-step end-to-end golden path | 33 | - | 0 | 0 | PASS |

Overall Stage Verdict: **FAIL on v4.0.0**. The test campaign failed the zero-5xx criterion due to two distinct defects: NUL character handling (F1) and lone Unicode surrogates in authentication tokens (F2). All responses conformed to schemas on valid inputs, and the golden path flow passed with 0 errors.

## Findings and Remediations

- **F1 (High Severity): NUL character (`\u0000`) in string parameters causes 500 error.**
  - *Cause:* PostgreSQL text columns cannot store NUL characters and raise an unhandled `DataError`. Affected search queries, registration (full_name), login (username), address fields, profile updates, and admin SKU.
  - *Reproducer:* `POST /api/v1/users/register` with `"full_name": "Test\u0000User"` returned 500 Internal Server Error.
  - *Remediation:* Implemented centralized validation at two levels: (1) `RequestBody` base model (`model_validator(mode="before")`) recursively scanning all JSON bodies, keys, and nested dictionaries; (2) `reject_unusable_text` dependency in `app/core/text_input.py` intercepting query, path, and form parameters. NUL inputs return 422 `validation-error` with type `nul_character`. Shipped in v4.0.1 (fix X4).
- **F2 (Medium Severity): Lone Unicode surrogates in tokens trigger 500 error.**
  - *Cause:* An unauthenticated request containing a lone surrogate (e.g. `"a\ud800b"`) caused PyJWT to raise `UnicodeEncodeError`, returning 500 before authentication validation.
  - *Reproducer:* `POST /api/v1/users/refresh` with `{"refresh_token": "a\ud800b"}` returned 500.
  - *Remediation:* Wrapped token decoding in `app/core/security.py` to catch `UnicodeError` and return standard 401 `invalid_token`. Shipped in v4.0.1 (fix X4).
- **F3 (Low Severity): Schema discrepancy on enforced business rules.**
  - *Cause:* Approximately 20 operations enforced validation rules absent from the OpenAPI document (e.g. `images` minItems 1, `parent_id` > 0, non-empty profile update, telephone regex, monetary decimal string pattern).
  - *Remediation:* Updated schema models to document required constraints. Shipped in v4.0.1 (fix X4).
- **F5 (Low Severity / Deferred): Lax type coercion in Pydantic.**
  - *Cause:* Inputs such as `marketing_consent: 0` were coerced to boolean `False`, and `start_date=0.5` was parsed as numeric Unix epoch.
  - *Decision:* Deferred as a candidate change, as strict type enforcement represents a breaking API change under SemVer rules.
- **F6 (Low Severity / RFC 9110 §15.5.6): Incomplete `Allow` header in 405 responses.**
  - *Cause:* On 8 paths where HTTP methods are split across multiple route definitions, Starlette collected methods only from the first matching route (e.g. `/users/me` returned `Allow: GET, HEAD` omitting PUT and PATCH).
  - *Remediation:* Custom 405 exception handler aggregates allowed methods across all matching routes. Shipped in v4.0.1 (fix X4).

## Limitations

- Fuzz-generated payloads rarely satisfied cross-field relational dependencies (e.g. valid category ID plus populated product image array), meaning autonomous stateful chains rarely completed orders without explicit guidance. The 33-step deterministic golden-path flow (e-flow) was used to guarantee end-to-end multi-entity coverage.
- Stateful run d was time-capped at 900 seconds.

---

See also: [fixes-4.0.x.md](fixes-4.0.x.md), [final-test-campaign.md](final-test-campaign.md), [T6-dast-owasp.md](T6-dast-owasp.md), [../API.md](../API.md).
