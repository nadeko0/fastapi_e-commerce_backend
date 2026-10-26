# Phase 2b: protocol and standards torture

Module: `tests/torture_suite/test_protocol_rfc_torture.py` (692 collected items, ruff clean).
Scratch outputs: `.cloud_torture/scratch_2b/final_sqlite.txt` and `final_pg.txt` (full `-v --hypothesis-show-statistics` runs).

The standards covered are NIST SP 800-63B with Unicode normalisation (UAX #15), RFC 7396 (JSON Merge Patch), RFC 5789 (PATCH), RFC 9110 §8.8.3 and §13 (ETags and conditional requests), RFC 8259 (JSON), RFC 9457 (Problem Details), and the project's own money and pagination contract.

## How it runs

```
uv run pytest tests/torture_suite/test_protocol_rfc_torture.py -p no:cacheprovider -v --hypothesis-show-statistics
TEST_DATABASE_URL=postgresql+psycopg://torture:torture@localhost:5432/postgres uv run pytest ...   # PostgreSQL 16
```

* Fixture `api` is a TestClient on the real app with `raise_server_exceptions=False`. A 500 therefore comes back as the response a client would get, and its problem document is checked too. It also stubs the welcome and account-exists e-mails.
* Fixture `world` creates, through `payment_support.Shop`, one admin, one category with a product and a variant, and one verified buyer with an address.
* Every response a test receives goes through `observe()` / `no_5xx()`. These are the RFC 9457 helpers:
  * `Content-Type: application/problem+json`, and the body is a JSON object.
  * `type` is a URI reference by RFC 3986 characters: either `about:blank` or an absolute http(s) URI.
  * `title` is a non-empty string.
  * `status` is an `int` (a bool is rejected) equal to the HTTP status.
  * `detail` and `instance` are strings when present; `instance` has no query string.
  * No member of `errors[]` contains `input`.
  * There is no leakage: `Traceback`, `sqlalchemy`, `psycopg`, `sqlite3`, `asyncpg`, `pydantic_core`, `File "`, `line N, in`, `.py`, `/home/`, `site-packages`, SQL keywords (`SELECT`/`INSERT INTO`/`UPDATE x SET`/`WHERE`/`FROM t`), exception class names, or `Circular reference` / `Error serializing`.
  * A 304 has an empty body and carries an ETag.
  * Optional `secrets=`: the password or other secret sent never appears in the response.

## Hypothesis settings

Each property test has its own `settings(max_examples=scaled_examples(N), deadline=None)`. `function_scoped_fixture` and `too_slow` are suppressed. The `ci`/`explore` profile doubles the budget through `conftest.scaled_examples`.

| Property test | Examples (dev profile) |
|---|---|
| test_password_round_trips_and_canonical_equivalents_log_in (bcrypt, register plus 1-3 logins) | 60 (+2 explicit) |
| test_passwords_differing_after_byte_72_never_collide | 40 |
| test_an_account_logs_in_with_the_exact_email_it_registered_with | 80 (+1 explicit) |
| test_one_mailbox_one_account_and_no_false_merges | 80 |
| test_unknown_members_are_422_and_never_applied (x4 endpoints) | 60 each |
| test_merge_patch_of_a_free_form_object_follows_rfc7396 | 60 (+1 explicit) |
| test_deeply_nested_free_form_json_is_never_a_500 | 40 |
| test_if_match_only_passes_for_star_or_the_current_strong_tag | 150 |
| test_money_fuzz | 250 |
| test_price_filters_never_500 | 250 (+2 explicit) |
| test_pagination_fuzz | 250 (+1 explicit) |
| test_ids_in_paths_never_overflow_the_database | 200 (+2 explicit) |
| test_any_body_gets_a_success_or_a_strict_problem | 250 |

Runtime is about 31 s on SQLite and about 69 s on PostgreSQL, well under the 10-minute budget.

## Test inventory

**1. NIST SP 800-63B, Unicode and e-mail**
* Password property tests:
  * `test_password_round_trips_and_canonical_equivalents_log_in`: a password registered as typed logs in as typed, as NFC and as NFD. The generator uses arbitrary UTF-8 plus a pool of ZWJ, ZWSP, BOM, RTL/LTR overrides, combining marks, U+212B, U+212A, ligatures, fullwidth, Hangul jamo, emoji ZWJ sequences, NBSP, U+3000, İ, ß and Σ.
  * `test_passwords_differing_after_byte_72_never_collide` (property).
* Parametrised password tables:
  * `test_password_normalisation_policy` (14 cases): canonical equivalents log in; NFKC-only equivalents and invisible characters do not.
  * `test_password_length_boundaries` (19 cases) covers:
    * ASCII lengths 7, 8, 63, 64, 65, 72, 73, 128 and 129;
    * 18 vs 19 four-byte emoji and 24 vs 25 three-byte euro signs;
    * 36 precomposed é;
    * 36 vs 37 decomposed `é`, which is more than 72 bytes as typed but at most 72 bytes in NFC;
    * a decomposed password that shrinks below 8 characters in NFC;
    * a stack of 30 combining marks;
    * 64 é (128 bytes).
  * `test_blocklist_compares_the_nfc_casefolded_password`: a policy record.
* Single-case password tests:
  * `test_password_that_is_not_unicode_text_is_422`: raw JSON with `\ud800`, `\udfff`, a reversed surrogate pair and `\u0000`.
  * `test_password_as_a_valid_surrogate_pair_escape_round_trips`.
  * `test_login_with_nul_in_the_password_is_422_not_500`.
  * `test_login_with_a_password_far_beyond_72_bytes_is_401`.
* E-mail tests:
  * `test_an_account_logs_in_with_the_exact_email_it_registered_with` (property).
  * `test_one_mailbox_one_account_and_no_false_merges` (property: case, NFC/NFD, padding and IDNA variants; checks DB row counts).
  * `test_bug_login_does_not_normalise_email_like_registration`.

**2. Merge Patch, PATCH media types and ETags**
* Merge Patch behaviour:
  * `test_explicit_null_on_every_patch_field_follows_the_schema` (24 cases). It enumerates every field of `UserUpdate`, `AddressUpdate`, `ProductUpdate` and `ProductVariantUpdate`. Each field's nullability is read from the model's own JSON Schema, which is the OpenAPI contract.
  * `test_a_patch_document_that_is_not_an_object_is_refused`: array, string, number, null, true, `[{}]`, a JSON Patch array; 4 endpoints.
  * `test_unknown_members_are_422_and_never_applied` (property).
  * `test_merge_patch_of_a_free_form_object_follows_rfc7396`: property against the RFC 7396 §2 reference algorithm.
  * `test_bug_merge_patch_replaces_nested_objects_instead_of_merging`.
  * `test_deeply_nested_free_form_json_is_never_a_500` (property, depths 1 to 900).
  * `test_bug_deeply_nested_characteristic_is_a_500_and_breaks_the_public_catalog`.
  * `test_bug_non_finite_numbers_in_free_form_json`.
* PATCH media types:
  * `test_patch_media_types` (7 types x 4 endpoints): application/json, merge-patch+json, `; charset=utf-8`, mixed case, text/plain, form, xml. Each 415 must carry `Accept` naming merge-patch.
  * `test_bug_json_patch_media_type_is_applied_as_a_merge_patch`.
* ETags and conditional requests:
  * `test_if_match_table` (19 cases x product/variant) covers:
    * `*`, the exact tag, a padded tag, and a list containing the current tag;
    * a weak tag (must give 412), an unquoted tag, a stale tag, a stale weak list;
    * an empty field, `" , ,"`, a near-miss tag, quotes inside a tag, a truncated tag;
    * `bogus*`, `"stale*`, `W/*`, `"stale", *`.
    * On every 412 the response must carry the current ETag and the resource must be unchanged.
  * `test_if_match_only_passes_for_star_or_the_current_strong_tag` (property).
  * `test_bug_if_match_wildcard_inside_a_malformed_field_is_honoured`.
  * `test_bug_if_match_is_ignored_on_profile_and_address_patch`.
  * `test_no_if_match_is_an_unconditional_update_not_428`.
  * `test_if_none_match_on_get` (9 x 2): weak comparison, lists, `*`, stale, unquoted, empty, prefix. Each is checked twice for the product, once on the DB path and once on the Redis path.
  * `test_if_none_match_star_on_a_missing_resource_is_404`.

**3. Money and pagination**
* `test_money_input_matches_the_documented_contract` (49 values x 4 writes: product create/patch, variant create/patch). The values include:
  * exponent forms `1e-2`, `1E+2`;
  * `-0`, `-0.00`, leading and trailing whitespace, `0.0000001`;
  * NaN, sNaN, Infinity, inf, `+1`, `1_000`, `0x10`, a 400-digit integer;
  * fullwidth, Arabic-Indic, mathematical-bold and Bengali digits;
  * JSON numbers and bools, `[]`, `{}`, null.
  * The oracle is the published OpenAPI pattern, matched with ASCII digits.
* `test_money_as_a_raw_json_number_literal_is_422` covers `1e400`, NaN and the other literals as raw JSON numbers.
* `test_money_fuzz` (property).
* `test_bug_money_accepts_non_ascii_digits`.
* `test_price_filters_never_500` (property; huge or tiny exponents, arbitrary text).
* `test_bug_price_filter_beyond_postgres_numeric_is_500`.
* Pagination bounds over 8 list endpoints: products, search, categories, addresses, my orders, admin orders, reconciliation discrepancies, webhook events.
  * `test_page_bounds` (21 values): 0, -1, -2^31, 1, 2, 2^31-1, 2^31, 2^63-1, 2^63, 10^100, `1.5`, `1e3`, `abc`, `""`, `１`, NaN, `0x10`, and the ambiguous `1.0`, `" 1"`, `+1`, `"1 "`.
  * `test_size_bounds` (10 values).
* `test_pagination_fuzz`.
* `test_ids_in_paths_never_overflow_the_database`: ids from -2^80 to 2^80 plus spelling variants, on 6 id routes.

**4. RFC 9457**
* The `observe()` helper is applied to every response above.
* `test_any_body_gets_a_success_or_a_strict_problem` (property): random bytes and random JSON, including NaN floats, sent as application/json, merge-patch or text/plain to register, PATCH /me, PATCH variant and POST address.
* `test_framework_errors_are_strict_problems`: Starlette 404, 405, 401 and 422.

## Results

| Back end | Result | Time |
|---|---|---|
| SQLite | **54 failed, 638 passed** | 31.5 s |
| PostgreSQL 16 | **59 failed, 633 passed** | 68.8 s |

The two back ends fail on exactly the same tests, plus two that fail only on PostgreSQL (4 + 1 items): `test_bug_price_filter_beyond_postgres_numeric_is_500` and `test_price_filters_never_500`. Every failing item traces to one of bugs B1–B9 below; there are no harness failures.

Failing items per test (PostgreSQL):

```
 1 test_an_account_logs_in_with_the_exact_email_it_registered_with     (B1)
 3 test_bug_login_does_not_normalise_email_like_registration           (B1)
 4 test_bug_json_patch_media_type_is_applied_as_a_merge_patch          (B7)
 1 test_merge_patch_of_a_free_form_object_follows_rfc7396              (B6)
 1 test_bug_merge_patch_replaces_nested_objects_instead_of_merging     (B6)
 1 test_deeply_nested_free_form_json_is_never_a_500                    (B2)
 1 test_bug_deeply_nested_characteristic_is_a_500_and_breaks_the_public_catalog (B2)
10 test_bug_non_finite_numbers_in_free_form_json                       (B3)
 8 test_if_match_table[15..18]                                         (B4)
 1 test_if_match_only_passes_for_star_or_the_current_strong_tag        (B4)
 1 test_bug_if_match_wildcard_inside_a_malformed_field_is_honoured     (B4)
 2 test_bug_if_match_is_ignored_on_profile_and_address_patch           (B8)
16 test_money_input_matches_the_documented_contract (4 unicode-digit values x 4 writes) (B5)
 1 test_money_fuzz   (falsifying: sent='߀', U+07C0 NKO DIGIT ZERO)    (B5)
 3 test_bug_money_accepts_non_ascii_digits                             (B5)
 4 test_bug_price_filter_beyond_postgres_numeric_is_500   [PG only]    (B9)
 1 test_price_filters_never_500                           [PG only]    (B9)
```

Two harness bugs were found and fixed during iteration:
* httpx refuses to serialise a float `inf`. That case moved to a raw-body test.
* A random 6-hex tag shared between the e-mail local part and the password tripped the context-word blocklist. The password is now independent of the tag.

A probe mistake (a list built before the loop that refreshed the ETag) briefly suggested an ETag inconsistency. It was not real: GET and PUT/PATCH ETags agree, on both the DB path and the Redis path.

## Suspected bugs

### B1: login does not normalise the e-mail the way registration does (both back ends)

* **Tests:** `test_bug_login_does_not_normalise_email_like_registration[idna-ace-domain|fullwidth-domain|ideographic-full-stop]` and `test_an_account_logs_in_with_the_exact_email_it_registered_with`.
* **Where:** registration validates with `EmailStr` (`app/schemas/user.py:104-108`, `normalise_email` then email-validator). email-validator applies IDNA/UTS #46 to the domain and stores that form. `_user_by_email` (`app/api/v1/users.py:118-126`), used by `/login`, only applies `normalize_email()` (`app/schemas/user.py:37`: NFC + lower()).
* **Reproducer:**
  ```
  POST /api/v1/users/register {"email":"jane@xn--mnchen-3ya.de","password":"Correct-Horse-9",...}  -> 202 (stored as jane@münchen.de)
  POST /api/v1/users/login  username=jane@xn--mnchen-3ya.de&password=Correct-Horse-9            -> 401 "Incorrect email or password"
  POST /api/v1/users/login  username=jane@münchen.de ...                                            -> 200
  ```
  The same happens for `jane@ｅｘａｍｐｌｅ.com` (stored `jane@example.com`) and `jane@example。com`.
* **Standard:** RFC 5890/5891, where an A-label and its U-label are the same domain, and UTS #46. It is also the project's own intent in the `normalize_email` docstring: "the one spelling of an email this application stores and compares".
* **Why it is not a test bug:** the account exists, and the very string accepted at registration fails at login. Password reset uses `EmailStr` and finds the account, so the two paths disagree.
* **Fix:** route every lookup through the same canonicaliser.
  ```diff
  --- a/app/schemas/user.py
  +def canonical_email(email: str) -> str:
  +    """What registration stores: normalize_email, then email-validator's form."""
  +    composed = normalize_email(email)
  +    try:
  +        return validate_email(composed, check_deliverability=False).normalized
  +    except EmailNotValidError:
  +        return composed
  --- a/app/api/v1/users.py:124
  -        select(User).where(func.lower(User.email) == normalize_email(email)).limit(1)
  +        select(User).where(func.lower(User.email) == canonical_email(email)).limit(1)
  ```
  The login failure counter (`check_login_attempt`) should be keyed on the same canonical form.

### B2: a deeply nested free-form JSON value is a 500 and permanently breaks the public catalog (both back ends)

* **Tests:** `test_bug_deeply_nested_characteristic_is_a_500_and_breaks_the_public_catalog` and `test_deeply_nested_free_form_json_is_never_a_500`. Hypothesis shrank the failure to `depth=255, kind='dict'`.
* **Where:**
  * `RequestBody` (`app/schemas/common.py:63-100`) has no depth bound for `dict[str, Any]` fields: `ProductVariant.attributes` and `ProductCharacteristic.value`.
  * pydantic-core's serializer stops at depth 255 with `PydanticSerializationError: Circular reference detected (depth exceeded)`.
  * For a variant update this is raised in `etag_for` (`app/core/etag.py:25`, called from `app/api/v1/admin.py:842`) after the flush.
  * For `create_product` / `create_product_variant` the row is committed (`app/api/v1/admin.py:212`, `:680`) before FastAPI serialises the response, so the 500 leaves the row in place.
  * From then on every `GET /products`, `/products/search` and `/products/{id}`, and the admin variant list, is a 500 for every visitor.
* **Reproducer:**
  ```
  POST /api/v1/admin/products {"name":"Deep product",...,"characteristics":{"c":{"name":"c","value":[[[...300 levels...]]]}}}
    -> 500 {"type":"about:blank","title":"Internal Server Error","status":500,...}   (row committed)
  GET /api/v1/products  -> 500     GET /api/v1/products/search?query=deep -> 500
  PATCH /api/v1/admin/products/1/variants/1 {"attributes":{"k":{"a":{"a":...255 levels}}}} -> 500
  ```
* **Standard:** RFC 9110 §15.6.1 (500 for a client-controlled input), and RFC 8259 §9, which lets a parser bound nesting depth: the bound should answer 4xx. It is also an availability defect, a stored denial of service on unauthenticated routes.
* **Fix:** bound depth where unusable text is already refused. The walk is already iterative, so add a depth counter.
  ```diff
  --- a/app/core/text_input.py (first_unusable_text)
  +MAX_JSON_DEPTH = 32
  -    pending = [(value, loc)]
  +    pending = [(value, loc, 0)]
       while pending:
  -        item, where = pending.pop()
  +        item, where, depth = pending.pop()
  +        if depth > MAX_JSON_DEPTH:
  +            return {"type": "json_too_deep", "loc": where, "msg": f"Nested deeper than {MAX_JSON_DEPTH} levels"}
  ```
  Children are pushed with `depth + 1`. Also commit only after the response model has been built and serialised: build `result.model_dump_json()` before `db.commit()`.

### B3: NaN, Infinity and 1e400 in free-form JSON give a 500 on PostgreSQL and are silently nulled on SQLite

* **Test:** `test_bug_non_finite_numbers_in_free_form_json` (5 literals x {variant `attributes`, product `characteristics` value}).
* **Where:** Starlette's `Request.json()` is `json.loads`, which accepts `NaN`, `Infinity` and `-Infinity` and turns `1e400` into `inf`. `RequestBody` lets floats through in `Any` fields.
  * **PostgreSQL:** JSONB refuses it, giving `500 "A database error occurred"`.
  * **SQLite:** the value is stored and served back as `null`, so data is silently altered.
* **Reproducer:**
  ```
  PATCH /api/v1/admin/products/1/variants/1  Content-Type: application/json  {"attributes": {"w": NaN}}
    PG: 500 {"type":"about:blank","title":"Internal Server Error","status":500,"detail":"A database error occurred",...}
    SQLite: 200 {"attributes":{"w":null},...}
  ```
* **Standard:** RFC 8259 §6 says "Numeric values that cannot be represented in the grammar below (such as Infinity and NaN) are not permitted". RFC 7159/8259 §9 lets a parser limit range: the answer should be a 400/422, not a 500 or a silent change.
* **Fix:** in `first_unusable_text`, treat `float` values with `not math.isfinite(item)` as a 422 (`type: "non_finite_number"`). Alternatively, parse bodies with `parse_constant` raising, which makes them a 400 malformed request.

### B4: an If-Match / If-None-Match field containing `*` anywhere is treated as `*` (both back ends)

* **Tests:**
  * `test_if_match_table[15..18]` (`bogus*`, `"stale*`, `W/*`, `"stale", *`);
  * `test_if_match_only_passes_for_star_or_the_current_strong_tag`, where Hypothesis found `W/"<current>",*`;
  * `test_bug_if_match_wildcard_inside_a_malformed_field_is_honoured`.
* **Where:** `app/core/etag.py:21` `_TAG = re.compile(r'(W/)?"([^"]*)"|\*')` is applied with `finditer`, so a `*` anywhere in the field becomes `(False, None)`, which `if_match_passes` (`:34`) and `if_none_match_hits` (`:43`) treat as a wildcard.
* **Reproducer:**
  ```
  PATCH /api/v1/admin/products/1  If-Match: "stale", *   {"stock_quantity": 99}
    -> 200 (update applied over a concurrent change)          expected 412
  GET /api/v1/products/1  If-None-Match: bogus*  -> 304 (client told it has a copy it never had)
  ```
* **Standard:** RFC 9110 §13.1.1 defines `If-Match = "*" / #entity-tag`. `*` is only the whole field value. The precondition exists to prevent lost updates, so a malformed field must not succeed.
* **Fix:**
  ```diff
  -def _parse(header):
  -    return [(bool(m.group(1)), m.group(2)) for m in _TAG.finditer(header)]
  +_ENTITY_TAG = re.compile(r'(W/)?"([\x21\x23-\x7e\x80-\xff]*)"')
  +def _parse(header):
  +    if header.strip() == "*":
  +        return [(False, None)]
  +    tags = []
  +    for item in (part.strip() for part in header.split(",")):
  +        if not item:
  +            continue
  +        m = _ENTITY_TAG.fullmatch(item)
  +        if m is None:
  +            return []          # malformed field: matches nothing (412 / 200)
  +        tags.append((bool(m.group(1)), m.group(2)))
  +    return tags
  ```

### B5: money accepts non-ASCII digits (both back ends)

* **Tests:** `test_bug_money_accepts_non_ascii_digits`, `test_money_input_matches_the_documented_contract` for 4 values x 4 writes, and `test_money_fuzz`, where Hypothesis found `'߀'`.
* **Where:** `app/schemas/order.py:45` `_MONEY_UNSIGNED = r"\d+(\.\d{1,2}0*)?"`, compiled at `:47` on a `str` pattern. Python's `\d` matches every Unicode decimal digit (Nd), and `Decimal()` converts them.
* **Reproducer:**
  ```
  PATCH /api/v1/admin/products/1 {"price": "１０.５０"}  -> 200 {"price":"10.50"}
  PATCH ... {"price": "١٠"} -> 200 "10.00";  {"price": "𝟏𝟎"} -> 200 "10.00"
  ```
* **Standard:** the published contract. The OpenAPI pattern is `^0*\d{1,8}(?:\.\d{1,2}0*)?$`, and in ECMA-262 regex (JSON Schema 2020-12 §6.4) `\d` is `[0-9]`. The server therefore accepts inputs its own schema forbids. The `_decimal_string_only` docstring says the intent is "a decimal string ('10.50')".
* **Fix:** `_MONEY_INPUT = re.compile(_MONEY_PATTERN, re.ASCII)`, or spell the pattern with `[0-9]`.

### B6: PATCH does not merge nested objects (RFC 7396 §2) (both back ends)

* **Tests:** `test_merge_patch_of_a_free_form_object_follows_rfc7396` and `test_bug_merge_patch_replaces_nested_objects_instead_of_merging`.
* **Where:** `app/api/v1/admin.py:831`, and `:286` for `characteristics`. The handler does `setattr(db_variant, field, value)` with the whole patch value. There is no recursive merge, and a `null` inside a nested object is stored rather than removing the member. For `characteristics`, `{"color": null}` is a 422 instead of a removal.
* **Reproducer:**
  ```
  variant.attributes = {"size": "M"}
  PATCH /api/v1/admin/products/1/variants/1  Content-Type: application/merge-patch+json  {"attributes": {"color": "red"}}
    -> 200 {"attributes": {"color": "red"}}             RFC 7396: {"size": "M", "color": "red"}
  PATCH ... {"attributes": {"size": null}} -> 200 {"attributes": {"size": null}}   RFC 7396: {} (member removed)
  ```
* **Standard:** RFC 7396 §2 (MergePatch is recursive) and its Appendix A examples. The API declares RFC 7396 and `application/merge-patch+json` (`app/api/merge_patch.py:17-21`). Its text "Members present replace the stored value" is only true for non-object members.
* **Fix:** for PATCH requests, apply `merge(stored, raw_patch_member)` with the reference algorithm to object-valued members (`attributes`, `characteristics`) before validation. Alternatively, stop advertising RFC 7396 for these fields and document them as whole-value replacement.

### B7: `application/json-patch+json` (and any `application/*+json`) is applied as a merge patch (both back ends)

* **Test:** `test_bug_json_patch_media_type_is_applied_as_a_merge_patch[user|address|product|variant]`.
* **Where:** FastAPI parses any `application/*+json` body as JSON, as noted in `app/api/merge_patch.py:10`. `_unsupported_media_type` (`app/core/errors.py`) only fires for non-JSON types.
* **Reproducer:**
  ```
  PATCH /api/v1/users/me  Content-Type: application/json-patch+json  {"full_name": "Mallory Name"}
    -> 200 {"full_name":"Mallory Name",...}        expected 415 + Accept-Patch
  ```
* **Standard:** RFC 5789 §2.2 says an unsupported patch document gives 415, and that the patch semantics are defined by its media type. A JSON Patch document (RFC 6902) must never be interpreted with merge-patch semantics.
* **Fix:** add a dependency on the PATCH routes. It refuses any Content-Type other than `application/json` or `application/merge-patch+json` with 415, and adds `Accept-Patch: application/merge-patch+json` (RFC 5789 §3.1).

### B8: If-Match is ignored on PATCH/PUT /users/me and /users/addresses/{id} (both back ends)

* **Test:** `test_bug_if_match_is_ignored_on_profile_and_address_patch[user|address]`.
* **Where:** `app/api/v1/users.py:490-566` (`update_current_user`, def at :511) and `:713-783` (`update_address`, def at :732). Neither declares or evaluates `If-Match`.
* **Reproducer:** `PATCH /api/v1/users/me  If-Match: "stale-tag"  {"full_name":"Lost Update"}` gives 200 and the update is applied. 412 is expected.
* **Standard:** RFC 9110 §13.1.1: "An origin server that receives an If-Match header field MUST evaluate the condition ... prior to performing the method." Severity is lower than B4, since the client opts in and these resources publish no ETag yet.
* **Fix:** compute `etag_for(UserResponse / AddressResponse)` and call `require_if_match` exactly as `update_product` does, and return ETag on GET /users/me and GET /users/addresses/{id}. Alternatively, explicitly refuse conditional headers on routes that do not support them.

### B9: price filters with an exponent beyond PostgreSQL `numeric` give a 500 (PostgreSQL only)

* **Tests:** `test_bug_price_filter_beyond_postgres_numeric_is_500[1e131072|1e-16384|0E-1000000|1e999999]` and `test_price_filters_never_500`.
* **Where:** `app/schemas/product.py:140-141`: `min_price` and `max_price` are `Decimal | None = Field(None, ge=0)`, with no `max_digits` or `decimal_places` and no exponent bound. The value is bound as a `numeric` parameter, and PostgreSQL raises "value overflows numeric format" or the equivalent.
* **Reproducer:**
  ```
  GET /api/v1/products?min_price=1e-16384  ->  PG: 500 {"type":"about:blank","title":"Internal Server Error","status":500,"detail":"A database error occurred",...}
  GET /api/v1/products?max_price=0E-1000000 -> PG: 500   (the value is zero)
  SQLite: 200 for all.
  ```
  The existing `test_catalog_money.py::test_a_price_filter_beyond_the_column_is_not_a_server_error` only tries `1e30`.
* **Standard:** RFC 9110 §15.6.1, a 500 for a client error on an unauthenticated route. Nothing internal leaks: the problem detail is generic.
* **Fix:**
  ```diff
  -    min_price: Decimal | None = Field(None, ge=0)
  -    max_price: Decimal | None = Field(None, ge=0)
  +    min_price: Decimal | None = Field(None, ge=0, le=Decimal("1e12"))
  +    max_price: Decimal | None = Field(None, ge=0, le=Decimal("1e12"))
  +    @field_validator("min_price", "max_price")
  +    @classmethod
  +    def _to_cents(cls, v): return None if v is None else v.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
  ```
  The quantize removes extreme negative exponents. Clamping above the column maximum (99999999.99) keeps "everything" semantics.

## Policy observations (asserted as documented, not failed)

* **Password normalisation is NFC, not NFKC.** The project documents NFC (`app/core/passwords.py:32`, citing NIST SP 800-63B-4 §3.1.1.2). NIST SP 800-63B §5.1.1.2 (rev. 3) says the verifier SHOULD apply NFKC or NFKD. Consequences, asserted in `test_password_normalisation_policy`:
  * `ﬁ` vs `fi`, fullwidth vs ASCII, `²` vs `2`, `Ⅳ` vs `IV`, and NBSP vs space are different passwords.
  * U+212B Å / U+00C5, the Kelvin sign U+212A vs K, Hangul jamo vs syllables, and combining sequences in either order are the same password.
* **Invisible characters are part of the password.** ZWJ, ZWSP, BOM and RTL override are never stripped. This is consistent with "any characters"; NIST does not ask for stripping.
* **The blocklist compares NFC + casefold.** `ｐａｓｓｗｏｒｄ` and `pass​word` are accepted (`test_blocklist_compares_the_nfc_casefolded_password`). NFKC would catch the fullwidth spelling.
* **Length.** The minimum is 8 characters after NFC and the maximum is 72 bytes of UTF-8, as documented in the UserCreate field description.
  * NIST SP 800-63B SHOULD permit at least 64 characters. The bcrypt limit allows only 18 astral characters or 36 two-byte characters, so `"é"*64` gives 422.
  * SP 800-63B-4 also requires a minimum of 15 for single-factor passwords, while the project uses 8.
  * Both are policy choices, recorded here only.
* **E-mail local parts are not NFKC-folded.** `ｊａｎｅ@example.com` and `jane@example.com` are two accounts, and `ß` and `ss` stay distinct. The project documents NFC (RFC 6531 §3.3), and its property holds: one canonical address gives one account, and two distinct canonical addresses never merge.
* **Pydantic lax mode on integer fields.** `"stock_quantity": true` is stored as 1 and `"5"` / `5.0` are accepted. Money is strict (string only); integers are not. Only observed.
* **Malformed JSON details.** Duplicate JSON keys follow last-wins. A UTF-16 body and a UTF-8 BOM are accepted: RFC 8259 §8.1 says senders MUST use UTF-8, and receivers MAY ignore a BOM.
* **No 428.** If-Match is optional on admin product and variant updates, as documented, so `test_no_if_match_is_an_unconditional_update_not_428` holds.
* **Negative zero is accepted.** `"-0"` and `"-0.00"` are accepted as `0.00`. The code lets the sign through on purpose (`app/schemas/order.py` comment), and the response never shows `-0.00`.
* **Dead schema.** `CategoryUpdate` has no route.

## Properties that held (both back ends)

* **Passwords:**
  * Any password admitted by the policy logs in as typed, as NFC and as NFD (60 random cases plus the named tables).
  * Decomposed input up to 108 typed bytes counts by its NFC form (72 bytes).
  * Passwords that share their first 72 bytes never collide: the longer one gives 422 at registration and 401 at login.
  * A 20 KB password gives 401, not 500.
* **Unusable text:** a lone surrogate or NUL in a password gives 422, both as a JSON escape and in the form login. A valid surrogate pair round-trips.
* **E-mail uniqueness:** no two accounts for one canonical address and no false merges, over 80 random case, NFC/NFD, padding and IDNA (U-label) variants.
* **Explicit null:** null on every field of the 4 PATCH schemas matches the published schema: nullable fields are cleared (200), the rest give 422 naming the field.
* **Body shape:**
  * A non-object merge patch gives 400 or 422.
  * Unknown members always give 422.
  * text/plain, form and xml give 415 with `Accept: application/merge-patch+json, application/json`.
  * `; charset=utf-8` and mixed-case media types are accepted.
* **ETags:**
  * `*`, the exact tag, and a list containing the strong current tag all match.
  * `W/` never matches for If-Match.
  * Unquoted, empty and stale fields give 412 carrying the current ETag, and the resource is left unchanged.
  * If-None-Match uses weak comparison. `*` gives 304 with an ETag and an empty body. `*` on a missing resource gives 404.
  * ETags agree between the DB path and the Redis path.
* **Money:** apart from Unicode digits (B5), every value outside the published pattern gives 422. This includes exponents, whitespace, signs, NaN, Infinity, sNaN, inf, underscores, hex, JSON numbers, bools, raw `1e400` and `NaN` literals, and over-precision. Accepted values are stored and returned as exactly two decimals.
* **Pagination:**
  * `page` is 1..2^31-1 and `size` is 1..100 on all 8 list endpoints. Everything else gives 422; nothing beyond int32 reaches the DB.
  * At page 2^31-1 with size 100 the response is 200 with `items: []` and `has_more: false`, and the OFFSET does not overflow on PostgreSQL.
  * 250 random page and size values (integers up to ±2^70 and arbitrary text) give only 200 or 422.
* **Ids:** ids in paths from -2^80 to 2^80, plus spellings (`-0`, `00001`, `1.0`, `0x1`, `１`, 30-digit values), give 404 or 422 on 6 routes, including on PostgreSQL.
* **RFC 9457:** every error response observed, including the 500s of B2, B3 and B9, is a well-formed problem document.
  * `type` is a URI reference, `title` a string, `status` equals the HTTP status, `instance` has no query string, and `errors` carry no `input`.
  * There is no traceback, SQL, driver or library name, or file path in any body.
  * 250 random bodies to 4 endpoints all received a success or a strict problem.
