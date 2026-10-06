# Production-oriented architecture (FIRA v3)

**Positioning.** FIRA is a *production-oriented transaction-monitoring and investigation platform prototype* that
demonstrates production engineering practices on **synthetic data**. It is not a production banking system, is not
regulatory-certified, and nothing here is evidence of real-world AML effectiveness. "Production-oriented" means: the
engineering practices (data validation, observability, security hardening, CI quality gates, backup and restore,
failure handling, performance measurement) are present and tested; it does **not** mean it has been operated in
production.

Tags used below: **EXISTING** (kept as is), **MODIFIED**, **NEW**, **OPTIONAL** (behind configuration or not required).

Sections 1 to 13 were written **before** the v3 implementation, as the audit and design, and are kept as written. Section 14
records what was actually built and where it deviated.

## 1. Existing architecture (audit)

```
generator → data/seeds → FrameStore (pandas, in-memory) | PostgreSQL (SqlStore)
   RiskEngine (19 detectors) · GraphBackend (NetworkX | Neo4j) · Hybrid retrieval (BM25+LSA+RRF)
   ToolRegistry (22 tools) → InvestigationAgent (14 nodes) → report → decision
   v2: ingestion (validated) → monitoring run → alerts (dedup, lifecycle) → cases → workbench → decision → audit
   FastAPI (JWT, RBAC, rate limit, PII masking) · React/Vite UI · MCP server
```

A modular monolith; optional components (Neo4j, Qdrant, LLM) are behind configuration.

## 2. Existing capabilities

- **Detection:** 19 deterministic detectors including STRUCTURING and FAN_OUT; alert tiers; explainable scores with
  category breakdown (EXISTING).
- **Alerts and cases:** `monitoring_alerts` with deduplication (unique index), lifecycle state machine, history; cases,
  notes, evidence, timeline, decisions; atomic operations in PostgreSQL mode (EXISTING, v2).
- **Graph:** counterparties (degree 1 and 2), shared beneficiaries, common recipients, cycles, shortest path, fund
  tracing, clusters (EXISTING).
- **Evaluation and performance:** independent monitoring benchmark, ablation, performance benchmark (EXISTING, v2).
- **Security:** JWT, scrypt, two roles, RBAC at API and tool layers, rate limit, PII masking, CORS, security headers
  (EXISTING).
- **Audit:** `audit_log` with database triggers rejecting update/delete (EXISTING).

## 3. Existing weaknesses (what v3 addresses)

| # | Weakness | Evidence |
|---|---|---|
| W1 | **No monitoring-data-quality or coverage layer.** Ingestion validates rows but does not account for batches: nothing records expected vs received vs processed, rejected rows are only returned once in the API response (not stored or drillable), there is no duplicate/late/missing accounting and no coverage metric | `app/monitoring/ingest.py`, `service.ingest` |
| W2 | **No alert triage.** Alerts have severity and a customer risk score, but investigators cannot rank by a defined priority; `risk_score` is the customer's total, not an alert-prioritisation score | `monitoring_alerts` columns |
| W3 | **Alert quality is thinly measured.** KPIs show a false-positive share of resolved alerts, with no formal definitions, no precision/false-discovery rate, and no statement of when FPR/recall are invalid | `build_kpis` |
| W4 | **Money-mule logic is scattered** across FAN_IN, FAN_OUT, RAPID_PASS_THROUGH and CIRCULAR_FLOW signals; there is no consolidated, explainable mule-risk view or flow visualisation | engine, workbench |
| W5 | **Case management gaps:** no priority change or explicit reassign endpoint semantics, no investigator work queue (my alerts, overdue, recently decided) | `routes_cases.py` |
| W6 | **Observability is partial.** Metrics cover HTTP and the agent only (no alerts, cases, transactions, detection latency, DB latency); log lines lack a `service`, `operation` and uniform `duration_ms`; `/metrics` needs an admin JWT (awkward for scrapers) | `core/observability.py` |
| W7 | **Security hardening gaps:** no request-size limit inside the app (nginx only), rate-limiter buckets are never evicted, no login lockout, no token revocation, `/docs` always public, no HSTS, production validation limited to the JWT secret | `main.py`, `ratelimit.py`, `auth.py` |
| W8 | **Configuration changes are not governed.** Risk-config proposals are audited, but monitoring/triage thresholds and detector enable/disable are files with no change record | `monitoring_config.yaml` |
| W9 | **No backup, restore or disaster-recovery capability.** Docker volumes only; no scripts, no documented RPO/RTO, no restore test | repository search |
| W10 | **No failure-mode tests** (database down, Qdrant down, bad config, restart, bad migration) | tests |
| W11 | **No performance regression baseline**; no memory measurement; benchmarks stop at 254k transactions | `app/monitoring/benchmark.py` |
| W12 | **CI has no coverage gate** and does not exercise backup/restore | `.github/workflows/ci.yml` |
| W13 | The evaluation dataset lacks mule-network, rapid-chain and fan-in-aggregator scenarios and their benign look-alikes; triage quality is not evaluated | `monitoring_benchmark.py` |

