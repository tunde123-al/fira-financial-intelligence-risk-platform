# Acceptance run (2026-10-06T15:49:57Z)

16 of 16 steps passed against `http://localhost:8765`.

| # | step | result | seconds |
|---|---|---|---|
| 1 | API is ready, schema current, baseline ingestion batch recorded from the dataset manifest | PASS | 8.68 |
| 2 | Data-quality failure: a dirty batch is quarantined with reasons, accounting balances, nothing is lost silently | PASS | 9.53 |
| 3 | Monitoring run over the scenario customers raises alerts with explainable triage | PASS | 12.6 |
| 4 | Why a score: the triage explanation lists every factor with points, maximum and reason | PASS | 2.06 |
| 5 | Money-mule scenario: indicators with evidence, flow graph, fan-in search | PASS | 6.84 |
| 6 | AI Investigation Copilot: grounded structured answer for the mule customer, with entity references and audit | PASS | 12.15 |
| 7 | False-positive scenario: a benign look-alike alerts, is reviewed and cleared; feedback is recorded | PASS | 8.81 |
| 8 | Investigator queue: assign, My Work ordering, invalid transitions refused | PASS | 8.32 |
| 9 | Case: create, investigate customer, transactions and graph, attach evidence, note, change priority | PASS | 26.96 |
| 10 | Decision closes the case, resolves its alerts and appears in the timeline in real-time order | PASS | 9.59 |
| 11 | Audit trail contains the journey and cannot be edited through the API | PASS | 10.22 |
| 12 | Operations: alert-quality metrics use valid denominators only; business metrics are exposed | PASS | 6.22 |
| 13 | Configuration governance: a detector switch and a threshold change are logged with who, why, old and new | PASS | 14.97 |
| 14 | Security behaviour over HTTP: 401, 403, oversize 413, login lockout 429, logout revokes the token | PASS | 33.83 |
| 15 | Backup (encrypted), verify, and restore test into a scratch database | PASS | 31.39 |
| 16 | Restore into a named database and verify the restored application | PASS | 35.8 |

## Evidence

### 1. API is ready, schema current, baseline ingestion batch recorded from the dataset manifest

```json
{
 "schema_revision": "0004",
 "db_latency_ms": 2.33,
 "baseline_expected": 40296,
 "baseline_received": 40296,
 "coverage": 1.0
}
```

### 2. Data-quality failure: a dirty batch is quarantined with reasons, accounting balances, nothing is lost silently

```json
{
 "batch": "BAT-EEDAA9D5B664",
 "accounting": "19 = 13 + 6 + 0",
 "reason_codes": [
  "DUPLICATE_ID",
  "INVALID_TIMESTAMP",
  "MALFORMED_ROW",
  "MISSING_FIELD",
  "NON_POSITIVE_AMOUNT",
  "UNKNOWN_ACCOUNT"
 ],
 "late": 1,
 "missing": 6,
 "coverage_overall": 0.999851,
 "quality_score": 99.98
}
```

### 3. Monitoring run over the scenario customers raises alerts with explainable triage

```json
{
 "customers_screened": 20,
 "alerts_created": 28,
 "duration_ms": 8349,
 "alerts_by_priority": {
  "CRITICAL": 4,
  "HIGH": 18,
  "MEDIUM": 6
 },
 "top_alert": {
  "alert_id": "MAL-1545D93B7BCE",
  "detector_id": "FAN_OUT",
  "triage_score": 67.8,
  "triage_priority": "CRITICAL"
 }
}
```

### 4. Why a score: the triage explanation lists every factor with points, maximum and reason

```json
{
 "alert": "MAL-1545D93B7BCE",
 "score": 67.8,
 "priority": "CRITICAL",
 "largest_factors": [
  [
   "Customer risk score (0-100 engine score)",
   19.95
  ],
  [
   "Detector severity",
   13.0
  ],
  [
   "Independent signal categories firing for the customer",
   10.0
  ]
 ]
}
```

