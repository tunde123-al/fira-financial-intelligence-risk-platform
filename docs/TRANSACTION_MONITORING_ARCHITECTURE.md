# Transaction monitoring architecture (FIRA v2)

Status: **implemented** (this document started as the design and is updated to match the code; deviations
are listed in section 16). Every item is tagged:

| Tag | Meaning |
|---|---|
| **EXISTING** | already in the repository and kept as is |
| **MODIFIED** | exists, changed by v2 (backward compatible unless stated) |
| **NEW** | added by v2 |
| **OPTIONAL/FUTURE** | deliberately not built, or built behind a flag |

FIRA is a synthetic-data portfolio prototype. Nothing here is a production banking or regulatory
compliance system.

## 1. Existing architecture (audit)

### 1.1 Data flow today

```
synthetic generator ─► data/seeds ─► FrameStore (pandas)  | PostgreSQL (SqlStore)
                                          │
        RiskEngine (17 detectors) ◄───────┤  on demand, per customer (API / agent)
        GraphBackend (NetworkX|Neo4j) ◄───┤
        Hybrid retrieval (BM25+LSA+RRF) ◄─┘
                      │
        ToolRegistry (22 tools) ─► InvestigationAgent (14 nodes) ─► report ─► pending_review
                                                                         │
                                          analyst decision (confirm/reject/escalate/more evidence)
                                          audit_log (append-only triggers in PostgreSQL mode)
```

### 1.2 Capabilities (EXISTING)

- **Risk detectors (17):** TRANSACTION_BURST, VELOCITY_SPIKE, AMOUNT_DEVIATION, PEER_AMOUNT_DEVIATION,
  RAPID_PASS_THROUGH, FAN_IN, GEO_NEW_COUNTRY, IMPOSSIBLE_TRAVEL, NEW_DEVICE, DEVICE_SHARING,
  SHARED_IDENTIFIER, CIRCULAR_FLOW, DORMANT_REACTIVATION, HIGH_RISK_MERCHANT, BEHAVIOURAL_SHIFT,
  NETWORK_EXPOSURE, HISTORICAL_ALERTS, plus optional ML_ANOMALY. Deterministic; weights, thresholds
  and group caps in `backend/app/risk/default_config.yaml` (version `default-1`).
- **Scoring:** `weight × strength`, group caps, cap 100, bands (low/medium/high/critical).
- **Data model:** 19 PostgreSQL tables (migrations 0001 schema, 0002 audit append-only triggers).
- **Alerts:** the `alerts` table holds **seeded synthetic legacy-rule alerts** only. No code creates
  alerts. `HISTORICAL_ALERTS` and `NETWORK_EXPOSURE` read this table.
- **Investigation workflow:** agent run → `investigations` row, evidence items (`E1…`), report with
  validated claims, status `pending_review`; analyst decision endpoint maps
  confirm/reject/escalate/request_more_evidence to status + conclusion.
- **APIs:** `/api/search`, `/api/customers/*`, `/api/risk/{type}/{id}`, `/api/alerts` (legacy list),
  `/api/investigations/*`, `/api/agent/investigate`, `/api/graph/*`, `/api/documents/*`,
  `/api/evaluation/*`, `/api/config/*`, `/api/audit`, `/health`, `/health/ready`, `/metrics`.
- **Frontend (React/Vite):** Dashboard, Investigation Search, Customer, Investigations,
  Investigation workspace (8 tabs), Graph Explorer, Document Search, Evaluation, Audit Log.
- **Audit:** every tool call, login and decision; PostgreSQL triggers reject UPDATE/DELETE/TRUNCATE
  on `audit_log`. Not cryptographically tamper-proof.
- **Evaluation:** risk P/R/F1/FPR/AUC on generator ground truth, retrieval, agent/RAG metrics. The
  generator and detectors were co-designed, so results are optimistic (documented).