## 4. Existing database schema (EXISTING)

Migrations 0001 (19 tables, constraints), 0002 (audit append-only triggers), 0003 (`monitoring_runs`,
`monitoring_alerts`, `monitoring_alert_transactions`, `monitoring_alert_events`, `cases`, `case_events`, `case_notes`,
`case_evidence`; partial unique indexes for one unresolved alert per customer+detector and one unclosed case per customer;
CHECK constraints tying status to resolution/closure; append-only triggers on four history tables). Indexes exist for
transaction time, sender/receiver+time, alert status/severity/detector/customer/assignee/risk/case, case status/assignee,
audit time and user. See `docs/DATA_MODEL.md`.

## 5. Existing APIs (EXISTING)

Original investigation, risk, graph, documents, evaluation, config and audit endpoints; v2 monitoring (runs, ingest,
alerts, kpis, detectors), cases (workbench, notes, evidence, decision, investigate), network and activity-window
endpoints; `/health`, `/ready`, `/health/ready`, `/metrics` (admin). Pagination (`limit` ≤ 200, `offset`), filters and
sort whitelists on the alert and case lists. See `docs/API.md`.

## 6. Existing deployment model

Docker Compose (PostgreSQL/PostGIS, Neo4j, Qdrant, backend, nginx frontend, ports bound to 127.0.0.1); a Render blueprint
and single-image Dockerfile (written, **never deployed**); bootstrap runs migrations, loads data, indexes documents.

## 7. Existing security controls

JWT HS256 with `exp/iss/jti`, scrypt hashing, `analyst`/`admin` roles, assignment-based write access to alerts and cases,
bound SQL parameters, `extra=forbid` request models, rate limiting (process-local), PII masking, CORS allow-list, security
headers, CSP for the served UI, secrets from environment, CI secret-pattern scan, bandit, pip-audit. Known gaps: W7 above and
those in `docs/SECURITY.md`.

## 8. Existing observability

Structured JSON logs with request/user/investigation/run correlation ids; an in-process Prometheus-text metrics registry
(HTTP request counts and latency, agent runs); `/health` and `/ready` (database, graph, vector store, monitoring store).

## 9. Existing testing

156 unit/agent/e2e tests (78% line coverage), 56 PostgreSQL/Qdrant integration tests, 14 Vitest tests; ruff, mypy, bandit,
pip-audit and npm audit clean at the last verification.

## 10. Existing CI/CD

GitHub Actions: lint/type-check (ruff, mypy, tsc, build, vitest, npm audit), unit tests with coverage, integration tests with
PostgreSQL/Neo4j/Qdrant service containers, security job (bandit, pip-audit, secret scan), image builds, publish on main.
Never run (repository not yet pushed).

## 11. Existing backup and recovery capability

**None.** Compose volumes persist data across restarts but there is no backup, no restore procedure, no RPO/RTO and no
restore test. The Render blueprint relies on the provider's database offering, which on the free plan has no
production-grade backups.

## 12. Target architecture

```
transactions ─► [NEW] data-quality gate: validate · batch accounting · quarantine (rejected store) · coverage
        └► monitoring (EXISTING) ─► rule engine (EXISTING detectors)
                                  ├ behaviour analysis (EXISTING baselines, activity windows)
                                  └ network analysis (EXISTING graph + [NEW] money-mule indicators)
              └► alert generation (EXISTING) ─► [NEW] explainable triage score + priority
                    └► [NEW] investigator queue ("my work") ─► case management (EXISTING + MODIFIED)
                          └► investigation: transactions · graph · evidence (EXISTING; [NEW] money-mule view)
                                └► human decision ─► audit trail (EXISTING) ─► [NEW] alert-quality metrics · KPIs
cross-cutting: [MODIFIED] observability · [MODIFIED] security hardening · [NEW] configuration governance ·
               [NEW] backup/restore · [NEW] failure tests · [MODIFIED] performance + regression baseline · [MODIFIED] CI gates
```

### 12.1 Design decisions

- **Modular monolith, no new services.** No Kafka, Kubernetes, microservices or ML models are added. Everything new is
  deterministic Python plus PostgreSQL tables.
- **Data-quality gate sits in front of monitoring** and is the system of record for "what did we receive and what happened to
  it". Rejected rows are stored (with a reason code and a sanitised copy of the row) so operators can drill down.
