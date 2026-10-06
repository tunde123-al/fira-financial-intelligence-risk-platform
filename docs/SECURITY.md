# Security and privacy

| Control | Implementation |
|---|---|
| Authentication | `POST /api/auth/login` issues JWT (HS256) tokens with `sub, role, iat, exp, iss, jti`, default TTL 60 min. Passwords use scrypt (N=2^14, r=8, p=1, 16-byte salt) with constant-time comparison; failed logins run a dummy verification. In production, startup fails if `JWT_SECRET` is missing or shorter than 32 characters; in development a random per-process secret is used |
| Users | Bootstrapped on first start only when `BOOTSTRAP_ADMIN_PASSWORD` / `BOOTSTRAP_ANALYST_PASSWORD` are set (min 10 chars). No default credentials exist |
| Authorisation (RBAC) | Roles `analyst` < `admin`, enforced at **two layers**: API dependencies (`require_role`) and the tool registry (`required_role` per tool). Admin-only: metrics, document ingest/upload, evaluation runs, config approval, unmasking. Analysts see only their own audit trail |
| Four-eyes | A configuration change cannot be approved by its proposer, and only `validated` candidates can be approved |
| MCP | `FIRA_MCP_API_KEY` must match an entry in `MCP_API_KEYS=key:role`, otherwise the server refuses to start. Calls run as `mcp:<key hash>` with that role through the same registry, so they get the same checks and audit |
| Input validation | Pydantic models everywhere. Entity ids are validated by regex at the API and tool boundaries. Lengths and ranges are bounded. Uploads are limited to pdf/docx/md/txt, a safe filename pattern and 20 MB |
| SQL injection | Every statement is a module-level constant with bound parameters. The only dynamic SQL (update helpers) builds column lists from code whitelists. `verify_sql_with_psql.py` exercises every statement |
| Rate limiting | Token bucket per bearer token (or client IP), `RATE_LIMIT_PER_MINUTE`, returning 429 with `Retry-After`. It is per process; behind several replicas, enforce limits at the gateway or load balancer. Login lockout and token revocation are described below |
| Secure errors | Unhandled exceptions return `{"detail":"internal error","request_id":…}`. Tool errors return types, not stack traces; details go to logs only |
| Headers | `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Cache-Control: no-store`, plus a strict CSP on the UI (nginx). CORS is restricted to configured origins and methods |
| Audit | `audit_log` records logins (incl. denied), every tool call (agent, API or MCP), agent runs, decisions, investigation creation, unmasking, ingestion, evaluation runs, proposals and approvals, each with request id. **PostgreSQL mode:** database-enforced append-only: Alembic revision 0002 adds triggers that reject `UPDATE`, `DELETE` and `TRUNCATE` on `audit_log` for every role (tested in `tests/integration/test_services.py::AuditAppendOnlyTest`). This is **not** cryptographic immutability (see Known limitations). **Frames mode:** the audit trail is in memory and is lost on restart |
| Secrets | Only from environment variables (`.env.example` lists them; `.env` is git-ignored). CI runs a secret-pattern scan, bandit and pip-audit |
| Decision boundary | No tool can change accounts or customers or file reports. The validator removes action directives that do not defer to a human. Investigations end in `pending_review` |

## Transaction monitoring, alerts and cases (v2)

