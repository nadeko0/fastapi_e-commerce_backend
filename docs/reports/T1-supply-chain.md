# Stage T1: Supply Chain and Static Security Analysis

Finding vulnerabilities in your own code is humbling; finding them in pre-installed packages you did not know you were shipping is just embarrassing. Stage T1 audits dependency supply chains, secret exposure risks, and base container image security to establish automated gates before release.

## Purpose and Exit Criterion

The purpose of Stage T1 was to audit dependency supply chain security, secret exposure risks, and base container image vulnerabilities before the 4.0.x releases.

Exit criteria (established prior to execution):
- Gitleaks: 0 secrets in the entire repository commit history.
- Pip-audit: 0 known vulnerabilities across production and development dependency graphs.
- Trivy: 0 fixable CRITICAL and 0 fixable HIGH vulnerabilities in the application image and all images defined in `docker-compose.yml`.

## Environment

All tooling satisfied the repository stability rule requiring external tools and base images to be older than two weeks:
- **Host system:** Local development workstation (Windows 11).
- **Gitleaks:** v8.30.1 executed via Docker container image pinned by digest.
- **Pip-audit:** v2.10.1 (auditing against both PyPI Advisory Database and OSV).
- **Trivy:** v0.75.0 (vulnerability database updated 24 October 2026).
- **Govulncheck:** v1.9.0 (Go vulnerability analyzer for call-graph reachability).
- **Container images evaluated:**
  - Application image: `ecom:4.0.0` (base `python:3.13-slim`, Debian 13.7 base).
  - Database service: `postgres:15-alpine` (pinned by digest).
  - Cache service: `redis:7-alpine` (pinned by digest).

## Method and Commands

Commands were executed from the repository root using generic paths:

1. **Secret detection across history:**
   ```bash
   gitleaks git --verbose --redact
   ```
2. **Secret detection in current working directory:**
   ```bash
   gitleaks dir --verbose --redact .
   ```
3. **Python dependency vulnerability audits:**
   ```bash
   pip-audit --requirement <work dir>/req-prod-no-extras.txt
   pip-audit --requirement <work dir>/req-all-extras.txt
   ```
4. **Container image vulnerability scanning:**
   ```bash
   trivy image --severity CRITICAL,HIGH --ignore-unfixed ecom:4.0.0
   trivy image --severity CRITICAL,HIGH --ignore-unfixed postgres:15-alpine
   trivy image --severity CRITICAL,HIGH --ignore-unfixed redis:7-alpine
   ```
5. **Call-graph verification of Go binaries:**
   ```bash
   govulncheck -mode binary <extracted binary path>/gosu
   ```

## Results

Initial execution against release tag v4.0.0:

| Scan Target | Tool | Scope | Result | Status |
|---|---|---|---|---|
| Git history (384 commits) | Gitleaks v8.30.1 | Full commit history | 55 hits (all false positives: dummy test tokens, synthetic passwords, sample webhook secrets). 0 real secrets. | PASS |
| Working tree | Gitleaks v8.30.1 | Filesystem without `.env` | 27 hits (all false positives in test fixtures). 0 real secrets. | PASS |
| Python deps (all extras) | Pip-audit v2.10.1 | 88 installed packages | 0 known vulnerabilities found across PyPI and OSV databases. | PASS |
| Python deps (production) | Pip-audit v2.10.1 | 51 production packages | 0 known vulnerabilities found across PyPI and OSV databases. | PASS |
| App image (`ecom:4.0.0`) | Trivy v0.75.0 | Application container | 1 CRITICAL and 4 HIGH fixable findings in bundled system pip; 44 unfixed HIGH in Debian 13.7. | FAIL |
| Database (`postgres:15-alpine`) | Trivy v0.75.0 | Compose database service | 1 CRITICAL and 25 HIGH fixable findings in bundled `gosu` binary (built with Go 1.24.6). | FAIL |
| Cache (`redis:7-alpine`) | Trivy v0.75.0 | Compose cache service | 2 HIGH fixable findings (OpenSSL 3.3.7-r1 vulnerabilities: CVE-2026-84782). | FAIL |

Remediation and post-fix scan results (v4.0.1):

| Container Image | Initial CRITICAL / HIGH (Fixable) | Post-Fix CRITICAL / HIGH (Fixable) | Post-Fix Status |
|---|---|---|---|
| Application image (`ecom:4.0.1`) | 1 / 4 | 0 / 0 | PASS |
| Database (`postgres:15-alpine`) | 1 / 25 | 0 / 0 (26 waivers in `.trivyignore.yaml`) | PASS |
| Cache (`redis:7.4.11-alpine`) | 0 / 2 | 0 / 0 | PASS |

