# Risk engine: explainable scoring (summary)

The full specification is [RISK_ENGINE.md](RISK_ENGINE.md). This page states the explainability contract and where to see it.

## Contract

* **Deterministic and reproducible.** Same data + same configuration => same score. No model decides the score (an optional
  isolation-forest signal is context-only and untrained by default).
* **Configurable and versioned.** Weights, thresholds and enabled flags are in `backend/app/risk/default_config.yaml`
  (`default-2`); every result carries the configuration version and a fingerprint; changes are recorded in the configuration change log.
* **Auditable.** `risk_score_generated` and alert events are written to the audit trail with the configuration version.
* **Separated from AI.** The score and its factors are computed by code. The AI copilot can only *describe* them.

## What a score looks like

`GET /api/risk/customer/{id}` returns, among other fields (real values from a synthetic mule customer, `CUST-10226`):

```json
{
  "score": 84.7, "band": "critical", "flagged": true, "investigation_threshold": 40,
  "contributors": [
    {"signal_type": "RAPID_PASS_THROUGH", "weight": 24, "strength": 0.947, "points": 22.73, "capped": false},
    {"signal_type": "AMOUNT_DEVIATION",   "weight": 16, "strength": 1.0,   "points": 16.0,  "capped": false}
  ],
  "score_components": [{"category": "transaction_behaviour", "label": "Transaction behaviour", "points": 33.3}],
  "signals": [{"signal_type": "RAPID_PASS_THROUGH", "description": "92% of USD 10,165.35 received across 20 inbound transfer credits left ... within 24 hours (baseline ratio 0%; threshold 70%).", "evidence": [{"kind": "transaction", "id": "TXN-00013769"}]}],
  "supporting_transactions": ["TXN-00013769", "..."],
  "explanation": "Risk score 84.7 (critical), above the investigation threshold of 40. Largest contributions: ...",
  "not_evaluated": [{"signal_type": "ML_ANOMALY", "reason": "ML model not trained/loaded"}]
}
```

| Required element | Field |
|---|---|
| Risk score / level | `score`, `band` (low, medium, high, critical) |
| Risk factors | `contributors` (one per detector that fired) and `signals` |
| Contribution | `points` = weight x strength, with `capped` where a group cap applied; category totals in `score_components` |
| Evidence | each signal's `evidence` references real transactions, accounts, devices; `supporting_transactions` |
| Calculation | `weight`, `strength`, `observed_value`, `baseline_value`, `threshold` on each signal; the `explanation` sentence |
| What was not computed | `not_evaluated`, `data_quality` |

The customer page shows the score and a contribution chart; the copilot (`docs/ai-investigation-copilot.md`) repeats the same factors with citations.
Factors are the engine's detectors (velocity, amount deviation, structuring, rapid pass-through, fan-in/out, geography, device,
network, history and others): each is calculated from fields in the database. FIRA does not maintain a second, hand-written factor list.

## Tests

`tests/unit/test_risk.py` and companions (detector behaviour on injected scenarios), `test_monitoring_detectors.py`
(STRUCTURING, FAN_OUT, score decomposition adds up), `test_core_math.py`, and `tests/unit/test_copilot.py::test_risk_factors_equal_the_engines_contributions`
(the explanation layer cannot drift from the engine).

## Limitations

Weights and thresholds are engineering defaults, uncalibrated on real data; detectors were evaluated on synthetic scenarios written by
the same authors (EVALUATION.md states the circularity). The score ranks customers for review; it is not a probability and not a verdict.