| Control | Implementation |
|---|---|
| Authorisation | Roles stay `analyst` < `admin`. Everyone authenticated can **view** alerts, cases and workbenches. Working an item (assign, transition, note, evidence, decision) requires being its assignee, or the item being unassigned (working it claims it), or `admin`. Assigning to someone else, running monitoring and ingesting transactions are `admin` only. Tests: `tests/unit/test_monitoring_api.py`, `tests/unit/test_monitoring_alerts.py` |
| No per-case confidentiality | There is **no** "restricted case" / need-to-know ACL: any analyst can read any case. This is a limitation, not a feature |
| No four-eyes on case decisions | The assignee (or an admin, who may be the same person) decides. Only risk-configuration changes are four-eyes |
| Decisions | Require a reason (at least 5 characters); are attributed to the authenticated user, recorded in `case_events`, in the alert history and in `audit_log` (`decision_recorded`, `case_closed`) |
| LLM authority | None. Detection, scoring, alert creation and every state change are deterministic code. An LLM can only draft narrative text from stored evidence; it cannot create, close or re-score anything. The UI labels AI-written claims |
| Input validation | Request bodies forbid unknown fields; ids, enums, dates and numeric ranges are validated; sort columns and filter values are whitelisted; every SQL statement uses bound parameters |
| Evidence integrity | Analysts can attach only objects that exist (a transaction of the customer, a retrieved document passage, an alert of the case). Free-text "facts" are rejected |
| Atomicity | In PostgreSQL mode each workflow operation (decision plus alert resolution plus history) commits or rolls back as one transaction, and audit entries are written only after the commit. The in-memory repository serialises operations but cannot roll back |
| History tables | `monitoring_alert_events`, `case_events`, `case_notes` and `case_evidence` reject `UPDATE` and `DELETE` through triggers (same mechanism and same limits as `audit_log`). `TRUNCATE` is **not** blocked on these four tables (a dataset reload with `loader --truncate` clears them); it is blocked on `audit_log`. Alerts and cases themselves are mutable by design (status, assignee), and every change is recorded |
| Error handling | Workflow errors return a short message and the right status (403, 404, 409, 422); no stack traces or SQL are returned |
| Public demo | Static UI is served with a Content-Security-Policy; synthetic data only; `LLM_PROVIDER=none`; rate limit lowered; see DEPLOYMENT.md for what to change before exposing it |

## Security review of the v2 additions (2026-10-03)

| Area | What was checked | Result |
|---|---|---|
| Authentication / JWT / passwords | unchanged from v1; every new route depends on `get_principal`; unauthenticated calls to the new endpoints return 401 (tests) | no new issue |
| Authorisation / RBAC | assignment-based rules and admin-only operations tested over HTTP and at service level, including another analyst's alert/case and admin override | works as specified; **limitation:** no per-case confidentiality (documented above) |
| SQL injection | all new SQL uses bound parameters; dynamic SQL is limited to whitelisted sort expressions and fixed fragments; `LIKE` input is escaped; injection-style filter values return 422 (tests) | no injection path found |
| Input validation | request models forbid unknown fields and validate ids, enums, ranges, lengths and dates. **Finding fixed:** an invalid `ip_address` in an ingested batch reached the PostgreSQL `inet` cast and would have failed the whole batch with a 500; rows are now validated and rejected individually (tests) | fixed |
| Secrets | no secrets committed; `render.yaml` uses `sync: false` / `generateValue` and contains no values; the CI secret scan passes | clean |
| CORS, rate limiting | unchanged; the static UI is same-origin; a CSP is added to UI responses only | no new issue |
| Audit | alert, case, note, evidence, decision and monitoring actions are audited; entries are written after the transaction commits; history tables reject update/delete in PostgreSQL | as documented |
| Sensitive data in responses | the workbench passes through the same PII masking as other endpoints (test asserts hashes are absent); detection results omit the heuristic `confidence` | no leak found |
| Error leakage | workflow errors return short messages with the right status; tests assert no stack trace or SQL in 4xx/5xx bodies | no leak found |
| Dependency vulnerabilities | `pip-audit -r backend/requirements.txt`: none known. `npm audit`: 0 vulnerabilities (a vulnerable Vitest 3.x was replaced by 5.x during this work) | clean at the time of the run (dependencies are unpinned) |
| Static analysis | `bandit -ll`: no medium or high findings; `ruff` and `mypy` clean | clean |
| Not done | penetration test, threat model review, dynamic scanning, load/abuse testing of the new endpoints | **not done** |

## Hardening review for the production-oriented upgrade (2026-10-03)

Checked and, where noted, changed. Evidence is in the test files named.

