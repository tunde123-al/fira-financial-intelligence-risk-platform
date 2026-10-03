# Agent architecture

## Explicit state graph

`app/agents/workflow.py` declares the workflow once as a `WorkflowSpec`. `app/agents/runtime.py`
compiles it to **LangGraph** (`StateGraph(AgentState)`) when `langgraph` is installed, which is the
default in Docker and CI. Otherwise it compiles to `MiniStateGraph`, a dependency-free runner with
the same semantics: nodes return partial state updates, routers choose the next edge, and a step
limit applies. CI checks that both engines produce the same node trace and score.

```
understand_request ──(no subject)──────────────────────────┐
  └▶ classify_investigation ──(not found / unsupported)───┤
       └▶ plan_evidence                                   │
            └▶ retrieve_structured ──(no customer)────────┤
                 └▶ run_risk_analytics ──(failed)─────────┤
                      └▶ query_graph                      │
                           └▶ retrieve_documents          │
                                └▶ check_history          │
                                     └▶ evidence_fusion   │
                                          └▶ risk_assessment
                                               └▶ generate_report ◀─┐ retry once with
                                                    └▶ validate_claims ┘ feedback if >34%
                                                         └▶ human_review → END   unsupported
                                                     finalize_incomplete → END ◀┘
```

| Node | Does | LLM? |
|---|---|---|
| understand_request | regex extraction of the subject id (CUST/ACC/TXN/DEV/MER) and lookback ("last 2 weeks") | no |
| classify_investigation | resolves the subject to the analysed customer; creates the investigation | no |
| plan_evidence | fixed tool plan per investigation type (`EXPECTED_TOOLS`) | no |
| retrieve_structured | `get_customer`, `get_transactions`, `get_transaction_statistics` | no |
| run_risk_analytics | `get_risk_signals`, `detect_anomalies`, `get_transaction` for cited transactions | no |
| query_graph | `find_suspicious_cluster`, `find_shared_device`, `trace_funds`, `find_connected_accounts` | no |
| retrieve_documents | `search_documents` with playbook queries | no |
| check_history | episodic memory (`get_previous_investigations`, case-note search) | no |
| evidence_fusion | builds E1…En, then `add_evidence` | no |
| risk_assessment | deterministic fact sections | no |
| generate_report | narrative: LLM (if configured) or rules | **optional** |
| validate_claims | grounding checks, removal, retry routing | no |
| human_review | stores the report, sets `pending_review`, saves the episode | no |

**Why the LLM does not choose tools.** For a compliance workflow the evidence set must be complete
and reproducible. A deterministic plan guarantees that every investigation of a given type collects
the same evidence, which makes tool-selection accuracy measurable (it is 1.0 in the benchmark) and
keeps behaviour auditable. The LLM's job is the part where language helps: summarising and weighing
already-collected evidence.

## Tools

The 22 tools live in `app/tools/definitions.py`. Each is a `ToolSpec` with a Pydantic **input
schema** (ids validated by regex, bounded integers), an **output schema**, a **minimum role**, a
**timeout**, and a **side-effect flag**. `ToolRegistry.invoke` returns a `ToolResult` envelope and
never raises into the agent. Its error types are:

`unknown_tool · permission_denied · invalid_input · timeout · not_found · malformed_output · internal`

Every call is logged (structured JSON with request, user and agent-run ids), counted in metrics and
written to the audit log with a summarised input, latency and result. Internal error details are
logged but never returned. The same registry backs the REST API and the MCP server.

## Evidence and the report

The report has these sections: Executive Summary, Risk Indicators table (signal, observation,
baseline, threshold, severity, points, evidence), Transaction Analysis, Graph Relationships,
Document Evidence, Risk Assessment (score construction), Interpretation, Uncertainty (missing
data, conflicting evidence, weak signals, assumptions), Recommended Investigation Actions
(suggestions only), and Human Decision (confirm / reject / escalate / request more evidence).

Each narrative claim has the form `{text, citations: [E..], kind: metric|fact|ml|interpretation|recommendation|assumption, source: deterministic|llm:<model>|system}`.
This keeps facts, calculated metrics, ML outputs, retrieved evidence, interpretations and
assumptions distinguishable, as the specification requires.

## Hallucination control (`app/agents/validator.py`)

A claim survives only if:

1. it cites ≥ 1 evidence ref and all cited refs exist (inline `[E3]` counts)
2. every number matches a number in the cited evidence (±1.1%, or rounding), every date appears in
   it, and every entity id (CUST-/ACC-/TXN-/DEV-/MER-/INV-) appears in it
3. it contains no accusatory or definitive-guilt language
4. it does not direct a consequential action (freeze, close, block, file a report …) unless it
   explicitly defers to human or authorised approval

The exception is "Insufficient evidence: …" statements, which need no citation. When an LLM draft
has more than 34% unsupported claims, it is regenerated once with the rejected claims as feedback.
Sections that end up empty are filled with validated deterministic text. The unsupported-claim
rate is stored per episode and reported in evaluation.

The LLM receives a compact, **masked** evidence pack (≤ 14k characters, prioritised: score,
profile, statistics, signals, then the rest) and a system prompt that forbids invention and
accusation and requires JSON output. Ground-truth labels never reach it; a test asserts this.

## Memory

- **Episodic** (`app/memory/episodic.py`): previous investigations on the subject and connected
  customers, with human decisions, plus similar case notes (semantic search re-ranked by
  signal-overlap Jaccard). It is shown as evidence. When an earlier case concluded *legitimate*
  with overlapping signals, it is surfaced as **conflicting evidence**. Memory never changes the
  score.
- **Semantic**: the policy corpus and case notes in the retrieval index.
- **Learning** is separate: the controlled improvement loop (RISK_ENGINE.md).

## Episodes

`agent_episodes` stores the request, plan, every tool call (node, tool, ok, error type, latency,
summarised input), node observations, evidence refs, a **generated summary of what was done**
(not chain-of-thought), the final status, score and node trace, evaluation (validation stats, tool
recall, failures, timeouts), model, tokens, latency, and the human feedback added later.

## LLM providers

`LLM_PROVIDER=none|anthropic|openai|ollama` with `LLM_MODEL`, `LLM_API_KEY` and `LLM_BASE_URL`.
Providers use plain HTTPS with retry on 429/5xx. Token usage and the cost (from
`LLM_COST_*_PER_MTOK`) are recorded per run. For data that must not leave the network, use
`ollama`.
