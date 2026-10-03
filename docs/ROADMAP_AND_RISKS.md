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

**Next steps (not built):**

- **Alert creation from FIRA risk scores.** Today the `alerts` table contains only seeded synthetic
  legacy-rule alerts; the risk engine scores subjects on demand (API, agent) and does not write
  alerts. A scheduled or event-driven job that scores customers and persists alerts with links to the
  contributing signals and transactions is not implemented.
- Token revocation, per-user lockout and a shared (gateway or Redis) rate limiter. See the Known
  limitations in [SECURITY.md](SECURITY.md).
- Cryptographic tamper-evidence for the audit log (hash chain or external anchoring). The current
  protection is trigger-enforced append-only only.
- Frontend tests (none exist; the UI is type-checked and built only).
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