| Area | What was checked / done | Result |
|---|---|---|
| Authentication | JWT with `jti`; `POST /api/auth/logout` revokes the presented token; per-username and per-address login lockout (429 + `Retry-After`, audited as `login` / `locked`); failed logins counted in `fira_login_failed_total` | added; `tests/unit/test_security_hardening.py`. Limits: in-memory (see Known limitations) |
| Authorisation | assignment-based rules unchanged; new endpoints: config changes, detector switches, triage recompute are `admin`; data-quality, quality, feedback, my-work, mule views are authenticated read-only; `my-work` returns only the caller's items | `test_case_management.py`, `test_governance.py` |
| Input validation | request bodies forbid unknown fields; ingestion now accepts raw rows and classifies each bad row with a reason code instead of failing the request (unknown fields, bad types, bad ids, bad IPs are quarantined, never stored in the transaction table); config overrides use an allow-list of paths and re-validate the whole configuration with Pydantic | `test_data_quality.py`, `test_governance.py` |
| Request size | requests larger than `MAX_BODY_BYTES` (default 5,000,000) are refused with 413, whether declared in `Content-Length` or streamed; the ingestion endpoint also caps a batch at 5,000 rows | `test_security_hardening.py` |
| Rate limiting | unchanged algorithm; bucket table is now bounded (idle buckets evicted) | tested |
| Headers | adds `Permissions-Policy`, `Cross-Origin-Opener-Policy`; `Strict-Transport-Security` only when `FIRA_ENV=production` (TLS terminates in front of the app); existing set unchanged | tested |
| API documentation | `/docs`, `/redoc`, `/openapi.json` are **off in production** unless `ENABLE_API_DOCS=true` (which the production check then reports) | tested |
| Production settings | the process refuses to start in production with: missing or short or well-known `JWT_SECRET`, in-memory store or no `DATABASE_URL`, `CORS_ORIGINS` containing `*`, bootstrap passwords under 12 characters or well-known, docs enabled, `LOG_LEVEL=DEBUG`. The message lists the variable names, never their values | `test_security_hardening.py` |
| Metrics | `/metrics` needs an admin JWT **or** the optional `METRICS_TOKEN` in `X-Metrics-Token` (constant-time comparison) for scrapers | tested |
| Database | all SQL uses bound parameters (unchanged); new tables have `CHECK` constraints and append-only triggers; a **least-privilege role** (`infrastructure/sql/least_privilege_role.sql`) lets the API read everything and insert/update but not alter schema, truncate, delete (except document chunks) or touch the append-only ledgers; the whole workflow was run as that role, and `CREATE`, `DROP`, `ALTER`, `TRUNCATE`, `DELETE`, ledger `UPDATE` and `CREATE ROLE` were each refused with SQLSTATE 42501 | `tests/integration/test_least_privilege_postgres.py` |
| Connection failures | connect timeout 5 s (`DB_CONNECT_TIMEOUT_S`); a failure test found start-up with an unreachable database took 136 s before this was set | fixed, tested |
| Secrets | none committed; CI secret scan, bandit and pip-audit; backup passphrases and database passwords are read from the environment and never printed or put on command lines | clean |
| Dependency scans | `ruff` clean; `mypy` clean (102 source files plus the two backup scripts); `bandit -ll` no medium/high findings (5 low); `pip-audit -r backend/requirements.txt` no known vulnerabilities; `npm audit` 0 vulnerabilities | clean at the time of the run |
| PII in quarantine | rejected-row payloads are sanitised copies: IP addresses reduced to a /16 prefix, strings truncated to 120 characters, at most 30 fields | tested |
| Error leakage | 4xx/5xx bodies contain a short message and a request id, never a stack trace, SQL or an exception message from a dependency | `test_failure_modes.py`, `test_security_hardening.py` |
| Not done | penetration test, threat-model review, dynamic scanning, fuzzing, load/abuse testing, review of the Docker images, secret rotation, multi-instance token revocation | **not done** |

## Configuration governance

Changes to the settings that decide what becomes an alert are recorded in an append-only table (`config_change_log`; triggers
reject `UPDATE` and `DELETE`, and the least-privilege role has no such privilege either).

* **What is governed:** the whole monitoring configuration (alert policy and thresholds, triage thresholds and tables, money-mule
  bands and thresholds, data-quality rules, disabled detectors) and the whole risk configuration (signal weights, enabled flags,
  bands, thresholds).
* **What a record holds:** configuration name, setting path, old value, new value, who, when, why, and the route
  (`api`, `api:config_approve`, or `startup`).
* **API changes** (`POST /api/config/monitoring`, `POST /api/config/detectors/{id}`; admin only) require a reason of at least
  five characters, are limited to an allow-list of paths, are validated as a whole, and are audited (`config_changed`,
  `detector_disabled`, `detector_enabled`). They apply to the running process only: **the YAML files stay the source of truth, so
  a restart reverts an override** and the revert is itself recorded.
