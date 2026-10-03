# FIRA evaluation EVAL-337D3EE31B52

- created: 2026-10-02T06:54:55.960448+00:00
- dataset: ../data/seeds
- risk config: default-1

## Risk detection

Threshold 40.0, n=631 labelled subjects, 115.7 ms/assessment.

| precision | recall | F1 | FPR | FNR | AUC |
|---|---|---|---|---|---|
| 0.991 | 0.8902 | 0.9379 | 0.0052 | 0.1098 | 0.9981 |

| scenario | suspicious | n | flag rate | mean score | expected-signal recall |
|---|---|---|---|---|---|
| account_takeover | True | 30 | 0.9333 | 54.65 | 0.9778 |
| circular_transfer | True | 34 | 1.0 | 67.6 | 1.0 |
| device_sharing_ring | True | 42 | 0.8333 | 51.27 | 1.0 |
| dormant_reactivation | True | 25 | 0.84 | 44.62 | 1.0 |
| geographic_anomaly | True | 30 | 1.0 | 56.55 | 1.0 |
| high_frequency_legit | False | 30 | 0.0 | 4.69 | None |
| high_risk_merchant | True | 25 | 0.8 | 48.87 | 1.0 |
| legit_high_value | False | 30 | 0.0 | 13.35 | None |
| mule_account | True | 30 | 1.0 | 73.92 | 0.9 |
| normal | False | 300 | 0.0 | 2.06 | None |
| transaction_burst | True | 30 | 0.7 | 47.09 | 1.0 |
| travel_legit | False | 25 | 0.08 | 18.52 | None |

| signal | fired | precision |
|---|---|---|
| AMOUNT_DEVIATION | 109 | 0.6422 |
| BEHAVIOURAL_SHIFT | 108 | 0.9259 |
| CIRCULAR_FLOW | 34 | 1.0 |
| DEVICE_SHARING | 42 | 1.0 |
| DORMANT_REACTIVATION | 25 | 1.0 |
| FAN_IN | 30 | 1.0 |
| GEO_NEW_COUNTRY | 212 | 0.8019 |
| HIGH_RISK_MERCHANT | 32 | 1.0 |
| HISTORICAL_ALERTS | 19 | 0.3158 |
| IMPOSSIBLE_TRAVEL | 33 | 0.9091 |
| ML_ANOMALY | 113 | 1.0 |
| NETWORK_EXPOSURE | 109 | 0.7339 |
| NEW_DEVICE | 146 | 0.8151 |
| PEER_AMOUNT_DEVIATION | 63 | 0.8889 |
| RAPID_PASS_THROUGH | 44 | 0.9545 |
| TRANSACTION_BURST | 60 | 1.0 |
| VELOCITY_SPIKE | 92 | 0.9783 |

## Retrieval

| mode | queries | P@1 | P@3 | R@3 | R@5 | MRR |
|---|---|---|---|---|---|---|
| keyword | 25 | 1.0 | 0.6533 | 0.8867 | 0.9467 | 1.0 |
| semantic | 25 | 0.92 | 0.64 | 0.8467 | 0.9267 | 0.948 |
| hybrid | 25 | 0.92 | 0.6667 | 0.9 | 0.9467 | 0.9533 |

## Agent / RAG / System

Engine: mini-state-graph, n=42 investigations, narrative: {'deterministic': 42}

| metric | value |
|---|---|
| agent.tool_selection_recall | 1.0 |
| agent.tool_selection_precision | 0.9949 |
| agent.task_completion | 1.0 |
| agent.evidence_coverage | 0.9792 |
| agent.unsupported_claim_rate | 0.0 |
| rag.context_relevance | 0.773 |
| rag.groundedness | 1.0 |
| rag.citation_correctness | 0.9783 |
| system.latency_p50_ms | 496.3 |
| system.latency_p95_ms | 763.1 |
| system.tokens_in | 0 |
| system.tokens_out | 0 |
| system.cost_usd | 0.0 |
| system.tool_calls | 724 |
| system.tool_failure_rate | 0.0 |
| system.timeout_rate | 0.0 |