### 5. Money-mule scenario: indicators with evidence, flow graph, fan-in search

```json
{
 "customer": "CUST-11490",
 "band": "HIGH",
 "score": 66.8,
 "indicators_fired": [
  "fan_in",
  "fan_out",
  "flagged_network",
  "low_retention",
  "rapid_movement"
 ],
 "flow_nodes": 60,
 "flow_edges": 61,
 "in_usd": 11929.64,
 "out_usd": 10200.9
}
```

### 6. AI Investigation Copilot: grounded structured answer for the mule customer, with entity references and audit

```json
{
 "customer": "CUST-11490",
 "risk": {
  "risk_score": 79.8,
  "risk_level": "CRITICAL",
  "flagged": true,
  "config_version": "default-2"
 },
 "facts": 3,
 "signals": 15,
 "evidence_items": 45,
 "ai_used": false,
 "dataset_quality_score": 100.0
}
```

### 7. False-positive scenario: a benign look-alike alerts, is reviewed and cleared; feedback is recorded

```json
{
 "customer": "CUST-11325",
 "alert": "MAL-22A79E884804",
 "mule_band": "LOW",
 "recorded": {
  "decision": "FALSE_POSITIVE",
  "investigator": "U-analyst",
  "decided_at": "2026-10-06T15:46:57.772341Z"
 }
}
```

### 8. Investigator queue: assign, My Work ordering, invalid transitions refused

```json
{
 "my_open": 1,
 "high_priority": 1,
 "overdue": 0
}
```

### 9. Case: create, investigate customer, transactions and graph, attach evidence, note, change priority

```json
{
 "case": "FC-2026-000001",
 "counterparties": 99,
 "customer_transactions": 49,
 "evidence_transaction": "TXN-00033400"
}
```

### 10. Decision closes the case, resolves its alerts and appears in the timeline in real-time order

```json
{
 "timeline_events": 11,
 "sources": [
  "alert",
  "case",
  "note"
 ]
}
```

### 11. Audit trail contains the journey and cannot be edited through the API

```json
{
 "audit_rows": 96,
 "actions_seen": [
  "ai_investigation_requested",
  "ai_response_generated",
  "alert_assigned",
  "alert_created",
  "alert_status_changed",
  "case_closed",
  "case_created",
  "case_priority_changed",
  "case_status_changed",
  "decision_recorded",
  "evidence_added",
  "graph_query",
  "investigation_note_added",
  "login",
  "monitoring_run",
  "mule_assessment",
  "risk_score_generated",
  "seed_batch_registered",
  "tool_call",
  "transactions_ingested"
 ]
}
```

### 12. Operations: alert-quality metrics use valid denominators only; business metrics are exposed

```json
{
 "decided": 2,
 "confirmation_rate": 0.5,
 "false_discovery_rate": 0.5,
 "low_sample_flag": true,
 "open_alerts": 26
}
```

### 13. Configuration governance: a detector switch and a threshold change are logged with who, why, old and new

```json
{
 "changes_recorded": 2,
 "example": {
  "path": "triage.thresholds.high",
  "old_value": 50.0,
  "new_value": 52.0,
  "changed_by": "U-admin",
  "source": "api"
 }
}
```

### 14. Security behaviour over HTTP: 401, 403, oversize 413, login lockout 429, logout revokes the token

```json
{
 "login_sequence": [
  401,
  401,
  401,
  401,
  401,
  429,
  429
 ],
 "oversize_request": "connection closed by server"
}
```

### 15. Backup (encrypted), verify, and restore test into a scratch database

```json
{
 "size_bytes": 1999093,
 "dump_seconds": 1.1,
 "restore_seconds": 23.36,
 "row_counts_match": true,
 "tables_checked": 13
}
```

### 16. Restore into a named database and verify the restored application

```json
{
 "restored_database": "fira_accept_restored_313e2a",
 "alerts": 28,
 "cases": 1,
 "batches": 2,
 "audit_append_only": true
}
```
