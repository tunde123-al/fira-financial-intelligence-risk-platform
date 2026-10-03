---
title: Investigation Report Standard
doc_type: internal_procedure
document_id: STD-REP-006
version: "2.0"
effective_date: 2026-04-01
jurisdiction: internal
owner: Head of Financial Crime Compliance
source: Lagoon Bank Plc (fictional institution) — synthetic document for FIRA development
---

# Investigation Report Standard

> SYNTHETIC DOCUMENT for FIRA development. Lagoon Bank Plc is fictional.

## 1. Required Sections

Every investigation report must contain:

1. **Executive summary** — what was reviewed and what was found, in neutral language.
2. **Risk indicators** — each indicator with the observed value, the baseline or threshold, severity and a
   reference to the supporting evidence.
3. **Transaction analysis** — the specific transactions relied upon.
4. **Relationships** — connected customers, accounts, devices and identifiers that are relevant.
5. **Policy references** — the policy or procedure sections that apply.
6. **Risk assessment** — how the overall risk rating was derived.
7. **Uncertainty** — missing data, conflicting evidence, weak signals and assumptions.
8. **Recommended actions** — further investigative steps. Recommendations are advisory.
9. **Human decision** — the investigator's decision and rationale.

## 2. Evidence and Citations

Every factual statement must be traceable to a record (transaction, account, customer, device), a calculated
metric, a graph query result, a document passage or a model output. Where evidence is not available the report
must say "insufficient evidence" rather than infer.

## 3. Language

Reports must not state or imply that a customer has committed a crime. Use terms such as "consistent with",
"indicator of" and "requires further review". Victims of fraud must not be described as perpetrators.

## 4. Automated Drafts

Drafts produced by automated systems must identify which statements are computed facts, which are model outputs
and which are interpretations, and must be reviewed by an investigator before the case is concluded.

## 5. Decision Options

- **Confirm** — the analyst agrees the activity warrants escalation as suspicious.
- **Reject** — the activity has a legitimate explanation; the alert is a false positive.
- **Escalate** — refer to a senior analyst or the MLRO.
- **Request more evidence** — the case cannot be concluded on the available evidence.