- **Triage is a heuristic score, not a probability.** Twelve documented factors with fixed maximum points summing to 100;
  priority bands CRITICAL/HIGH/MEDIUM/LOW at documented thresholds. It uses only information available when the alert is
  raised (and the alert's age, recomputed); it never reads the alert's own outcome or any ground-truth label.
- **Alert-quality metrics are defined only where valid.** Operationally there are no true negatives for alerts, so FPR and
  recall are *not* computed from the alert workflow; precision, false-discovery rate, confirmation rate, closure rate and
  disposition rate are. FPR and recall appear only in the labelled synthetic evaluation, where true negatives are defined.
- **Money-mule risk is a consolidated indicator view** built on the existing detectors, graph backend and transaction
  frames (no second graph implementation). It reports indicators with evidence, never a verdict.
- **Backups use `pg_dump`/`pg_restore`** wrapped in scripts that also write a manifest (row counts, migration revision,
  checksum), optional passphrase encryption, retention pruning and a verification step; a real restore test is performed.
- **Configuration governance** records changes by comparing a canonical snapshot of every governed configuration with the
  last recorded one, plus explicit rows for API-driven changes; the log is append-only.

### 12.2 Database changes (migration 0004, as designed)

`monitoring_alerts`: `triage_score`, `triage_priority`, `triage_factors` (jsonb), `triage_computed_at`.
`ingestion_batches` (per-batch accounting), `rejected_transactions` (quarantine with reason codes),
`config_change_log` (append-only), indexes for triage ordering and the investigator queue. Only indexes with a real query
behind them are added; before/after timings are recorded in `docs/PERFORMANCE.md`.

### 12.3 API changes (as designed)

NEW: `/api/data-quality/{summary,batches,rejected}`; `/api/monitoring/triage/*`, `/api/monitoring/quality`,
`/api/monitoring/my-work`; `/api/mule/*`; case priority update; `/api/config/changes`; `POST /api/auth/logout`.
MODIFIED: alert list (priority filter and sort), alert detail (triage factors), ingestion (batch accounting, reason codes),
`/ready` (schema version), `/metrics` (business metrics; optional scrape token), OpenAPI tags.

### 12.4 Frontend changes (as designed)

NEW pages: My Work, Data Quality, Money-mule flow view (also a workbench tab). MODIFIED: alert queue (priority, triage
sort/filter), alert detail (factor breakdown), dashboard (operations KPIs from real calculations only).

### 12.5 Testing strategy

Unit tests per rule and boundary; PostgreSQL integration tests mirroring the in-memory ones; security tests (RBAC, lockout,
size limit, revocation, production validation); failure tests (database down, Qdrant down, bad config, restart); a real
backup-and-restore test; a leakage test proving detectors, triage and mule logic never read ground-truth labels; benchmark
regression check against a stored baseline (same machine only).

### 12.6 Operational strategy

Environments `development`, `test`, `production` (the public demo uses `production` settings with synthetic data). Production
refuses to start with unsafe settings. Health = liveness; readiness = dependencies and schema version. Backups daily with a
documented retention; RPO and RTO are *targets* until measured.

## 13. Known conflicts with the request (decisions)

| Request | Decision |
|---|---|
| "False positive rate = FP / (FP + TN)" for alerts | Not valid in the alert workflow (no defined TN). Reported only in the labelled evaluation; operational metrics use precision, false-discovery rate, confirmation rate |
| Free hosting backups | The Render free database has no production-grade backups; the repository ships scripts and documents an external-storage procedure rather than pretending |
| 1,000,000-transaction benchmark | Attempted only if the machine can hold it safely; the development machine could not previously (7.9 GB RAM) |
| "Previous investigator outcomes" in triage | Used only from the same customer's *earlier* closed cases, never from the alert's own outcome |
| Refresh tokens | Not added: access tokens are short-lived (60 min) with logout revocation; refresh adds attack surface without a need in this prototype |

## 14. As built: what was delivered and where it deviated from the design

### 14.1 Delivered

| Area | Delivered | Where |
|---|---|---|
| Data-quality gate | per-row validation with 22 reason codes in four groups; batch ledger (`ingestion_batches`: expected, received, processed, rejected, duplicates, malformed, late, failed, missing, ids generated; `CHECK received = processed + rejected + failed`); quarantine (`rejected_transactions`); coverage and processing success computed from stored values; `/api/data-quality/*`; Data Quality page with drill-down | `monitoring/ingest.py`, `service.ingest`, `routes_quality.py` |
| Triage | 12 factors summing to 100, bands, stored factors, priority-change events, recompute, queue filter and sort | `monitoring/triage.py` |
| Alert quality | confirmation / false-discovery / closure rates, `not_computed` for FPR and recall, outcome feedback list, My Work | `monitoring/quality.py`, [ALERT_QUALITY.md](ALERT_QUALITY.md) |
| Money-mule view | 8 indicators with evidence, score and bands, flow subgraph, fan-in/fan-out graph search, suspects list, benign context notes | `monitoring/mule.py`, `graph/networkx_backend.py` |
| Case management | priority change (reason required), `my-work`, existing transitions/assignment/notes/evidence/decision/timeline unchanged | `routes_cases.py` |
| Configuration governance | append-only change log, startup snapshot diff, runtime overrides with allow-list, detector switch | `monitoring/governance.py`, `routes_config.py` |
| Observability | `service`/`env`/`operation`/`duration_ms` log fields; business gauges; DB latency; schema revision in readiness; scrape token | `core/observability.py`, `routes_core.py` |
| Security hardening | lockout, logout revocation, body limit, production checks, docs off in production, HSTS, bounded limiter, least-privilege role | `security/hardening.py`, `infrastructure/sql/` |
| Backup and DR | backup/verify/restore/restore-test with manifests, encryption, pruning; restored-app verification; [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md) | `infrastructure/scripts/` |
| Failure tests | database down/unreachable, killed connections, restart, schema behind, bad migration, Qdrant down, invalid config, isolated failures | `tests/unit/test_failure_modes.py`, `tests/integration/test_failure_modes_postgres.py` |
| Performance | adds memory, triage, mule, data-quality and gate-with-rejections timings; regression comparison; index review | `monitoring/benchmark.py`, `infrastructure/scripts/index_review.py`, [PERFORMANCE.md](PERFORMANCE.md) |
| Evaluation | money-mule family in the benchmark, triage and mule evaluation, holdout seed | [EVALUATION.md](EVALUATION.md) Part 1b |
| CI | coverage floor, migration reversibility, backup/restore step, advisory performance smoke | `.github/workflows/ci.yml` |
| API documentation | one documented tag per operation (health, metrics, auth, transactions, monitoring, alerts, triage, risk, cases, investigations, evidence, graph, data-quality, config, evaluation, audit) | `api/tags.py`, `tests/unit/test_openapi.py` |

### 14.2 Deviations from the design, and corrections found on the way

1. **A v2 metric was mislabelled and is corrected.** The dashboard KPI "false-positive rate" divided false positives by *resolved
   alerts*, which is a false-discovery rate. It is renamed (`false_discovery_rate`, plus `confirmation_rate`) and counts cleared
   as well as false-positive resolutions. No FPR is computed from the alert workflow anywhere.
2. **Triage was redesigned once after it failed its own evaluation** (non-monotonic, worse than its largest input). The first-design
   result file is kept. Weights and thresholds were then fixed and the holdout was run afterwards: see
   [ALERT_QUALITY.md](ALERT_QUALITY.md) and [EVALUATION.md](EVALUATION.md).
3. **The mule view was changed once for the same reason** (a network indicator that fired for most ordinary customers; salary
   counted as pass-through). It is **not** a better classifier than the existing fan-in / rapid-pass-through alerts (holdout F1 0.56
   vs 0.72); it ranks well (AUC 0.97) and adds evidence and a flow picture.
4. **Index review changed the migration.** Measured on 300,000 synthetic alerts: the triage indexes needed `NULLS LAST` to match the
   repository's `ORDER BY` (otherwise unused), a non-partial variant was 6x larger with no gain, and the single-column
   `ix_cases_assigned` became redundant and is dropped. Queue top-50: 60 to 75 ms → 0.16 ms; my open alerts 10 to 12 ms → 1.0 ms;
   recently resolved 118 ms → 0.6 ms. Details in [PERFORMANCE.md](PERFORMANCE.md).
5. **A failure test found a real defect:** start-up against an unreachable database took 136 s because no connect timeout was set.
   Fixed (5 s default, `DB_CONNECT_TIMEOUT_S`).
6. **Runtime configuration overrides are not persisted.** The YAML files remain the source of truth; a restart reverts an
   override and logs the revert. Persisting overrides would make the database a second source of truth.
7. **Money-mule workbench tab not built.** The flow view is a standalone page (`#/mule/CUST-…`) linked from the alert triage card
   and the Money-mule View menu entry; it is not embedded in the case workbench. Mule graph queries are NetworkX-only (Neo4j
   backend returns 501 for them).
8. **Refresh tokens: not built** (as decided in section 13); logout revocation is per process.
9. **1,000,000-transaction benchmark: not run.** The machine has 7.9 GB of RAM with about 1.9 GB free while Docker runs; the
   in-memory generator and store cannot hold it safely. Measured sizes are in [PERFORMANCE.md](PERFORMANCE.md).
10. **Not exercised:** GitHub Actions, the full Docker Compose stack, the Render deployment, the Neo4j backend. The restricted
    database role is tested, but the Render blueprint still connects as the owner.
11. **Targets, not achievements:** RPO 24 h, RTO 1 h. Only the restore *step* has been timed (21 s on a 15,000-transaction database).

