# Implementation roadmap, verification status and risk register

## D. Roadmap (as executed)

| Phase | Scope | Status |
|---|---|---|
| 1 | Architecture, schema + migration, synthetic generator with ground truth, FastAPI, basic UI | Done |
| 2 | Risk engine: 17 detectors, transparent scoring, geospatial, ML anomaly model | Done |
| 3 | Graph projection, Neo4j and NetworkX backends, investigator graph tools | Done |
| 4 | Document ingestion (MD/PDF/OCR/DOCX/Docling), Qdrant, hybrid RAG with provenance | Done |
| 5 | Agent workflow (LangGraph), tool registry, report generation, claim validation, MCP | Done |
| 6 | Evaluation framework, episodic memory, human feedback, controlled improvement | Done |
| 7 | Security, observability, CI/CD, Docker deployment, documentation | Done |

| 8 (v2) | Transaction monitoring: ingestion with validation, STRUCTURING and FAN_OUT detectors, automatic alert generation with deduplication and a lifecycle, cases, investigator workbench, network queries, activity windows, evaluation benchmark, performance benchmark, deployment recipe. See [TRANSACTION_MONITORING_ARCHITECTURE.md](TRANSACTION_MONITORING_ARCHITECTURE.md) | Done (see limitations) |

| 9 (v3) | Production-oriented upgrade: data-quality gate with batch ledger and quarantine, explainable alert triage, operational alert-quality metrics, money-mule indicator view and flow graph, case priority and investigator queue, configuration governance, observability and security hardening, backup/restore with a real restore test, failure tests, index review, performance extensions, CI gates, money-mule benchmark scenarios and evaluation. See [PRODUCTION_ORIENTED_ARCHITECTURE.md](PRODUCTION_ORIENTED_ARCHITECTURE.md) section 14 | Done (see limitations) |

**Alert creation (v2).** FIRA now creates its own alerts (`monitoring_alerts`) from deterministic detectors
during monitoring runs. The seeded `alerts` table is still the legacy upstream feed and is not written by FIRA.
What is still *not* built: scheduled/continuous monitoring (runs are started by an admin, the CLI or an
external scheduler), alert feedback into detector configuration, SLA timers and queue routing, and any
regulatory-reporting workflow.

**What v3 changed and what it did not.** v3 adds the engineering practices around the monitoring workflow (data quality,
triage, measurement, governance, hardening, backup and restore, failure testing); it does not make FIRA a production banking
system. Nothing has been operated, deployed, penetration-tested or run on real data. The detectors are unchanged; the new
money-mule view is an evidence aid that ranks well (AUC 0.97 on synthetic data) but does not beat the existing alerts as a
classifier. RPO 24 h and RTO 1 h are targets, not measurements.

**Next steps (not built):**

- Persistent, shared token revocation and login-lockout state and a shared (gateway or Redis) rate limiter: v3 added logout
  revocation and lockout, but both are per process. See the Known limitations in [SECURITY.md](SECURITY.md).
- Scheduling and off-host storage for backups, point-in-time recovery (WAL archiving), and a restore test on production-sized data.
- Continuous or scheduled monitoring, a job queue, and SLA timers beyond the configurable "overdue" flag.
- An incremental graph projection (ingestion rebuilds the whole in-memory graph).
- Persisting runtime configuration overrides (today the YAML is the source of truth and a restart reverts an override).
- A calibration study of triage weights and thresholds against real investigator outcomes; today they are documented heuristics
  tuned once on synthetic data.
- Embedding the money-mule flow view in the case workbench and a Neo4j implementation of the mule graph queries.
- Mutually exclusive roles / four-eyes for case decisions and per-case confidentiality.
- Cryptographic tamper-evidence for the audit log (hash chain or external anchoring). The current
  protection is trigger-enforced append-only only.
- Frontend component and end-to-end tests (v2 added unit tests for the workflow helpers only; there are no component or browser tests).
- Transaction table partitioning and a job scheduler, which matter only at volumes far beyond the
  synthetic 254k transactions.
- An asynchronous job queue for agent runs. The API is currently synchronous; with an LLM a run
  takes about 10–30 s.
- Incremental Neo4j projection (CDC from PostgreSQL). The projection is currently rebuilt in full.
- SSO/OIDC instead of local users.
- Field-level encryption with KMS for identifiers.
- Neural embeddings by default once a GPU or embedding API is approved.
- A labelled real-data evaluation set.
- Multimodal evidence (ID-document images, screenshots). The evidence model supports
  `source_type` extensions, and OCR is already wired for scanned documents.