- **Auth/RBAC:** JWT, roles `analyst` < `admin`, enforced at API and tool layers; rate limit; PII
  masking.

### 1.3 Gaps this upgrade addresses

1. Nothing ingests transactions or runs detection automatically.
2. No FIRA-generated alerts, no alert lifecycle, no deduplication.
3. No case management; no assignment; no notes.
4. No structuring or fan-out detector; no per-detector metadata (name/category/explain).
5. No alert/case KPIs on the dashboard.
6. Evaluation does not cover the alert pipeline and shares its design with the generator.
7. No performance measurements; no deployment recipe beyond Docker Compose.

## 2. Proposed architecture (v2)

```
Transactions ─► ingestion (NEW) ─► monitoring run (NEW)
                                        │ affected customers
                                        ▼
                         RiskEngine (MODIFIED: +STRUCTURING, +FAN_OUT, category breakdown)
                                        │ triggered detectors
                                        ▼
                  alert generation + deduplication (NEW) ─► monitoring_alerts (NEW)
                                        │
                  alert lifecycle (NEW): NEW → TRIAGED → INVESTIGATING → ESCALATED → RESOLVED
                                        │ create/attach
                                        ▼
                  cases (NEW) ─► workbench (NEW): profile, risk, rules, transactions, activity,
                                 counterparties, graph, alerts, documents, evidence, notes, timeline
                                        │ (reuses EXISTING agent, graph, retrieval)
                                        ▼
                  investigator decision (NEW vocabulary) ─► audit trail (EXISTING mechanism)
                                        ▼
                  KPIs / dashboard (MODIFIED) · evaluation (NEW) · benchmarks (NEW)
```

Principle: the LLM never decides whether something is suspicious. Detection, alerting, scoring and
lifecycle are deterministic code. The optional LLM only drafts narrative from validated evidence
(EXISTING behaviour, preserved).

### 2.1 Why monitoring alerts are a separate table (design decision)

The existing `alerts` table is the **upstream legacy-rule alert feed** the synthetic dataset ships
with. Two existing detectors read it (`HISTORICAL_ALERTS`, `NETWORK_EXPOSURE`) and the evaluation
harness depends on it. Writing FIRA's own alerts into the same table would (a) feed FIRA's output back
into its own inputs and contaminate the evaluation, and (b) change existing behaviour. v2 therefore
adds `monitoring_alerts` (**NEW**). The legacy table and endpoints are **EXISTING** and unchanged. In
the UI the two are labelled "FIRA monitoring alerts" and "legacy seeded alerts".

## 3. Detection framework

### 3.1 Detector catalogue (MODIFIED / NEW)

Detectors are the existing risk-engine signals. v2 adds metadata and a structured result on top and
does not duplicate logic.

| Field | Source |
|---|---|
| `detector_id`, `name`, `description`, `category`, default `severity` | **NEW** catalogue (`app/monitoring/detectors.py`) |
| `enabled`, thresholds | EXISTING YAML (`default_config.yaml`, now version `default-2`) |
| `evaluate()` | EXISTING `RiskEngine._<detector>` methods, executed by `RiskEngine.assess_customer` |
| `explain()` | **NEW**, builds the human-readable explanation from the signal (observed, baseline, threshold, unit, evidence refs) |

| Category | Detectors |
|---|---|
| transaction_behaviour | TRANSACTION_BURST, VELOCITY_SPIKE, AMOUNT_DEVIATION, PEER_AMOUNT_DEVIATION, **STRUCTURING (NEW)**, HIGH_RISK_MERCHANT, DORMANT_REACTIVATION, BEHAVIOURAL_SHIFT |
| fund_flow | RAPID_PASS_THROUGH, FAN_IN, **FAN_OUT (NEW)**, CIRCULAR_FLOW |
| geographic | GEO_NEW_COUNTRY, IMPOSSIBLE_TRAVEL |
| device_identity | NEW_DEVICE, DEVICE_SHARING, SHARED_IDENTIFIER |
| network | NETWORK_EXPOSURE |
| history | HISTORICAL_ALERTS |
| anomaly_model | ML_ANOMALY (optional) |

