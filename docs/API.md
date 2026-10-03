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

## MCP

`python -m app.mcp.server` (stdio) with `FIRA_MCP_API_KEY` set. It exposes `customer_lookup`,
`transaction_analysis`, `graph_search`, `risk_analysis`, `document_search` and
`investigation_history`. Each one delegates to the tool registry under the key's role and is
audited. Example client configuration:

```json
{"mcpServers": {"fira": {"command": "python", "args": ["-m", "app.mcp.server"], "cwd": "backend",
  "env": {"FIRA_MCP_API_KEY": "<key>", "MCP_API_KEYS": "<key>:analyst", "DATABASE_URL": "..."}}}}
```
