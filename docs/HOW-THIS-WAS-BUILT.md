# How this was built

This is a first-person account of who did what in this repository, where the main decisions came from, what went wrong in the process, and where my own understanding stops. It is written from the git history, my chat logs with the AI tools, and my notes.

## Where it started

**January 2025.** A friend wanted to open an online shop and I wrote the backend for it. I wrote it myself, but following an AI assistant's instructions: the models back then were not very strong, yet they could explain things and suggest code. Honestly, the split was about 70% AI and 30% me. The shop never opened, so I kept the code on GitHub. That first version is the five commits from 12 to 14 January 2025.

**July 2026.** While cleaning up my GitHub before looking for a backend job, I picked this repository to finish. The goal was a training ground: a project I know end to end, can break and fix, and can talk about in interviews.

**August to September 2026.** A first round of fixes and modernisation, mostly in Claude Code: migration to uv, JSON logging, CI, a Stripe test double with idempotency, PostgreSQL checks, coverage. In September I started a line-by-line walkthrough of the code for interview preparation, and a larger refactoring was being prepared, so several holes found along the way were closed first (Docker multi-stage build, Celery task names, foreign keys in migrations, SQLAlchemy 2.1 with psycopg 3). The chat transcripts from these months are no longer kept, so this part comes from git and my notes.

**October 2026.** Load testing, a defensive security review, then releases 1.0.0 to 4.0.3, a final test campaign and the documentation you are reading.

## Who did what

- **The AI wrote the code.** Practically all code since 2026, including tests, migrations, CI workflows and most of the documentation, was written by AI agents. Mostly Claude Code: an Opus session planned the work, wrote task briefs and reviewed every result, while Sonnet and Haiku agents did the implementation and documentation lookups. When Claude usage limits ran out during the final campaign, some tasks went to Gemini through the Antigravity CLI, with the same review.
- **I did everything except writing the code.** I read the code and the diffs, researched topics myself, set up and ran the infrastructure (WSL, Docker, my own server for the live test, Stripe test mode, GitHub), set the rules and the scope, and decided when something was released.

## Where the decisions came from

| Decision | Origin |
|---|---|
| 4.0.0 is the last breaking release; after it, only testing and fixes | Mine |
| Strict SemVer: a release with a new endpoint is MINOR, so the planned 3.0.1 became 3.1.0 | Mine |
| Replace the custom rate limiter with one simple module that holds 200 concurrent users | Mine, after the AI had built a much larger one |
| Finish the async migration instead of falling back to plain `def` routes | Mine |
| Minimum password length of 8 | Mine |
| No response envelope; errors as RFC 9457 problem details | I asked for research on how production APIs do it instead of guessing; the AI proposed this from Google AIP-131, Microsoft, Stripe and Zalando guidelines; I accepted it |
| Strict input (`extra="forbid"`, money as strings) | Proposed by the AI after an API audit; I accepted it for 4.0.0 |
| Redis unreachable during a token check returns 503, not 401 | The AI's call |
| Dependencies at least two weeks old | The model's rule. Mine was "the newest version without known CVEs". When the two-week rule left known HIGH/CRITICAL CVEs in the Redis image, I made the exception: official images with security patches go in immediately, with scanner output as proof |
| No public promises (response times, support) and no statements about the project's future | Mine |

## Mistakes caught in review

Mistakes by the AI that I caught: a `SECURITY.md` draft that promised a 48-hour response to vulnerability reports, a promise I never made (removed), and a background job that hung for two hours.

Mistakes by agents that the reviewing session caught:

- **A wrong PASS.** The chaos test of stopping PostgreSQL under load was marked PASS although 2,205 responses were HTTP 500. It was reclassified as a FAIL and fixed in 4.0.2 (503 with `Retry-After`).
- **An optimistic memory reading.** A one-hour soak test was described as a memory "plateau" while memory still grew slowly. It is now reported as "plateau with a caveat".
- **Invented file paths.** Documentation drafts contained about 30 file paths that did not exist; all were replaced with real ones and checked by script.
- **Unread sources.** Two commit messages cited RFC sections that had not been opened; the citations were checked against the text and corrected.
- **A broken idempotent retry.** A refund pre-check made a retried refund return 409; property-based tests caught it and the pre-check was removed.
- **Messy commits.** Several agent branches had mislabeled or non-building commits and were rebuilt before merging.

## What I understand, and what I did not verify by hand

- **I understand the domain and the models.** I understood the January 2025 project as it was.
- **I walked through `app/core/` line by line** in September 2026 (configuration, database sessions, password hashing and JWT, the old rate limiter, logging) and started on the Redis service. An automated security audit I ran at the same time found real bugs, including one in the old rate limiter that my walkthrough had missed. The walkthrough also corrected my own misconceptions: for example, I first thought FastAPI plus Celery made this a microservice system; it is a modular monolith.
- **Not yet walked through line by line:** the routers, schemas, the payment and webhook code, and the Celery tasks, which were also heavily rewritten in October. I have read the diffs and know how they work at the level of flows and decisions, but not every line.
- **Numbers I did not recompute myself:** load-test latencies, mutation scores and scanner results. I ran the infrastructure; the numbers come from tool output collected by agents and checked by the reviewing session. The raw reports are in [docs/reports/](reports/README.md).
- **Never tested with real users.** No real frontend or client has used this backend, and payments have only run in Stripe test mode.