## Findings and Remediations

- **F1 (High Severity): Vulnerabilities in base image bundled pip.**
  - *Cause:* The `python:3.13-slim` base image shipped with system `pip 26.2.1` in `/usr/local/lib/python3.13/site-packages/pip/_vendor/`, containing vulnerable vendored packages: certifi (CRITICAL GHSA-6q48-6c7w-9gw2), msgpack (HIGH GHSA-6v7p-g79w-8964), setuptools (HIGH CVE-2025-47273), and urllib3 (HIGH CVE-2026-97687, CVE-2026-97689). The application virtual environment was unaffected.
  - *Reproducer:* Run Trivy on the freshly built application image: `trivy image ecom:4.0.0`.
  - *Remediation:* Added `python -m pip uninstall -y pip` in the runtime stage of `Dockerfile`. Health checks rely on standard library `urllib`. Shipped in v4.0.1 (fix X1).
- **F2 (High Severity): Vulnerabilities in `gosu` within the official PostgreSQL image.**
  - *Cause:* The official `postgres:15-alpine` image included `/usr/local/bin/gosu` built with Go 1.24.6, triggering 1 CRITICAL and 25 HIGH standard library CVEs. No upstream patched image existed.
  - *Reproducer:* Run Trivy on the official postgres container: `trivy image postgres:15-alpine`.
  - *Analysis and Remediation:* Extracted `gosu` and executed `govulncheck v1.9.0` in binary analysis mode. Govulncheck confirmed that none of the 26 vulnerable functions are called by gosu; they exist only as compiled package symbols. All 26 CVE IDs were recorded in `.trivyignore.yaml` tied strictly to path `usr/local/bin/gosu` with explicit justification. One reachable MEDIUM vulnerability (CVE-2026-61801 / GO-2026-6686 in `moby/sys/user`) is practically unreachable because gosu parses root-owned container files once during initialization. Shipped in v4.0.1 (fix X8).
- **F3 (High Severity): OpenSSL vulnerabilities in Redis image.**
  - *Cause:* `redis:7-alpine` contained OpenSSL 3.3.7-r1 with CVE-2026-84782 in `libssl3` and `libcrypto3`.
  - *Reproducer:* `trivy image redis:7-alpine`.
  - *Remediation:* Granted a documented security exception to the two-week age rule to pin patched `redis:7.4.11` (Alpine 3.21.9, digest `sha256:57e4255cc971b1c8d6f8a6ca283ac01bc3ecdd914273ef6bfaede87aaecd6541`). Post-fix scan showed 0 vulnerabilities. Shipped in v4.0.1 (fix X8).
- **F5 (Low Severity): Unnecessary artifacts copied into container image.**
  - *Cause:* `COPY . .` copied test files, markdown documentation, and compose manifests into production image.
  - *Remediation:* Updated `.dockerignore` and restricted Dockerfile `COPY` instructions to `app/`, `migrations/`, `main.py`, `alembic.ini`, and `pyproject.toml`. Shipped in v4.0.1 (fix X1).
- **F6 (Low Severity): Missing `EXPOSE` instruction in Dockerfile.**
  - *Remediation:* Added `EXPOSE 8000` to Dockerfile. Shipped in v4.0.1 (fix X1).
- **F7 (Low Severity): Absence of Gitleaks baseline configuration and CI enforcement.**
  - *Remediation:* Added `.gitleaks.toml` with specific allowlists for synthetic test tokens and created a dedicated `secrets` CI workflow step running on every push. Shipped in v4.0.1 (fix X1).
- **Incidental Finding (Low Severity): Alembic offline SQL generation failure.**
  - *Cause:* Running `alembic upgrade head --sql` failed on migration `3f6c2a9d1b7e` due to an online database query executed during offline generation.
  - *Remediation:* Refactored migration data checks into PostgreSQL `DO $$` blocks. Shipped in v4.0.1 (fix L2).

## Limitations

- The 44 unfixed HIGH vulnerabilities in Debian 13.7 base packages (e.g. bsdutils, libmount1) cannot be resolved until upstream Debian publishes package updates. They are excluded from failing CI via `--ignore-unfixed`.
- Official Docker library PostgreSQL images continue to bundle outdated Go toolchains in gosu; reachability analysis via `govulncheck` must be re-run whenever the PostgreSQL digest is updated.

---

See also: [fixes-4.0.x.md](fixes-4.0.x.md), [final-test-campaign.md](final-test-campaign.md), [T6-dast-owasp.md](T6-dast-owasp.md), [../../SECURITY.md](../../SECURITY.md).
