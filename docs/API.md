# API

Interactive OpenAPI documentation is served at `http://localhost:8000/docs` (Swagger UI) and
`/openapi.json`. Every `/api/*` endpoint except login requires `Authorization: Bearer <token>`.
Responses include `X-Request-ID`. Errors return `{"detail": …}`, and 422 responses also include
field errors.

```bash
TOKEN=$(curl -s localhost:8000/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"analyst","password":"..."}' | jq -r .access_token)
curl -s localhost:8000/api/agent/investigate -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"request":"Investigate customer CUST-10291 and identify unusual activity during the last 30 days."}'
```

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/health` | — | liveness |
| GET | `/health/ready` | — | dependency readiness (503 when degraded) |
| GET | `/metrics` | admin | Prometheus metrics |
| POST | `/api/auth/login` | — | issue a JWT |
| GET | `/api/auth/me` | any | current principal |
| GET | `/api/search?q=&entity_type=` | analyst | search customers, accounts, transactions, merchants, devices |
| GET | `/api/customers/{id}` | analyst | profile, masked identifiers, accounts, alerts, investigations (`?unmask=true` is admin-only and audited) |
| GET | `/api/customers/{id}/transactions?lookback_days=&limit=` | analyst | transactions |
| GET | `/api/customers/{id}/statistics?lookback_days=` | analyst | window vs baseline statistics |
| GET | `/api/accounts/{id}` | analyst | account and owner |
| GET | `/api/transactions/{id}` | analyst | transaction |
| GET | `/api/merchants/{id}` | analyst | merchant |
| GET | `/api/devices/{id}` | analyst | device and its users |
| GET | `/api/alerts?status=&entity_id=&limit=` | analyst | alerts |
| GET | `/api/risk/{customer\|account}/{id}?lookback_days=` | analyst | explainable risk assessment |
| GET | `/api/dashboard` | analyst | counts, alerts, investigations, risk distribution, system |
| GET | `/api/tools` | analyst | tools available to the caller, with JSON schemas |
| GET | `/api/investigations?status=&subject_id=` | analyst | list |
| POST | `/api/investigations` | analyst | create (`subject_type`, `subject_id`, `request_text`) |
| GET | `/api/investigations/{id}` | analyst | investigation, report and decisions |
| GET | `/api/investigations/{id}/evidence` | analyst | evidence items E1…En |
| GET | `/api/investigations/{id}/graph?depth=` | analyst | relationship subgraph |
| GET | `/api/investigations/{id}/episodes` | analyst | agent episodes (plan, tool calls, observations, summary, feedback) |
| GET | `/api/investigations/{id}/report.md` | analyst | Markdown export |
| POST | `/api/investigations/{id}/decision` | analyst | `{decision: confirm\|reject\|escalate\|request_more_evidence, rationale, failure_categories[], report_quality}` |
| POST | `/api/agent/investigate` | analyst | `{request, subject_type?, subject_id?, lookback_days?, investigation_id?}` runs the agent |
| GET | `/api/graph/stats` | analyst | graph size |
| GET | `/api/graph/{kind}/{id}?depth=&limit=` | analyst | neighbourhood |
| GET | `/api/graph/cluster/{customer_id}` | analyst | cluster analytics |
| GET | `/api/graph/trace/{account_id}?direction=&max_hops=` | analyst | fund tracing |
| GET | `/api/graph/path?source=&target=` | analyst | shortest transfer path |
| GET | `/api/documents` | analyst | document registry |
| GET | `/api/documents/search?q=&k=&mode=&doc_type=` | analyst | hybrid, semantic or keyword search with provenance |
| POST | `/api/documents/ingest` | admin | re-ingest the documents directory |
| POST | `/api/documents/upload` (multipart) | admin | upload and ingest |
| GET | `/api/evaluation/runs` | analyst | stored benchmark results |
| POST | `/api/evaluation/run` | admin | run benchmarks |
| GET | `/api/evaluation/failures` | analyst | failure classification from analyst feedback |
| POST | `/api/config/proposals` | analyst | propose and regress a risk-config change |
| GET | `/api/config/versions` | analyst | active config (YAML) and version history |
| POST | `/api/config/versions/{id}/approve` | admin (not the proposer) | activate a validated version |
| GET | `/api/audit?action=&user_id=&limit=` | analyst (own) / admin (all) | audit trail |

## Transaction monitoring, alerts, cases and network (v2)

Errors: 401 no/invalid token, 403 not allowed (role or assignment), 404 unknown id, 409 illegal state
transition or duplicate, 422 invalid input.

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/ready` | — | readiness (alias of `/health/ready`; includes `monitoring_store`) |
| GET | `/api/monitoring/detectors` | analyst | detector catalogue with weights, parameters and alert tier (`standalone`, `supporting`, `context`) |
| GET | `/api/monitoring/kpis` | analyst | alert and case KPIs computed from stored data |
| GET | `/api/monitoring/runs?limit=` | analyst | recent monitoring runs |
| POST | `/api/monitoring/run` | admin | `{window_end?, lookback_days?, active_days?, customer_ids?}` run monitoring (synchronous) |
| POST | `/api/monitoring/transactions` | admin | `{transactions[1..5000], run_monitoring}` validate, store and monitor a batch; rejected rows are reported with reasons |
| GET | `/api/monitoring/alerts` | analyst | alert queue. Filters: `status` (comma list), `severity`, `detector`, `min_risk`, `max_risk`, `customer_id`, `assigned_to` (`me`, `unassigned`, user id), `date_from`, `date_to`, `q`, `case_id`; `sort` (`triggered_at`, `risk_score`, `severity_rank`, `status`, `customer_id`, `detector_id`, `updated_at`), `order`, `limit` (<= 200), `offset` |
| GET | `/api/monitoring/alerts/{id}` | analyst | alert, supporting transactions, history, case |
| POST | `/api/monitoring/alerts/{id}/assign` | analyst (self) / admin (anyone) | `{assignee: "U-…"}` |
| POST | `/api/monitoring/alerts/{id}/transition` | assignee / admin | `{status: TRIAGED\|INVESTIGATING\|ESCALATED\|RESOLVED, reason?, resolution?}` |
| POST | `/api/monitoring/alerts/{id}/resolve` | assignee / admin | `{resolution: CLEARED\|FALSE_POSITIVE\|CONFIRMED_SUSPICIOUS, reason}` |
| POST | `/api/monitoring/alerts/{id}/case` | analyst | create a case for the alert, or join the customer's open case |
| GET | `/api/cases` | analyst | list (`status`, `priority`, `customer_id`, `assigned_to`, `q`, paging) |
| POST | `/api/cases` | analyst | `{customer_id, alert_ids[], priority?, title?}` |
| GET | `/api/cases/{id}` | analyst | case, alerts, notes |
| GET | `/api/cases/{id}/workbench` | analyst | customer profile, risk and components, triggered rules, related and recent transactions, activity windows, counterparties (degree 1 and 2, shared beneficiaries), network graph and cluster, related alerts (FIRA and legacy), retrieved documents, classified evidence, linked investigation and narrative (AI-written claims labelled), notes, timeline, decision |
| POST | `/api/cases/{id}/assign` | analyst (self) / admin | `{assignee}` |
| POST | `/api/cases/{id}/transition` | assignee / admin | `{status: INVESTIGATING\|PENDING_REVIEW}` |
| POST | `/api/cases/{id}/decision` | assignee / admin | `{decision: CLEARED\|FALSE_POSITIVE\|CONFIRMED_SUSPICIOUS\|ESCALATED, reason}`; closing decisions resolve the case's alerts and, if an investigation is linked and open, record the equivalent existing decision on it |
| POST | `/api/cases/{id}/notes` | assignee / admin | `{body}` append-only |
| GET/POST | `/api/cases/{id}/evidence` | analyst / assignee | `{kind: transaction\|document\|alert, ref, note?}`; only existing objects are accepted |
| POST | `/api/cases/{id}/investigate` | assignee / admin | run the evidence-grounded agent for the case's customer and link it |
| GET | `/api/risk/{customer\|account}/{id}` | analyst | **extended**: adds `detector_results`, `explanation`, `score_components` (category breakdown), `supporting_transactions`, `not_in_score`. Existing fields unchanged |
| GET | `/api/customers/{id}/activity-windows?windows=1h,24h,7d,30d` | analyst | counts, value and counterparties per window versus the baseline |
| GET | `/api/network/customers/{id}/counterparties?degree=1\|2&days=` | analyst | direct and second-degree transfer counterparties |
| GET | `/api/network/customers/{id}/shared-beneficiaries` | analyst | beneficiaries also paid by other customers |
| GET | `/api/network/customers/{id}/cycles` | analyst | circular paths, with whether each is time-ordered |
| GET | `/api/network/common-recipients?accounts=ACC-1,ACC-2` | analyst | accounts receiving from several of the given accounts |
| GET | `/api/network/flagged-recipients/{id}` | analyst | common recipients of the flagged customers in this customer's cluster |

