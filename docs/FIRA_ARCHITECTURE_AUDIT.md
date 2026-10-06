# FIRA architecture audit (2026-10-06)

An inspection of the repository **before** this upgrade pass, so that new work reuses what exists instead of replacing it.
Everything is synthetic data; nothing here describes real customers. Evidence is in the files named.

## A. What already works (IMPLEMENTED and tested)

| Area | State | Where |
|---|---|---|
| Data generation and load | seeded synthetic bank (customers, accounts, devices, merchants, transactions, legacy alerts, scenario labels), `COPY` loader into PostgreSQL | `backend/app/synthetic/`, `app/db/loader.py` |
| Ingestion validation | row-level gate with 22 reason codes, batch ledger (`received = processed + rejected + failed`), quarantine, coverage | `app/monitoring/ingest.py`, `service.ingest`, `schema_production.sql` |
| Risk scoring | 19 deterministic detectors, weight x strength with group caps, category breakdown, explanation, supporting transactions, YAML configuration with versions and fingerprint | `app/risk/`, `app/monitoring/risk_view.py`, `GET /api/risk/{customer\|account}/{id}` |
| Alerts | monitoring alerts with dedup, lifecycle, heuristic triage (12 factors, bands), alert-quality metrics | `app/monitoring/` |
| Graph | NetworkX projection from the store (Neo4j optional): clusters, shared devices, paths, cycles, fund tracing, counterparties, shared beneficiaries, fan-in/fan-out search, flow subgraph | `app/graph/`, `routes_network.py` |
| Money-mule indicators | 8 evidence-backed indicators, score, bands | `app/monitoring/mule.py` |
| Investigations | stateful agent (14 nodes, 22 tools), evidence fusion with five evidence classes, claim validator, human decision, `pending_review` boundary | `app/agents/`, `app/tools/` |
| Cases | state machine, assignment rules, notes, evidence, decisions, timeline, workbench | `routes_cases.py`, `app/api/workbench.py` |
| Documents and retrieval | MD/PDF/OCR/DOCX ingestion, BM25 + LSA + RRF hybrid retrieval with provenance | `app/documents/`, `app/retrieval/` |
| LLM abstraction | `NullProvider` default, Anthropic / OpenAI / Ollama over stdlib HTTP, keys from environment | `app/llm/provider.py` |
| Audit | append-only `audit_log` (database triggers), alert/case history, configuration change log | `schema_audit_guard.sql`, `monitoring/governance.py` |
| Security | JWT + scrypt, two roles, RBAC at API and tool layers, rate limiting, lockout, PII masking, production start-up checks | `app/security/`, `docs/SECURITY.md` |
| Frontend | React 19 / TypeScript: dashboard, search, customer, investigation, graph explorer, alert queue, cases, My Work, money-mule view, data quality, operations | `frontend/src/` |
| Quality gates | 289+ backend tests, Vitest, ruff, mypy, bandit, pip-audit, CI workflow (never run on GitHub) | `backend/tests/`, `.github/workflows/ci.yml` |

## B. What was partially implemented (before this pass)

| Gap | Detail |
|---|---|
| Follow-up questions | The agent answers one request per run and writes a report; there was no way to ask a *specific question* ("which transactions first and why?") about a customer and get a structured, cited answer |
| Dataset-level data quality | The ingestion gate judges rows as they arrive; nothing audited the **stored** dataset (orphans, duplicates, invalid amounts and currencies, impossible states) |
| Employer-facing documentation | detailed design docs existed but no short, current-vs-future architecture set |
| Dashboard | no data-quality status tile; "high-risk customers" is not a cheap query (see Limitations) |

## C. What is missing and was NOT added (deliberately)

* Kafka, Spark, Databricks, Azure/AWS services, dbt, Airflow: none exist in the codebase and none were added for appearance. They appear in the architecture document only under *future production-scale architecture*.
* A separate "customer-level risk factor" taxonomy distinct from the detectors: the existing detector contributions are the risk factors; a second taxonomy would duplicate and could disagree with them.
* New authentication complexity, MFA/SSO, per-case confidentiality.

## D. What should not be changed

* Detector logic, weights and risk configuration (evaluated and versioned).
* The alert/case state machines, the append-only ledgers and their triggers.
* The agent's evidence-fusion and claim-validation design.
* The ingestion gate's reason codes and batch accounting (tests and docs depend on them).

## E. Architectural risks found

1. **Derived state is rebuilt in memory at start-up** (graph, vector index): cold starts are heavy and ingestion rebuilds the whole graph.
2. **Dataset audit cost grows with table size** (it reads the needed columns into pandas). Fine at 250k transactions; not designed for hundreds of millions of rows.
3. **LLM text is the only non-deterministic component.** The risk was it asserting entities that do not exist: handled by the allow-list validation in the copilot (see `docs/ai-investigation-copilot.md`).
4. Per-process security state (lockout, revocation, rate limits).

## F. Highest-value improvements (and what this pass did)

| Improvement | Done |
|---|---|
| Grounded Q&A copilot with OBSERVED FACT / DERIVED SIGNAL / AI INTERPRETATION / RECOMMENDATION sections, entity references, validation of AI text, safe degradation, audit | **yes** (`app/copilot/`, `POST /api/copilot/ask`, UI panel) |
| Stored-dataset data-quality audit with computed counts and a score, API and UI | **yes** (`app/data/quality.py`, `GET /api/data-quality/dataset`) |
| Documentation set separating current implementation from future architecture | **yes** (`docs/ARCHITECTURE.md` (overview section) and companions) |
| README positioning for employers | **yes** |
| Rework of risk scoring, graph and investigation workflow | **no change needed**: the existing implementations already meet the stated requirements (see the mapping in `docs/ARCHITECTURE.md` (overview section)) |

## Mapping of the requested phases to the repository

| Phase | Status | Evidence |
|---|---|---|
| 2 Target architecture | documented; implemented stack is the diagram's left-to-right path with PostgreSQL, deterministic engines and the agent; big-data tooling is future | `docs/ARCHITECTURE.md` (overview section) |
| 3 Explainable risk scoring | **already implemented**; factors, contribution, evidence and explanation are in `GET /api/risk/...` and the customer page | `risk_view.py`, `docs/risk-engine.md` |
| 4 Graph intelligence | **already implemented**; the copilot now surfaces graph findings with citations | `docs/aml-investigation.md` |
| 5 Investigate Customer | **already implemented** (customer page button, agent, case workbench) | `docs/aml-investigation.md` |
| 6-7 AI copilot, structured response | **implemented in this pass** | `docs/ai-investigation-copilot.md` |
| 8 Data quality | ingestion gate (existing) + stored-dataset audit (**new**) | `docs/data-quality.md` |
| 9 Investigation evidence | existing evidence classes; AI output cannot be attached as primary evidence (copilot responses are never persisted as evidence) | `docs/aml-investigation.md` |
| 10 Audit | existing events plus `ai_investigation_requested`, `ai_response_generated` (**new**) | `docs/SECURITY.md` |
| 11 Frontend | copilot panel, dataset audit, dashboard tile (**new**); rest existing | `frontend/src/` |
| 12 Security review | review recorded in `docs/SECURITY.md` | |
| 13 Testing | 29+ new tests for the copilot and dataset audit | `backend/tests/unit/test_copilot.py`, `test_dataset_quality.py` |
