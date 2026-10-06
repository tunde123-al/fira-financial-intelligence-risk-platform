# AI Investigation Copilot

A grounded question-answering assistant for investigators. It answers questions about **one customer** from FIRA's own records and
never from the model's imagination. Synthetic data only.

## What it is and is not

* It **is** a retrieval-and-explain pipeline: rules choose what to fetch, FIRA's engines compute the signals, and an optional LLM
  writes one paragraph of interpretation that is checked against the retrieved evidence.
* It is **not** a general chatbot, has no database or shell access, cannot change any record, and works with no LLM configured.

## Request flow

```
question (3-500 chars, control characters stripped)
   -> intent rules (keywords): why_high_risk | top_transactions | network | aml_history | evidence_review | summary
   -> retrieval from FIRA: customer + accounts, transactions in the window, risk engine assessment, legacy alerts,
      monitoring alerts, previous investigations (and the given investigation), graph cluster + shared devices,
      money-mule indicators
   -> EvidencePackage: every entity the answer may mention (an allow-list of ids)
   -> structured response built from the package
   -> [optional] LLM: one JSON paragraph {"interpretation": "..."} from the package
   -> validation: unknown entity ids (hyphenated, spaced, underscored, lowercase), unhedged accusations, certainty about wrongdoing, action directives, links/markup, instruction echoes, contradiction of the risk level, wrong score => draft discarded. The model also never receives database prose (only numbers, codes, ids, sanitised tokens)
   -> audit events; response
```

The question is treated as data. It selects intents by keyword only; text in it (including "ignore previous instructions" or an id
of another customer) cannot widen retrieval. The LLM is told the package is data, not instructions.

## Response (`POST /api/copilot/ask`)

```json
{
  "status": "ok | insufficient_evidence",
  "customer_id": "CUST-10226", "question": "...", "intents": ["why_high_risk"],
  "summary": "Risk score 84.7 (critical) ... Largest contributions: RAPID_PASS_THROUGH +22.7 ...",
  "risk": {"risk_score": 84.7, "risk_level": "CRITICAL", "flagged": true, "config_version": "default-2"},
  "observed_facts":  [{"statement": "...", "source": "database",    "refs": [{"type": "transaction", "id": "TXN-..."}]}],
  "derived_signals": [{"statement": "...", "source": "risk_engine|graph|analytics", "refs": []}],
  "risk_factors":    [{"factor": "RAPID_PASS_THROUGH", "contribution": 22.73, "capped": false, "description": "...", "label": "DERIVED SIGNAL"}],
  "evidence":        [{"type": "transaction", "id": "TXN-...", "why": "supports FAN_IN"}],
  "interpretation":  {"label": "AI INTERPRETATION (not evidence)", "text": "...", "generated_by": "deterministic template | provider:model"},
  "recommendations": [{"statement": "...", "label": "SYSTEM RECOMMENDATION (a suggested review step; not a legal conclusion)"}],
  "limitations": ["..."], "grounding": {"allowed_entity_ids": 39, "ai_used": false}
}
```

| Label | Meaning | Produced by |
|---|---|---|
| OBSERVED FACT | read directly from the database | stores |
| DERIVED SIGNAL | calculated by FIRA analytics (risk engine, graph, mule indicators) | deterministic code |
| AI INTERPRETATION | prose explaining the evidence; **not evidence** | LLM, or a deterministic template when no LLM |
| RECOMMENDATION | a suggested review step, labelled as a system recommendation, not a legal conclusion | playbook rules |

## Failure behaviour

| Situation | Result |
|---|---|
| no provider configured (`LLM_PROVIDER=none`, the default) | deterministic template interpretation; `generated_by: "deterministic template"` |
| provider error, timeout, HTTP failure | deterministic answer + a limitation saying the AI draft was unavailable |
| not valid JSON / empty | discarded + limitation |
| draft names an entity not in the evidence | discarded + limitation stating only the **count** (invented ids are not echoed) |
| accusatory wording ("is a criminal", "committed fraud") | discarded + limitation |
| too little data (no signals, no factors, no more than the profile) | `status: insufficient_evidence`, no recommendations, a limitation saying so |
| unknown customer / bad input | 404 / 422 |

## Provider setup

Reuses `app/llm/provider.py` (Anthropic, OpenAI, Ollama via stdlib HTTP; `NullProvider` by default). Environment variables:
`LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_TIMEOUT_S`. Keys are never committed (`.env` is git-ignored). **No live
LLM provider has been exercised against the copilot in this repository**: tests use a scripted fake provider (valid reply, invented ids,
invalid JSON, empty, HTTP error, timeout, accusatory text).

## Audit and privacy

Two events per request: `ai_investigation_requested` (customer, question **length**, investigation id; the question text is not stored)
and `ai_response_generated` (status, whether AI text was used, intents, counts). Responses go through the same PII masking as the other
endpoints. Copilot output is **never persisted as evidence**; investigation evidence keeps its own classes and an LLM summary is always
labelled `LLM_GENERATED_SUMMARY` there.

## Tests

`backend/tests/unit/test_copilot.py` (15 tests): every cited id exists in the database; factors equal the engine's contributions; the
four sections are separate and labelled; top-transaction ranking cites real transactions; a valid AI paragraph is accepted and labelled;
invented ids, invalid JSON, empty text, provider errors, timeouts and accusatory text all fall back safely; insufficient evidence;
prompt-injection text cannot pull another customer; auth and validation; audit records contain no question text; and the chain
customer -> risk calculation -> evidence retrieval -> investigation -> copilot context.

## Limitations

* Intent detection is keyword-based: an unusual phrasing falls back to the general summary.
* One customer per question; no cross-customer queries.
* The validator is rule-based: ids, the risk level, the quoted score and accusation/directive/markup patterns. A careful paraphrase or an invented non-id fact can pass (pinned by a test). It does not verify every claim in free text. That is why the
  structured sections (not the paragraph) carry the facts.
* Network answers use the NetworkX backend; with Neo4j some graph findings are reported as unavailable.
