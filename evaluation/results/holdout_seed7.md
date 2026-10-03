# FIRA evaluation EVAL-6E57CF052578

- created: 2026-10-02T06:57:09.828617+00:00
- dataset: holdout (synthetic, generator seed 7, 5,000 customers; generated locally, not committed)
- risk config: default-1

## Risk detection

Threshold 40.0, n=467 labelled subjects, 99.3 ms/assessment.

| precision | recall | F1 | FPR | FNR | AUC |
|---|---|---|---|---|---|
| 0.9913 | 0.912 | 0.95 | 0.0029 | 0.088 | 0.9986 |

| scenario | suspicious | n | flag rate | mean score | expected-signal recall |
|---|---|---|---|---|---|
| account_takeover | True | 15 | 1.0 | 56.19 | 0.9778 |
| circular_transfer | True | 18 | 1.0 | 65.05 | 1.0 |
| device_sharing_ring | True | 23 | 1.0 | 51.97 | 1.0 |
| dormant_reactivation | True | 12 | 0.75 | 43.77 | 1.0 |
| geographic_anomaly | True | 15 | 1.0 | 51.22 | 1.0 |
| high_frequency_legit | False | 15 | 0.0 | 3.14 | None |
| high_risk_merchant | True | 12 | 0.6667 | 50.65 | 1.0 |
| legit_high_value | False | 15 | 0.0 | 10.27 | None |
| mule_account | True | 15 | 0.9333 | 67.57 | 0.9333 |
| normal | False | 300 | 0.0 | 2.31 | None |
| transaction_burst | True | 15 | 0.8 | 45.55 | 1.0 |
| travel_legit | False | 12 | 0.0833 | 17.01 | None |

| signal | fired | precision |
|---|---|---|
| AMOUNT_DEVIATION | 58 | 0.5345 |
| BEHAVIOURAL_SHIFT | 51 | 0.9412 |
| CIRCULAR_FLOW | 18 | 1.0 |
| DEVICE_SHARING | 23 | 1.0 |
| DORMANT_REACTIVATION | 12 | 1.0 |
| FAN_IN | 15 | 1.0 |
| GEO_NEW_COUNTRY | 121 | 0.7686 |
| HIGH_RISK_MERCHANT | 13 | 1.0 |
| HISTORICAL_ALERTS | 10 | 0.1 |
| IMPOSSIBLE_TRAVEL | 16 | 0.9375 |
| ML_ANOMALY | 62 | 0.9839 |
| NETWORK_EXPOSURE | 71 | 0.662 |
| NEW_DEVICE | 81 | 0.7037 |
| PEER_AMOUNT_DEVIATION | 29 | 0.8966 |
| RAPID_PASS_THROUGH | 18 | 1.0 |
| TRANSACTION_BURST | 30 | 1.0 |
| VELOCITY_SPIKE | 45 | 0.9778 |

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
| agent.evidence_coverage | 0.9861 |
| agent.unsupported_claim_rate | 0.0 |
| rag.context_relevance | 0.767 |
| rag.groundedness | 1.0 |
| rag.citation_correctness | 0.9769 |
| system.latency_p50_ms | 389.4 |
| system.latency_p95_ms | 572.2 |
| system.tokens_in | 0 |
| system.tokens_out | 0 |
| system.cost_usd | 0.0 |
| system.tool_calls | 732 |
| system.tool_failure_rate | 0.0 |
| system.timeout_rate | 0.0 |
