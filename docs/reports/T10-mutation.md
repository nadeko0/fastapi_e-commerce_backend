# Stage T10: Mutation Testing (Test Adequacy & Fault Injection)

High code coverage can produce a false sense of security; tests that execute every line without asserting behavior are little more than expensive CPU heaters. Stage T10 runs mutation testing with mutmut 3.8.0, systematically injecting thousands of synthetic bugs into the financial core to verify that existing assertions actually catch them.

## Purpose and Exit Criterion

The purpose of Stage T10 was to evaluate the effectiveness, rigor, and fault-detection capabilities of the automated test suite on critical payment, event processing, order, idempotency, rate limiting, and error handling components via automated mutation analysis.

Exit criteria:
- High mutation score across the core financial and security codebase.
- All surviving mutants must be analyzed and either killed by adding targeted tests or proven to be equivalent mutations (syntactic changes that do not alter observable behavior).
- Zero surviving mutants in core financial calculations, state transitions, or access control boundaries.

## Environment

- **Host Machine:** WSL2 Ubuntu (Linux x86_64). Mutation testing was executed under Linux because mutmut 3.8.0 does not support Windows.
- **Target Repository:** Release tag `v4.0.2`.
- **Tooling:** Mutmut 3.8.0, Python 3.13.16, pytest 9.1.1, pluggy 1.7.0.
- **Process Isolation:** Forkserver runner (`ForkServerRunner`) with 4 parallel worker processes.
- **Test Configuration:** Executed with `HYPOTHESIS_PROFILE=ci` to ensure deterministic execution, running `pytest tests/ -p no:cacheprovider -m "not timing_sensitive"`.
- **Target Scope:**
  - *Priority 1:* `app/services/payment/` (all providers and models), `app/services/stripe_events.py`, `app/services/reconciliation.py`, `app/core/idempotency.py`.
  - *Priority 2:* `app/services/orders.py`, `app/core/rate_limit.py`, `app/core/errors.py`.
- **Execution Duration:** 55 minutes 48 seconds total (7 minutes coverage tracing, 48 minutes 38 seconds mutant testing across 3165 mutants; ~1.1 mutants per second).

## Method and Commands

1. **Mutmut Configuration in `pyproject.toml`:**
   ```toml
   [tool.mutmut]
   paths_to_mutate = "app/services/payment,app/services/stripe_events.py,app/services/reconciliation.py,app/core/idempotency.py,app/services/orders.py,app/core/rate_limit.py,app/core/errors.py"
   backup = false
   runner = "pytest tests/ -p no:cacheprovider -m 'not timing_sensitive'"
   ```
2. **Mutation Run:**
   ```bash
   mutmut run
   ```
3. **Survivor Extraction and Triage:**
   ```bash
   python3 <work dir>/dump_all_survivors.py
   ```

## Results

### Initial Mutation Score on Release v4.0.2

| Target Source File | Total Mutants | Mutants Killed | Mutants Survived | Mutation Score |
|---|---|---|---|---|
| `app/services/orders.py` | 56 | 56 | 0 | 100.00% |
| `app/services/payment/__init__.py` | 4 | 4 | 0 | 100.00% |
| `app/services/payment/base.py` | 52 | 52 | 0 | 100.00% |
| `app/services/payment/exceptions.py` | 39 | 39 | 0 | 100.00% |
| `app/services/payment/stripe_provider.py` (Mock) | 527 | 527 | 0 | 100.00% |
| `app/core/rate_limit.py` | 297 | 292 | 5 | 98.32% |
| `app/services/stripe_events.py` | 945 | 925 | 20 | 97.88% |
| `app/core/idempotency.py` | 29 | 28 | 1 | 96.55% |
| `app/services/payment/live_stripe_provider.py` | 585 | 564 | 21 | 96.41% |
| `app/services/reconciliation.py` | 353 | 324 | 29 | 91.78% |
| `app/core/errors.py` | 278 | 252 | 26 | 90.65% |
| **Total** | **3165** | **3063** | **102** | **96.78%** |

### Triage of the 102 Surviving Mutants

Every single surviving mutant was classified into one of two categories:
- **52 Equivalent Mutants:** Syntactic variations that cannot change runtime program output:
  - Mutating default parameters that are always overridden by caller contexts (e.g. ASGI `scope.get("root_path", "")` mutations where Starlette always defines the key).
  - Internal string formatting in human-readable exception messages that do not affect machine-readable problem status codes or problem type URIs.
  - Redundant guards and fallback checks where upstream invariant validation already guarantees safe execution.
- **50 Real Test Gaps:** Valid behaviors that lacked asserting test coverage:
  - Rare error codes in live Stripe provider: refund list failures, refunds exceeding pagination limits (>500 items), custom signature tolerance offsets.
  - Unusual webhook structures: webhook `data` object not formatted as a dictionary, and GDPR redaction of stored event payloads.
  - Reconciliation paging, missing transaction edge cases, and discrepancy counters.
  - Problem Details formatting for RFC 9110 §15.5.6 (405 Method Not Allowed `Allow` header formatting) and RFC 9110 §15.5.16 (415 Unsupported Media Type detail messages).
  - Rate-limit partition key generation when parsing malformed or non-Bearer authorization tokens.
- **Zero Financial Gaps:** Zero surviving mutants in monetary calculations, discount computations, stock deductions, or order status state machines.

### Final Verification and Resolution in Release v4.0.3

In task X10, 50 targeted unit and integration tests were written to kill all 50 real test gaps. Each test was proven against its specific mutant in mutmut before commit.

Post-remediation score (Release v4.0.3):
- Total mutants: 3165
- Mutants killed: 3113
- Mutants survived: 52 (100% verified equivalent)
- **Final Mutation Score:** **98.36%** (98.4%)
- Application code changed: 0 lines (test suite enhancement only).

## Findings and Remediations

- **Finding 1 (Low Severity / Test Coverage Gap): Unasserted live Stripe error conditions.**
  - *Detail:* Rare exception handling branches in `app/services/payment/live_stripe_provider.py` (such as `StripeError` handling during refund listings) lacked specific test assertions.
  - *Remediation:* Added tests simulating exact Stripe SDK error responses. Shipped in v4.0.3 (fix X10).
- **Finding 2 (Low Severity / Test Coverage Gap): Webhook payload boundary conditions.**
  - *Detail:* Malformed webhook payloads where `data` was a non-dict scalar survived mutation in `stripe_events.py`.
  - *Remediation:* Added tests asserting RFC 9457 problem detail output for non-dictionary payload structures. Shipped in v4.0.3 (fix X10).
- **Finding 3 (Low Severity / Test Coverage Gap): Rate limit partition parsing.**
  - *Detail:* Parsing non-standard Authorization header schemes in `rate_limit.py` survived mutation.
  - *Remediation:* Added tests validating IP fallback key generation for non-Bearer schemes. Shipped in v4.0.3 (fix X10).

## Limitations

- Mutmut 3.8.0 was executed exclusively in Linux/WSL2; Windows execution is unsupported upstream.
- Mutation testing was focused on the payment, ordering, and security layers; non-critical utility modules were not mutated due to CPU execution costs.

---

See also: [fixes-4.0.x.md](fixes-4.0.x.md), [final-test-campaign.md](final-test-campaign.md), [../TESTING.md](../TESTING.md).