## Verification status (be precise about what has been run)

The build environment could reach only GitHub. PyPI, npm, Docker Hub and the Neo4j download site
were blocked, and installing packages from source was not permitted. What was actually executed:

| Component | How it was verified |
|---|---|
| Synthetic generator, risk engine, ML model, graph (NetworkX), documents (MD/PDF/table/OCR/DOCX), retrieval (LSA + BM25 + RRF), agent workflow (built-in runner), validator, evidence, memory, evaluation, improvement loop, MCP server (mcp 2.x SDK) | **Executed** — 84 automated tests passing, plus full benchmarks on 10k (dev) and 5k (holdout) datasets |
| PostgreSQL schema + full data load (all constraints and FKs, 254k transactions) | **Executed** against a real PostgreSQL 16 using `psql \copy` of the loader's output |
| All 63 `SqlStore` / document-repository SQL statements | **Executed** against PostgreSQL with real data (`infrastructure/scripts/verify_sql_with_psql.py`). Results cross-checked with `FrameStore` |
| REST route handlers (42 routes) | **Executed** directly (login, RBAC, masking, agent run, decisions, proposals/approval, audit) through a minimal FastAPI stand-in. The real ASGI stack (middleware, OpenAPI) runs in CI (`ApiStackTest`) |
| React UI | **Bundled** with esbuild against React 19, **type-checked** with tsc (React types stubbed), and **driven end to end in Chromium** (login → search → profile → investigation → all tabs → decision → graph → documents → evaluation → audit) with zero console errors |
| ruff, mypy | Clean |
| Neo4j backend, Qdrant REST client, LangGraph compilation, psycopg/SQLAlchemy runtime, PostGIS layer, Docker images, Alembic run | **Written and unit-reasoned but not executed here.** They are covered by `tests/integration` and the CI workflow (service containers), which compare Neo4j answers against NetworkX and Postgres results against the in-process store |
| Live LLM providers | Not called (no credentials). The LLM path is tested with scripted providers, including hallucination, invalid JSON and HTTP errors |

**Re-verification on 2026-10-03 (Windows 10, Python 3.13, Docker Desktop; run-specific, not a guarantee):**

| Component | Result |
|---|---|
| Unit, agent and e2e tests | See the Testing section of the README for the exact counts and coverage |
| PostgreSQL 15 (single container) | `alembic upgrade head` → revision 0002, downgrade to 0001 and back; `tests/integration` against it passed, including `PostgresStoreTest` and the audit append-only tests |
| LangGraph | The workflow compiled and ran under the `langgraph` engine in the live API |
| Neo4j, Qdrant | See the Testing section of the README |
| Full `docker compose` stack | Not run |
| GitHub Actions | Not run: the workflow only executes after the repository is pushed to GitHub |

## E. Risk register