The network endpoints use the NetworkX backend; with the optional Neo4j backend they answer 501.
`/api/dashboard` gains a `monitoring` object (KPIs). `/api/alerts` remains the **legacy seeded** alert list.

## Production-oriented additions (v3)

Authenticated unless noted. Errors as above, plus 413 (request body over `MAX_BODY_BYTES`) and 429 (rate limit or login lockout).
Every operation carries exactly one OpenAPI tag: `health`, `metrics`, `auth`, `transactions`, `monitoring`, `alerts`, `triage`,
`risk`, `cases`, `investigations`, `evidence`, `graph`, `data-quality`, `config`, `evaluation`, `audit`.

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/api/monitoring/transactions` | admin | **changed:** `{transactions[1..5000] (raw objects), run_monitoring, source?, expected?: {count \| ids \| sequence}, require_transaction_id?}`. Bad rows are quarantined with a reason code, never a request failure; the response adds `batch` (the accounting). A failure after validation returns 500 and records the batch as `FAILED` |
| GET | `/api/data-quality/summary` | analyst | totals (expected, received, processed, rejected, duplicates, malformed, late, failed, missing), coverage (null without a declared expectation), processing success, quality score, rejection breakdown, recent batches, definitions |
| GET | `/api/data-quality/batches`, `/batches/{id}` | analyst | batch ledger |
| GET | `/api/data-quality/rejected` | analyst | quarantined rows; filters `batch_id`, `reason_code`, `reason_group`; paging |
| GET | `/api/monitoring/alerts` | analyst | **extended:** `priority` filter (comma list), `sort=triage_score`; each item carries `triage_score`, `triage_priority`, `triage_factors`, `triage_computed_at` |
| GET | `/api/monitoring/triage/{alert_id}` | analyst | the factor breakdown, thresholds and method statement |
| POST | `/api/monitoring/triage/recompute` | admin | `{alert_id?}` re-score unresolved alerts; resolved alerts keep their score (409) |
| GET | `/api/monitoring/quality?date_from=` | analyst | confirmation / false-discovery / closure rates, by priority, detector and severity, with `not_computed` explaining why FPR and recall are absent |
| GET | `/api/monitoring/feedback?limit=` | analyst | recorded outcomes: alert, decision, reason, investigator, time |
| GET | `/api/monitoring/my-work?recent_days=` | analyst | the caller's open alerts by triage score, high priority, overdue, recently escalated/confirmed/cleared, open cases |
| POST | `/api/cases/{id}/priority` | assignee / admin | `{priority, reason}` (reason of 5+ characters; 409 if unchanged or closed) |
| GET | `/api/mule/customers/{id}` | analyst | money-mule indicators (8, with points, evidence and reasons), score, band, disclaimer, context notes |
| GET | `/api/mule/customers/{id}/flow?depth=1..3&days=` | analyst | nodes (account, owner, depth, roles, flagged, degrees) and edges (source, destination, USD, count, first/last time, direction, depth); `truncated` when a size bound is hit |
| GET | `/api/mule/patterns?min_degree=&days=` | analyst | accounts with many distinct senders / receivers |
| GET | `/api/mule/suspects?min_band=` | analyst | customers with fund-flow alerts ranked by indicator score |
| GET | `/api/config/monitoring` | analyst | current monitoring configuration, fingerprint, runtime-overridable paths |
| GET | `/api/config/changes?limit=` | analyst | configuration change log |
| POST | `/api/config/monitoring` | admin | `{path, value, reason}` runtime override of one allow-listed setting; validated as a whole; recorded; not persisted to the YAML file |
| POST | `/api/config/detectors/{detector_id}` | admin | `{enabled, reason}` switch alerting for one detector |
| POST | `/api/auth/logout` | analyst | revoke the presented token until it expires |
| GET | `/health/ready` | — | **extended:** `database_latency_ms`, `schema_revision`, `schema_expected`, `schema_current` (false makes the response 503) |
| GET | `/metrics` | admin JWT or `X-Metrics-Token` | adds business gauges (alerts by priority, cases, last run, ingestion rows, data-quality score, DB ping) |

Dashboard KPI change: `monitoring.false_positive_rate` is **replaced** by `false_discovery_rate` and `confirmation_rate`
(shares of decided alerts). An FPR needs true negatives, which the alert workflow does not have.

## MCP

`python -m app.mcp.server` (stdio) with `FIRA_MCP_API_KEY` set. It exposes `customer_lookup`,
`transaction_analysis`, `graph_search`, `risk_analysis`, `document_search` and
`investigation_history`. Each one delegates to the tool registry under the key's role and is
audited. Example client configuration:

```json
{"mcpServers": {"fira": {"command": "python", "args": ["-m", "app.mcp.server"], "cwd": "backend",
  "env": {"FIRA_MCP_API_KEY": "<key>", "MCP_API_KEYS": "<key>:analyst", "DATABASE_URL": "..."}}}}
```
