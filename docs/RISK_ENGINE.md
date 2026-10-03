# Risk engine

`backend/app/risk/engine.py` is fully deterministic and makes no LLM calls. For an entity and a
window (default: the last 30 days, with a 90-day baseline before it) it:

1. loads the customer's transactions for `[baseline_start, window_end]` (all accounts, or one
   account for account reviews)
2. orients them as outbound, inbound or internal relative to the customer's own accounts
3. computes **identical** statistics for the baseline and the window (`analytics/stats.py::period_stats`)
4. runs each enabled detector. A detector emits a `RiskSignal` only when its observed value crosses
   a threshold derived from configuration and/or the baseline. It **always** records its raw
   metric, which the ML model and the benchmarks use
5. scores the fired signals

## Signals

Every signal carries `signal_id, signal_type, entity, observed_value, baseline_value, threshold,
unit, severity, strength, confidence, description, evidence (transaction/device/customer/... refs),
timestamp, window`.

| Signal | Observed | Threshold (defaults) |
|---|---|---|
| TRANSACTION_BURST | max outbound count in any rolling 60 min | max(6, 2 × baseline max) |
| VELOCITY_SPIKE | max outbound count in one day | max(6, baseline mean + 4σ) |
| AMOUNT_DEVIATION | largest completed outbound (USD) | max(200, median + 6·MAD·1.4826, 2 × baseline p99); needs ≥5 baseline txns, otherwise *not evaluated* |
| PEER_AMOUNT_DEVIATION | same | 3 × p99 of the customer's segment over the baseline period |
| RAPID_PASS_THROUGH | share of inbound transfer value forwarded within 24 h (FIFO matching) | ≥ 0.7, with ≥ USD 500 and ≥ 3 credits |
| FAN_IN | distinct inbound counterparties | max(8, 3 × baseline rate) |
| GEO_NEW_COUNTRY | transactions in countries absent from baseline and KYC country | ≥ 1; configured high-risk jurisdictions force high severity (list is empty by default) |
| IMPOSSIBLE_TRAVEL | implied speed between consecutive card-present transactions | > 900 km/h and ≥ 500 km; digital channels excluded (IP geolocation is unreliable) |
| NEW_DEVICE | share of outbound digital value from devices not seen in baseline | ≥ 0.3, or ≥ 3 transactions; lower confidence when the customer had no baseline devices |
| DEVICE_SHARING | distinct customers on a device the subject used (90 days) | ≥ 3 (two-person households are normal) |
| SHARED_IDENTIFIER | customers sharing a phone, email or address hash | ≥ 3, or any shared phone/email |
| CIRCULAR_FLOW | value of a **temporally ordered** cycle back to the subject (graph candidates verified on transactions: each hop within 72 h of the previous, amount within ±30%) | ≥ USD 500 |
| DORMANT_REACTIVATION | days of inactivity before window activity (looks back beyond the baseline) | ≥ 90 days and ≥ USD 1,000 moved |
| HIGH_RISK_MERCHANT | USD to high-risk merchants in the window | ≥ 3 payments and max(300, 3 × baseline rate) |
| BEHAVIOURAL_SHIFT | Jensen–Shannon divergence of type × channel mix | ≥ 0.35 with ≥ 8 window transactions |
| NETWORK_EXPOSURE | direct counterparties with open alerts or confirmed cases | ≥ 1 |
| HISTORICAL_ALERTS | alerts before the window | ≥ 1 |
| ML_ANOMALY | Isolation-Forest percentile of the metric vector | ≥ 99th percentile of the training population |

## Score

```
strength  = 0.5 at the threshold, rising linearly to 1.0 at threshold × (1 + ramp)
raw       = weight × strength
group cap = correlated signals share a cap (velocity, amount, flow, geo, device):
            contributions are taken in descending order until the cap is reached
score     = min(100, Σ contributions)
flagged   = score ≥ investigation_threshold (40)
band      = low <25 ≤ medium <50 ≤ high <75 ≤ critical
```

Every contribution is visible in the API, the report and the UI: signal, weight, strength, raw
points, capped points and capped flag. For example:

```
Risk Score: 84.7 (critical)
RAPID_PASS_THROUGH   +22.73  (24 × 0.947)
AMOUNT_DEVIATION     +16.00  (16 × 1.0)
GEO_NEW_COUNTRY      +12.00
VELOCITY_SPIKE        +9.33
FAN_IN                +9.27  (group-capped)
BEHAVIOURAL_SHIFT     +8.00
NETWORK_EXPOSURE      +6.00
HISTORICAL_ALERTS     +1.33
```

## Configuration — engineering defaults, not regulatory truth

`backend/app/risk/default_config.yaml` holds every weight, threshold, group cap and band.

**These values were set by the FIRA engineering team so that the synthetic benchmark behaves
sensibly.** They are not regulatory thresholds, they have not been calibrated on any real
portfolio, and an institution must re-derive and approve them through model-risk governance before
use. Three weights (IMPOSSIBLE_TRAVEL, DEVICE_SHARING, HIGH_RISK_MERCHANT) were raised after a first
evaluation on the dev seed. To limit overfitting, results are also reported on a holdout dataset
generated with a different seed (see EVALUATION.md). The holdout still comes from the same
generator, which is the main limitation.

Configuration changes go through `app/evaluation/improvement.py`:

1. **propose** — derived from analyst feedback (down-weight signals dominated by false positives,
   up-weight signals present in missed cases, in bounded steps), or supplied as explicit YAML
2. **regress** — the current and candidate configurations are run on the labelled benchmark. The
   candidate is accepted only if recall drops by at most 0.02, FPR rises by at most 0.01, and F1
   does not get worse
3. **approve** — an admin who is **not the proposer** activates it (four-eyes). The previous
   version is retired and the engine reloads

The system never modifies its own configuration.

## ML anomaly model

`python -m app.risk.ml train --sample 2000` fits an Isolation Forest on the detectors' raw metric
ratios plus volume features for a random, **unlabelled** sample of customers. Scores are calibrated
to population percentiles. The signal is typed `ml`, carries confidence 0.6 and its top deviating
features, and has a default weight of 8. A gradient-boosting model was rejected: the only labels
available are synthetic, and a supervised model would learn the generator rather than behaviour.