| # | Area | Risk | Likelihood | Impact | Mitigation in this build | Residual / next step |
|---|---|---|---|---|---|---|
| R1 | AI | LLM fabricates facts, laws or numbers | High | High | LLM drafts only narrative from an evidence pack. Each claim must cite existing refs; numbers, dates and entity ids are checked against cited evidence; unsupported claims are removed; one retry, then a deterministic fallback; unsupported-claim rate is tracked | The validator checks grounding, not reasoning quality. Keep human review mandatory |
| R2 | AI / legal | Accusatory language, or an automated adverse action | Medium | High | Validator rejects accusatory phrasing and action directives that don't defer to a human. The agent has no write tools beyond its own evidence. The final state is `pending_review` | Periodic review of report samples |
| R3 | Model | Detectors overfit to the synthetic generator | **High** | High | Holdout seed and population alert rate are reported; caveats are documented; weights are labelled engineering defaults | Re-calibrate on real labelled data before any use |
| R4 | Model | Silent drift / bad configuration change | Medium | High | Versioned configs, offline regression gates (recall drop ≤ 0.02, FPR increase ≤ 0.01, F1 not worse), four-eyes approval, one active version enforced by a DB index | Add production monitoring of alert volumes per signal |
| R5 | Data | PII exposure (API, logs, LLM prompts) | Medium | High | Identifiers stored only as salted hashes plus masked values; IP/fingerprint masking at the API; unmask is admin-only and audited; logs carry ids, not payloads; the evidence pack sent to the LLM is masked | Field-level encryption (KMS); DPA with the LLM vendor or a local model (Ollama) |
| R6 | Security | Broken authentication / authorisation | Low | High | JWT HS256 with `exp/iat/iss`; production refuses a missing or short secret; scrypt hashing; role checks at both the API and tool layers; MCP keys map to roles | OIDC/SSO, token revocation list |
| R7 | Security | SQL injection | Low | High | Bound parameters only; dynamic SQL limited to whitelisted column names; path and query ids validated by regex | — |
| R8 | Security | Abuse / DoS | Medium | Medium | Token-bucket rate limiting, upload size and type limits, bounded graph traversals (hub suppression, depth caps), tool timeouts | Per-process limiter; use a gateway limiter for multi-replica deployments |
| R9 | Ops | Graph projection stale vs Postgres | Medium | Medium | Bootstrap projection; `neo4j_backend load` command; readiness check | CDC-based incremental sync |
| R10 | Ops | Synchronous agent runs block workers | Medium | Medium | Per-tool timeouts; uvicorn workers configurable | Job queue + polling endpoint |
| R11 | Architecture | Dependency drift (LangGraph, MCP SDK major versions) | Medium | Medium | Engine-agnostic workflow spec; MCP compatibility import for SDK 1.x and 2.x; CI runs LangGraph parity | Pin versions via a lock file in the user environment |
| R12 | Data | Synthetic policies mistaken for real regulation | Low | High | Every document is labelled synthetic and attributed to a fictional institution; high-risk jurisdiction list is empty by default | Load approved institutional and regulatory sources |
| R13 | Ops | Timeout leaves a worker thread running | Low | Low | Tools are read-mostly; side-effecting tools are single-transaction; the result is discarded | Move heavy tools to cancellable workers |
| R14 | Retrieval | LSA embeddings miss paraphrases | Medium | Medium | Hybrid with BM25/FTS; measured R@5 0.95 on the policy benchmark | Switch to an approved neural embedding model |
| R15 | Monitoring | Alert fatigue from noisy detectors | High | Medium | Detector tiers: baseline-deviation detectors alert only when the combined score is flagged; context signals never alert; deduplication; measured alert volume in EVALUATION.md | Calibrate on real data; tune per institution |
| R16 | Monitoring | Evaluation circularity (scenarios written with the detectors) | **High** | High | Independent benchmark generator with its own seed, evasive positives and hard negatives; results labelled synthetic | Real labelled data |
| R17 | Workflow | Inconsistent state after a partial failure | Low | High | Per-operation PostgreSQL transactions; audit written after commit; DB CHECKs tie status to resolution/closure | In-memory mode has no rollback |
| R18 | Workflow | Unauthorised access to cases | Medium | High | Assignment-based RBAC; admin-only operations | No per-case confidentiality ACL |
| R19 | Deployment | Public demo abuse or data exposure | Medium | Medium | Synthetic data only, no LLM, rate limit, CSP, strong-password requirement, docs off and HSTS in production, production start-up checks, login lockout | Not deployed or reviewed; see DEPLOYMENT.md |
| R20 | Data quality | Bad or missing source data silently distorts alerts | Medium | High | Validation gate with 22 reason codes, quarantine with drill-down, batch accounting identity enforced by a CHECK, coverage only from declared expectations, late and duplicate accounting | Real feeds need their own contracts and reconciliation; only synthetic batches were tested |
| R21 | Metrics | A metric is reported with an invalid denominator (e.g. FPR from alerts) | Medium | High | Operational metrics limited to decided-alert denominators; `not_computed` states what is absent; a v2 mislabelled KPI was corrected; tests assert FPR and recall never appear in operational output | Reviewers must keep the distinction when adding metrics |
| R22 | Triage | Heuristic priority mistaken for a probability or trusted beyond its evidence | Medium | Medium | Labelled "not a probability" in API and UI; factor breakdown stored per alert; evaluation shows it is not better than the customer risk score as a ranking | Calibrate on real outcomes before relying on it |
| R23 | Recovery | Backups exist but cannot be restored, or are lost with the host | Medium | High | `restore-test` and a restored-application check were run and passed; checksums, optional encryption | No schedule, no off-host copy, no PITR; RPO/RTO unmeasured at scale |
| R24 | Config | An unrecorded threshold change alters what becomes an alert | Medium | High | Append-only change log, startup snapshot diff, allow-listed audited overrides | File edits are attributed to `system`; a restart reverts overrides |
| R25 | Ops | Unreachable database hangs start-up | Low | Medium | 5 s connect timeout (a failure test found a 136 s hang), readiness reports schema and latency | Orchestrator restart policies are outside this repository |
