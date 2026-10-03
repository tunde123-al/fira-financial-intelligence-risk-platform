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
| Rate limiting | Token bucket per bearer token (or client IP), `RATE_LIMIT_PER_MINUTE`, returning 429 with `Retry-After`. It is per process; behind several replicas, enforce limits at the gateway or load balancer |
| Secure errors | Unhandled exceptions return `{"detail":"internal error","request_id":…}`. Tool errors return types, not stack traces; details go to logs only |
| Headers | `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Cache-Control: no-store`, plus a strict CSP on the UI (nginx). CORS is restricted to configured origins and methods |
| Audit | `audit_log` records logins (incl. denied), every tool call (agent, API or MCP), agent runs, decisions, investigation creation, unmasking, ingestion, evaluation runs, proposals and approvals, each with request id. **PostgreSQL mode:** database-enforced append-only: Alembic revision 0002 adds triggers that reject `UPDATE`, `DELETE` and `TRUNCATE` on `audit_log` for every role (tested in `tests/integration/test_services.py::AuditAppendOnlyTest`). This is **not** cryptographic immutability (see Known limitations). **Frames mode:** the audit trail is in memory and is lost on restart |
| Secrets | Only from environment variables (`.env.example` lists them; `.env` is git-ignored). CI runs a secret-pattern scan, bandit and pip-audit |
| Decision boundary | No tool can change accounts or customers or file reports. The validator removes action directives that do not defer to a human. Investigations end in `pending_review` |

## Known limitations

These are real gaps in the current implementation. FIRA is a prototype on synthetic data and has not
been through a security review or penetration test.

- **No token revocation.** Access tokens are stateless HS256 JWTs valid until `exp` (default 60 min).
  A logout only discards the token in the browser (it is kept in `sessionStorage`), and a disabled
  user's existing token keeps working until it expires.
- **No per-user lockout.** Failed logins are audited and the rate limiter applies, but there is no
  account lockout or progressive delay.
- **Rate limiting is process-local** (in-memory token bucket keyed by bearer-token hash or client IP).
  It is not shared across workers or replicas.
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