New detectors (config in `default_config.yaml`):

- **STRUCTURING:** several completed outbound cash-type or transfer transactions, each between
  `lower_ratio × reporting_threshold_usd` and the threshold, within `window_hours`, summing to at
  least the threshold. The threshold is a configurable, **illustrative** value (default USD 10,000);
  it is not a statement of any real regulation.
- **FAN_OUT:** distinct outbound counterparties in the window versus a baseline rate (mirror of
  FAN_IN).

Not implemented (and why): *unusual account balance movement* — the dataset stores a single balance
snapshot per account, not a balance history, so there is nothing to measure.

**Alert tiers.** Not every detector should raise an alert on its own. Each detector has a tier in
`app/risk/catalog.py`:

| Tier | Detectors | Rule |
|---|---|---|
| `standalone` | STRUCTURING, TRANSACTION_BURST, RAPID_PASS_THROUGH, FAN_IN, FAN_OUT, CIRCULAR_FLOW, IMPOSSIBLE_TRAVEL, DEVICE_SHARING, DORMANT_REACTIVATION, HIGH_RISK_MERCHANT | a triggered detector raises an alert |
| `supporting` | AMOUNT_DEVIATION, PEER_AMOUNT_DEVIATION, VELOCITY_SPIKE, GEO_NEW_COUNTRY, NEW_DEVICE, SHARED_IDENTIFIER | alert only if the customer's combined score is at least the investigation threshold (default 40) |
| `context` | HISTORICAL_ALERTS, NETWORK_EXPOSURE, BEHAVIOURAL_SHIFT, ML_ANOMALY | add to the score, never alert |

*Why:* a first version alerted on any triggered detector. On a 1,500-customer smoke dataset (seed 2024) that gave
precision 0.17 and a false-positive rate of 0.17, almost entirely from the three baseline-deviation detectors
(new country, new device, unusual amount), which fire on ordinary behaviour such as travel or a phone
upgrade. The tiers were introduced as a design decision from that observation (they are by-kind, not numeric
thresholds tuned on a dataset), and the reported evaluation uses **different seeds** that were not used to make
that decision. This is disclosed in EVALUATION.md.

### 3.2 Detection result (NEW)

```json
{ "detector_id": "FAN_IN", "triggered": true, "severity": "high", "risk_contribution": 18.0,
  "reason": "...", "transaction_ids": ["TXN-…"], "customer_id": "CUST-…", "account_ids": ["ACC-…"],
  "observed": 21, "baseline": 0.67, "threshold": 8, "unit": "counterparties",
  "window_start": "…", "window_end": "…" }
```

**`confidence` is intentionally omitted.** The existing engine attaches a heuristic constant (0.6 /
0.85 / 0.9) that is not a calibrated probability, so exposing it would imply statistical meaning it
does not have. The data-quality flag `thin_baseline` is exposed instead.

### 3.3 Monitoring run (NEW)

`MonitoringService.run(window_end, lookback_days, customer_ids | active_days)`:

1. Select customers: given ids, or customers with at least one transaction in the last
   `active_days` (default 1) before `window_end`.
2. Assess each with the risk engine (`lookback_days`, default 30, baseline 90).
3. For each triggered **primary** detector, create or update an alert (section 4).
4. Record a `monitoring_runs` row (counts, config version and fingerprint, duration).

Transaction ingestion (`POST /api/monitoring/transactions`, admin) validates a batch (accounts exist,
amounts positive, ids unique), stores it, rebuilds the in-memory graph projection, and optionally runs
monitoring for the affected customers. Bulk loading for benchmarks uses the existing COPY loader.

## 4. Alerts

### 4.1 Schema (NEW, migration 0003)