* **File edits** made between runs are found at start-up by comparing a canonical snapshot with the last recorded one, and are
  attributed to `system` with source `startup`: the process cannot know who edited a file. The first run records a baseline and
  no change rows.
* **Risk-config versions** keep the existing four-eyes proposal/approval workflow; an approval also writes change rows.
* The log can be read at `GET /api/config/changes` and on the Operations page. It is evidence of *what* changed, not proof of who
  edited a file on disk.

## Responsible disclosure

FIRA is an open-source prototype on synthetic data; it has had no external security review. If you find a vulnerability:

1. **Do not open a public issue** for it.
2. Report it privately: use GitHub's *Report a vulnerability* (private security advisory) on the repository. If that is not
   enabled, contact the maintainer through the contact details on their GitHub profile. (The maintainer should enable private
   vulnerability reporting before publishing the repository; a copy of this policy is in the root `SECURITY.md`.)
3. Include what you found, how to reproduce it, the version or commit, and what you believe the impact is.
4. Expect an acknowledgement within 7 days and a status update within 30 days. There is no bug bounty and no service-level
   commitment: this is a personal project.
5. Please test only against your own deployment and synthetic data; do not access data that is not yours.

Supported versions: the `main` branch only.

## Known limitations

These are real gaps in the current implementation. FIRA is a prototype on synthetic data and has not
been through a security review or penetration test.

- **Token revocation is per process.** `POST /api/auth/logout` revokes the token's `jti` until it expires, but the revocation
  list is in memory: it is lost on restart and not shared across replicas. A disabled user's existing token keeps working until
  it expires (default 60 min). There are no refresh tokens: the token simply expires and the user signs in again.
- **Login lockout is per process.** Five failed logins for a username (or twenty from one address) within 15 minutes lock that key
  out with 429 and `Retry-After`; a success resets the username. The counters are in memory (same limit as above).
- **Rate limiting is process-local** (in-memory token bucket keyed by bearer-token hash or client IP).
  It is not shared across workers or replicas. Idle buckets are evicted so the table cannot grow without bound.
- **Audit log is append-only, not tamper-proof.** The triggers stop the application and ordinary SQL
  from altering rows, but a superuser or the table owner can drop or disable them, and rows have no
  hash chain or signature. Retention purges (`AUDIT_RETENTION_DAYS`) are configuration only: nothing
  deletes rows automatically, and a purge would need a deliberate DBA action that temporarily
  disables the trigger.
- **Frames mode keeps users, investigations, evidence, decisions and the audit trail in memory.**
  Use PostgreSQL mode for anything that must survive a restart.
- **The frontend container is stock `nginx`** and does not set a non-root user (the backend image runs
  as a non-root user).
- **JWT signing key is symmetric (HS256)** and shared by all backend processes. There is no key
  rotation.
- **Local users only**: no SSO/OIDC and no MFA.
- **Synthetic data only.** Masking and hashing have not been evaluated against real PII.

## Privacy

| Principle | Implementation |
|---|---|
| Synthetic data | Development needs no real data. The generator produces realistic, labelled data |
| Data minimisation | No names are stored. Phone, email and address are stored only as salted SHA-256 hashes (for matching) plus masked display values. The agent receives only the evidence needed for the case, and the LLM evidence pack is masked |
| Sensitive-field masking | Applied at the API and MCP boundary: hashes and password hashes are dropped, IPs reduced to /16, device fingerprints truncated. `?unmask=true` is admin-only and audited. Controlled by `PII_MASKING` |
| Retention | `AUDIT_RETENTION_DAYS` (default 5 years) and `EPISODE_RETENTION_DAYS` (2 years) are configured. Enforce them with a scheduled job, e.g. `DELETE FROM agent_episodes WHERE finished_at < now() - interval '730 days' AND investigation_id NOT IN (open cases)`, run by a privileged role |
| Encryption-ready | TLS terminates at the load balancer or nginx. Use encrypted volumes and RDS/KMS at rest. `value_hash` and `masked_value` isolate identifiers, so field-level encryption can be added without schema redesign |
| External LLMs | Off by default. When enabled, only the masked evidence pack is sent. `ollama` keeps inference local |
