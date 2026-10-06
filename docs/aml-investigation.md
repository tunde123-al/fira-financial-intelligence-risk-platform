# AML investigation workflow

How an investigator uses FIRA, from a flagged customer to a recorded decision. All data is synthetic, and every output is decision
support: FIRA never states that anyone committed a crime, never blocks or files anything, and ends every investigation in
`pending_review` for a person.

## Current implementation: the investigation journey

| Step | Where in FIRA | Backed by |
|---|---|---|
| 1. Find who to look at | Dashboard, **Alert Queue** (sorted by triage score), **My Work**, **Money-mule View** suspects | alerts, triage, mule indicators |
| 2. Open the customer | **Customer** page: profile, accounts, risk score and contribution chart, transactions, alerts, investigations | `GET /api/customers/{id}`, `/api/risk/customer/{id}` |
| 3. Ask why | **AI Investigation Copilot** panel: grounded, cited answers (facts / signals / AI interpretation / recommendation) | `POST /api/copilot/ask` |
| 4. Explore the network | **Graph Explorer**, **Money-mule View** (flow graph with amounts, times, direction, depth), counterparties, shared beneficiaries, cycles | `routes_network.py`, `app/graph/` |
| 5. Run the investigation | **Run investigation** (agent) -> an investigation with signals, evidence, report, claim validation | `POST /api/agent/investigate` |
| 6. Work the case | create a case from alerts, assign, notes, attach existing transactions/documents/alerts as evidence, change priority with a reason | `routes_cases.py` |
| 7. Decide | confirm / reject / escalate / request more evidence, with a mandatory reason | `POST .../decision` |
| 8. Review the trail | alert and case timelines, **Audit Log** | `audit_log`, `*_events` |

### What the investigation summary contains

Customer and accounts; risk score, band and factors with contributions and supporting transactions; transaction activity (volume,
value, unusual transactions, windows 1 h / 24 h / 7 d / 30 d against the baseline); network (connected accounts and customers,
counterparties, shared devices, suspicious clusters, money-mule indicators); AML history (legacy alerts, monitoring alerts,
previous investigations, decisions); audit events; and **recommended actions labelled as suggestions** ("Review required",
"Enhanced review recommended", "Continue monitoring", "Gather additional documentation"). The case workbench assembles these in one view.

### Evidence and its classes

Evidence is typed, and AI text can never be primary evidence:

| Class | Meaning |
|---|---|
| DATABASE_FACT | read from a stored record |
| RULE_RESULT | produced by a deterministic detector |
| GRAPH_RESULT | produced by a graph query |
| DOCUMENT_EVIDENCE | a retrieved passage from an ingested policy document, with provenance |
| LLM_GENERATED_SUMMARY | AI-written text, always labelled, never a source of fact |

Analysts can attach only objects that exist (a transaction of the customer, a retrieved passage, an alert of the case); free-text "facts"
are rejected. The copilot's answers are **not** persisted as evidence.

### Audit trail

Recorded with request id and actor: login, monitoring runs, alert creation/assignment/status changes, risk score generation, case
creation/priority/status/decision/closure, notes, evidence added, investigation creation, agent runs, **AI investigation requested /
AI response generated**, graph queries, mule assessments, configuration changes. Secrets are never logged.

### Network patterns FIRA surfaces (from stored data only)

Many-to-one concentration (FAN_IN), one-to-many distribution (FAN_OUT), circular paths (CIRCULAR_FLOW, verified for time order), shared
devices and identifiers, repeated and shared beneficiaries, rapid movement of funds (RAPID_PASS_THROUGH), connections to customers with open
alerts, onward chains. Each carries counts, amounts and transaction references so it can answer "why is this customer connected to
suspicious activity?" with specific records, not a label.

## Limits

Recommendations are heuristics tuned on synthetic scenarios; the money-mule indicators are not a better classifier than the existing
alerts (EVALUATION.md Part 1b); there is no regulatory reporting workflow and no real-world calibration. Nothing here is legal or
compliance advice.

## Production-scale future

Queue routing and SLA timers, case-level access control, four-eyes on decisions, a regulatory-reporting workflow and integration with
real KYC/sanctions data would be required in a real institution. None is implemented.