`monitoring_alerts`: `alert_id` (`MAL-…`), `run_id`, `customer_id` (FK), `account_id` (FK, nullable),
`transaction_id` (FK, nullable; first triggering transaction), `detector_id`, `alert_type`,
`category`, `severity`, `risk_score` (customer's overall score when raised), `risk_contribution`
(detector points), `status`, `description`, `explanation` (jsonb), `occurrence_count`,
`window_start`, `window_end`, `triggered_at`, `created_at`, `updated_at`, `last_seen_at`,
`assigned_to`, `case_id` (FK), `resolution`, `resolution_reason`, `resolved_at`, `resolved_by`.

`monitoring_alert_transactions (alert_id, transaction_id)` links alerts to the transactions that
caused them (foreign keys to `transactions`). `monitoring_alert_events` is the append-only history of
every change.

### 4.2 Severity

Detector severity from signal strength (EXISTING `severity_of`: low < 0.65 ≤ medium < 0.85 ≤ high),
raised to `critical` when the customer's overall score is in the `critical` band (≥ 75).

### 4.3 Deduplication policy (exact)

Key: `(customer_id, detector_id)`.

1. **Unresolved alert exists** (status ≠ RESOLVED): update it. Union the transaction ids, increment
   `occurrence_count`, set `last_seen_at`, `window_end`, and keep the highest `risk_score`,
   `risk_contribution` and severity. No new alert. Enforced by a partial unique index
   `(customer_id, detector_id) WHERE status <> 'RESOLVED'`.
2. **Only resolved alerts exist** and every triggering transaction is already linked to a resolved
   alert for the same key: suppress (same underlying behaviour was already adjudicated). Counted as
   `suppressed` in the run.
3. Otherwise create a new alert.

Consequence: re-running a monitoring run over the same data is idempotent.

### 4.4 Alert lifecycle (state machine)

```
NEW ─► TRIAGED ─► INVESTIGATING ─► ESCALATED ─► RESOLVED (terminal)
 │        │  ▲            │  ▲          │
 │        │  └── (return) ┘  └──────────┘ (de-escalate to INVESTIGATING)
 └─► any non-terminal state may go directly to ESCALATED or RESOLVED
```

Allowed transitions: NEW → TRIAGED | INVESTIGATING | ESCALATED | RESOLVED; TRIAGED → INVESTIGATING |
ESCALATED | RESOLVED; INVESTIGATING → ESCALATED | RESOLVED; ESCALATED → INVESTIGATING | RESOLVED.
RESOLVED has no outgoing transitions. **Resolution outcomes** (only with RESOLVED, reason required):
`CLEARED`, `FALSE_POSITIVE`, `CONFIRMED_SUSPICIOUS`. "Escalated" is a **status**, not an outcome, so
a resolved alert can never also be escalated. A database CHECK ties `resolution` to
`status = 'RESOLVED'`.

## 5. Cases

### 5.1 Schema (NEW, migration 0003)

`cases` (`case_id`, `case_number` `FC-YYYY-NNNNNN` from a sequence, `customer_id`, `status`,
`priority`, `title`, `assigned_to`, `investigation_id` (FK to the existing investigation/agent run),
`opened_at`, `created_at`, `updated_at`, `closed_at`, `decision`, `decision_reason`, `decided_by`),
`case_notes` (append-only), `case_evidence`, `case_events` (append-only). A unique partial index
allows **one unclosed case per customer**; new alerts for that customer attach to it.

### 5.2 Case states

```
OPEN ─► INVESTIGATING ─► PENDING_REVIEW ─► CLOSED (terminal)
  │          │   ▲              │
  │          ▼   │              └─► INVESTIGATING (returned)
  │       ESCALATED ─► INVESTIGATING | PENDING_REVIEW | CLOSED
  └─► CLOSED (only with a decision)
```

### 5.3 Decisions (NEW vocabulary)

`CLEARED`, `FALSE_POSITIVE`, `CONFIRMED_SUSPICIOUS` close the case (reason required, ≥ 5 chars).
`ESCALATED` moves the case to ESCALATED (also with a reason). Closing resolves the case's unresolved
alerts with the same outcome, records an audit entry and, if an existing investigation is linked and
still under review, records the equivalent existing decision on it
(CONFIRMED_SUSPICIOUS → confirm, CLEARED/FALSE_POSITIVE → reject, ESCALATED → escalate) so the two
records never contradict each other. The existing investigation endpoints keep their old vocabulary.

### 5.4 Authorisation (policy)

| Action | analyst | admin |
|---|---|---|
| view alerts, cases, workbench | yes | yes |
| claim an unassigned alert/case, work items assigned to themselves | yes | yes |
| act on items assigned to **another** user | 403 | yes |
| assign to someone else, reassign | 403 | yes |
| run monitoring, ingest transactions | 403 | yes |
| edit/delete audit, notes, decisions | nobody (append-only) | nobody |

## 6. Risk scoring (MODIFIED)

The score stays `Σ weight × strength` with group caps (EXISTING). v2 adds a **category breakdown**
(sum of capped points per detector category, plus a `cap_adjustment` row when the global cap of 100
applies) and per-detector results to the existing `/api/risk/{type}/{id}` response. **Customer
profile and document signals are not part of the score**: the engine has no such inputs, and the
score will not be presented as if it did. Documents are retrieved as context evidence only.

## 7. Temporal analysis

Existing windows: 60-minute burst, daily velocity, 24-hour pass-through, 30-day window against a 90-day
baseline. v2 adds `STRUCTURING` (`window_hours`, default 24) and a read-only
`GET /api/customers/{id}/activity-windows` returning counts/volume for 1 h, 24 h, 7 d and 30 d ending
at a given time, versus the baseline daily rate. All thresholds are in `default_config.yaml`
(`docs/RISK_ENGINE.md` documents each).

## 8. Graph investigation

EXISTING (NetworkX default, Neo4j optional): neighbourhood, shared devices, shortest path,
connected accounts, fund tracing, cycles, clusters. NEW (NetworkX; **not implemented for Neo4j**, the
API answers 501 there): direct and second-degree counterparties of a customer, customers sharing a
beneficiary, accounts receiving funds from multiple flagged accounts. Neo4j remains optional and is
not required.

## 9. Evidence

Evidence items carry a class so a reader can tell where a fact came from:

| Class | Source |
|---|---|
| `DATABASE_FACT` | rows read from the store (transactions, profile, history) |
| `RULE_RESULT` | detector output and risk metrics (including the ML score) |
| `GRAPH_RESULT` | graph queries |
| `DOCUMENT_EVIDENCE` | retrieved passages with document, section and chunk id |
| `LLM_GENERATED_SUMMARY` | narrative text written by an LLM; **never** treated as a source of fact |

The existing `evidence.source_type` maps to the first four. Narrative claims already carry
`source: deterministic | llm`; the UI labels `llm` claims "AI-generated". Analysts can attach
transactions or retrieved passages to a case (`case_evidence`); the API verifies the referenced
object exists and does not accept free-text "facts".

## 10. Audit

New audited actions: `alert_created`, `alert_updated`, `alert_assigned`, `alert_status_changed`,
`case_created`, `case_assigned`, `case_status_changed`, `investigation_note_added`, `evidence_added`,
`decision_recorded`, `case_closed`, `risk_score_generated`, `monitoring_run`,
`transactions_ingested`. The new history tables (`monitoring_alert_events`, `case_events`,
`case_notes`, `case_evidence`) get the same append-only triggers as `audit_log` (migration 0003).
This is **database-enforced append-only, not cryptographic immutability**; there is no hash chain or
signature.

## 11. Evaluation and performance plan

- **Evaluation (NEW):** an independent benchmark dataset from `MonitoringBenchmarkBank` (separate
  seed, new scenarios including structuring and fan-out, evasive positives the detectors are not
  designed to catch, and legitimate look-alikes). Customer-level labels, alert-level metrics:
  precision, recall, F1, FPR, PR-AUC (ranked by risk score), alerts per 1,000 transactions,
  detection latency and throughput. Held-out seed. Existing metrics are not re-labelled as
  performance. See `docs/EVALUATION.md` for caveats.
- **Performance (NEW):** `python -m app.monitoring.benchmark` measures ingestion, detection, alert
  generation and query latency at 10k, 100k (and, if the machine allows, 1M) transactions. Only
  measured numbers are published (`docs/PERFORMANCE.md`).

## 12. API changes (summary)

NEW: `/api/monitoring/{alerts,alerts/{id},alerts/{id}/assign|transition|resolve|case,run,runs,transactions,kpis,detectors}`,
`/api/cases{,/{id},/{id}/workbench|notes|assign|transition|decision|investigate|evidence}`,
`/api/network/*`, `/api/customers/{id}/activity-windows`, `/ready`.
MODIFIED (backward compatible, additive fields): `/api/risk/{type}/{id}`, `/api/dashboard`,
`/health/ready`. EXISTING and unchanged: everything else.

## 13. Frontend changes

NEW pages: Alert Queue (filter, search, sort, assign, transition, resolve, create case), Alert detail,
Cases list, Case workbench (profile, risk, rules, transactions, activity, counterparties, graph,
related alerts, documents, evidence, notes, timeline, decision), Monitoring (admin: run, ingest, runs).
MODIFIED: Dashboard (monitoring KPIs from the database). EXISTING pages unchanged.

## 14. Testing strategy

Unit: detectors (trigger / no trigger / threshold boundary / empty / malformed), dedup, state
machines. Service: alert generation, idempotency, case flow. API: RBAC and error paths. Security:
audit and history tables are append-only; unauthorised access is rejected. PostgreSQL integration
tests mirror the in-memory repository tests. Frontend: unit tests for pure helpers (if the Vite
setup supports them) plus type-check and build.

## 15. Operational strategy

`/health` (liveness), `/ready` and `/health/ready` (database, graph, vector store). Structured JSON
logs (existing) never include credentials. Configuration only through environment variables.
Migrations are reproducible (`alembic upgrade head` 0001 → 0003). Indexes cover the alert queue
filters. Deployment target: GitHub → CI → Render (API + PostgreSQL + static frontend) using synthetic
data only; Neo4j/Qdrant/LLM stay optional. See `docs/DEPLOYMENT.md`.

## 16. Known conflicts and deliberate deviations

| Decision made during implementation | Reason |
|---|---|
| Alert tiers (section 3.1) instead of "any triggered detector" | Measured alert noise on a smoke dataset |
| Workflow operations run in one PostgreSQL transaction; audit rows are written after commit | Avoid half-applied operations and audit entries for rolled-back actions |
| Monitoring runs are admin/CLI initiated, not continuous | Keeps the prototype simple; a scheduler is future work |
| `TRUNCATE` is not blocked on the four history tables | `loader --truncate` must be able to reset a demo database; documented in SECURITY.md |

| Request | Decision |
|---|---|
| Reuse the `alerts` table | Separate `monitoring_alerts`; reason in 2.1 |
| Detection `confidence` | Omitted: not statistically meaningful |
| "Document/evidence" and "customer profile" score components | Not part of the score; shown as context |
| Account balance movement detector | Not implemented: no balance history in the data |
| Investigation decision names | Case decisions use the new vocabulary; existing investigation vocabulary unchanged and mapped |
| Replay evaluation | The graph projection is built from the full dataset, so graph-based detectors in a day-by-day replay can see future edges. The evaluation is therefore a single batch at the dataset end; no day-by-day replay evaluation was built |
| Neo4j | Remains optional; new network queries are NetworkX-only |
