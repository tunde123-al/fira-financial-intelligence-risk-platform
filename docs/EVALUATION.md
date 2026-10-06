# Evaluation

This page has three parts. **Part 1** evaluates the transaction-monitoring pipeline added in v2 (automatic alert
generation). **Part 1b** evaluates the production-oriented additions: money-mule indicators, alert triage and the
operational alert-quality metrics. **Part 2** is the original risk-detection, retrieval and agent benchmark, which was
produced with risk configuration `default-1` before the monitoring upgrade and is kept unchanged.

> **Everything here is measured on synthetic data whose scenarios were written by the same authors as the
> detectors. None of it is evidence of real-world AML or fraud detection performance.**

---

# Part 1. Transaction-monitoring evaluation (v2)

## What is evaluated

The question: *given a labelled synthetic bank, how well do the alerts that a monitoring run creates line up with the
customers the generator planted as suspicious?* A monitoring run screens **every** customer at the end of the dataset
(30-day lookback, 90-day baseline) with the shipped configuration. A customer counts as **predicted positive** if the
run raised at least one alert for them.

## Dataset generation

`python -m app.synthetic.monitoring_benchmark` generates an independent benchmark bank:

- the standard FIRA synthetic bank (same base behaviour, the eight original suspicious scenarios and three
  look-alike traps) generated with **its own seed**, so it is not the dataset the detectors were developed on;
- plus new scenarios the original benchmark did not contain:

| Scenario | Label | Designed so that |
|---|---|---|
| structuring_detectable | suspicious | 3 to 6 transactions of USD 8,200 to 9,950 inside one day |
| structuring_evasive | suspicious | the same amounts spaced more than 24 hours apart, **so the structuring detector is not designed to catch it** |
| fan_out_detectable | suspicious | 9 to 16 distinct beneficiaries within about 36 hours |
| fan_out_evasive | suspicious | only 5 to 7 beneficiaries, below the detector threshold |
| legit_recurring_large_payments | benign | a business pays USD 8.3k to 9.8k invoices every month, so the pattern is in its baseline |
| legit_payroll | benign | monthly payroll to 10 to 14 employees, in the baseline |
| legit_new_business_invoices | benign | a first-ever batch of large invoices: **ambiguous on purpose, a false positive is expected** |
| legit_first_payroll | benign | a first-ever payroll run: **ambiguous on purpose** |

Labels come from `scenario_labels.json` written by the generator. Customers with **no label** carry no injected scenario
and are treated as benign. That is conservative: incidental risky-looking behaviour in the random base data counts as a
false positive. The default dataset used by the demo and by the original benchmark is **unchanged** by this module
(a test regenerates it and compares).

Two seeds are reported: a **dev** seed (2025, 4,000 customers) and a **holdout** seed (7, 3,000 customers).

## Detector methodology

Detection is the risk engine's deterministic detectors (`docs/RISK_ENGINE.md`), now including STRUCTURING and FAN_OUT
(risk configuration `default-2`). Alerting follows detector tiers: *standalone* typology detectors alert on their own,
*supporting* baseline-deviation detectors alert only when the customer's combined score reaches the investigation
threshold (40), and *context* signals never alert. Thresholds and weights are the shipped engineering defaults. **No
threshold was tuned on the reported datasets** (`thresholds_tuned_on_this_data: false` in the result files).

### A disclosure about how the tiers came about

The first implementation alerted on *any* triggered detector. A smoke run on a 1,500-customer bank (seed 2024) showed
precision 0.17 and a false-positive rate of 0.17, almost all from three detectors that fire on ordinary behaviour
(new country, new device, unusual amount). The tier design was introduced as a result. So the tier decision **was
influenced by results on a dataset of the same generator** (seed 2024). The numbers below use different seeds (2025
and 7) that were not used for that decision, but the principle that the design reacted to benchmark output applies, and
the ablation below shows the effect of the decision on the dev seed.

## Metrics

At customer level: precision, recall, F1, false-positive rate (FPR), **PR-AUC** (customers ranked by the risk score of
their highest alert, no alert = 0; the no-skill baseline is the prevalence, about 0.033) and ROC-AUC. Volume:
**alerts per 1,000 transactions** (transactions in the 30-day window) and per 1,000 customers. Speed: detection
latency per customer (assessment plus detector results, sampled), customers per second and window transactions per
second. "Detection latency" is **processing time**; the benchmark runs in batch mode, so dataset-time lag between a
transaction and its alert is not measured.

## Results

| | Dev (seed 2025) | Holdout (seed 7) |
|---|---|---|
| Customers / transactions | 4,000 / 102,556 | 3,000 / 77,469 |
| Suspicious customers (prevalence) | 133 (3.3%) | 100 (3.3%) |
| **Precision** | 0.858 | 0.878 |
| **Recall** | 0.865 | 0.860 |
| **F1** | 0.861 | 0.869 |
| **False-positive rate** | 0.0049 | 0.0041 |
| **PR-AUC** (no-skill 0.033) | 0.859 | 0.855 |
| ROC-AUC | 0.932 | 0.930 |
| TP / FP / FN / TN | 115 / 19 / 18 / 3,848 | 86 / 12 / 14 / 2,888 |
| Alerts raised (on customers) | 365 (134) | 270 (98) |
| **Alerts per 1,000 transactions** | 20.1 | 19.7 |
| Alerts per 1,000 customers | 91 | 90 |
| Monitoring run, all customers | 427.7 s (9.4 customers/s) | 310.4 s (9.7 customers/s) |
| Detection latency per customer mean / p95 / p99 | 109.5 / 156 / 201 ms | 103.3 / 127 / 171 ms |

With roughly 100 to 130 positives the proportions carry an uncertainty of about plus or minus 6 points (normal
approximation, 95%); there is one run per seed and no confidence intervals were computed beyond that estimate.

### What the numbers are made of

- **All 18 (dev) and 14 (holdout) missed positives are the evasive scenarios**, which fail by construction
  (structuring_evasive 0 of 10, fan_out_evasive 0 of 8 on dev). Every scenario the detectors were designed for was
  alerted: recall on the non-evasive positives is 115 of 115 on dev. That is a statement about the generator and the
  detectors being written together, not a promise about real behaviour. The honest summary of recall is the
  evasive scenarios: the detectors miss behaviour that deliberately sits outside their thresholds.
- **False positives:** on dev, 10 of the 19 are the two deliberately ambiguous benign scenarios (a first payroll run and
  a first invoice batch look exactly like fan-out and structuring), 8 are unlabelled customers and 1 is a "normal"
  customer. The recurring-payment, payroll, high-frequency, high-value and legitimate-travel look-alikes raised **no**
  alert: the baseline logic and the supporting-detector gate suppressed them.
- Per-detector alert precision on dev ranges from 0.63 (STRUCTURING, hit by the first-invoice-batch look-alike) to 1.0
  for most others (full tables in the result files).

### Ablation: what the alert tiers do (dev seed)

| Policy | Precision | Recall | FPR | Alerts | Alerts per 1,000 txns |
|---|---|---|---|---|---|
| Tiers (shipped) | 0.858 | 0.865 | 0.0049 | 365 | 20.1 |
| Every triggered detector alerts (`supporting_min_customer_score: 0`) | 0.161 | 0.985 | 0.176 | 1,133 | 62.5 |

Recall rises to 0.985 because the baseline-deviation detectors catch some evasive cases, at the price of 682 false
positives (a 17.6% false-positive rate), which no alert queue could absorb. The shipped policy is a precision-oriented
choice on this synthetic data; a real institution would pick its own operating point.

The operating-point tables in the result files show the same trade-off along the alert risk score (for example,
requiring a score of at least 40 gives precision 0.98 and recall 0.69 on dev).

## Limitations

1. **Synthetic data and circularity.** Scenario authors and detector authors are the same people. Detectors fire on what
   the generator plants. Real fraud is adversarial and varied in ways this generator is not.
2. **Constructed ambiguity.** The evasive positives and ambiguous negatives were chosen by us. Different choices would
   give different recall and false-positive numbers. They are included to avoid a flattering 100%, not to model reality.
3. **Label quality.** Unlabelled customers are assumed benign. Customer-level labels hide that a customer may be
   suspicious in a way the generator did not label.
4. **Customer-level, batch-mode.** Metrics are per customer at one point in time. Transaction-level labels, alert
   timeliness and analyst workload are not evaluated. Graph signals use a projection of the full dataset, so a day-by-day
   replay would leak future edges; no replay evaluation was built.
5. **Small samples.** About 100 to 130 positives per seed; no confidence intervals beyond the rough estimate above.
6. **Not evaluated:** the ML anomaly signal (not trained for these runs), the LLM (not used), real-world drift,
   adversarial adaptation, and the investigator workflow's quality.
7. **Tier design influenced by a smoke run** (see the disclosure above).

## Reproducibility

```bash
cd backend
python -m app.synthetic.monitoring_benchmark --out ../data/benchmark --customers 4000 --seed 2025
python -m app.evaluation.monitoring_eval --dataset ../data/benchmark --out ../evaluation/results/monitoring_dev_seed2025
# ablation
python -m app.evaluation.monitoring_eval --dataset ../data/benchmark --supporting-min-score 0 --out ../evaluation/results/ablation
```

Committed results: [`evaluation/results/monitoring_dev_seed2025.md`](../evaluation/results/monitoring_dev_seed2025.md),
[`monitoring_holdout_seed7.md`](../evaluation/results/monitoring_holdout_seed7.md) and
[`monitoring_dev_seed2025_ablation_no_gate.md`](../evaluation/results/monitoring_dev_seed2025_ablation_no_gate.md)
(the `.json` files are git-ignored). Counts and rates are deterministic for a given seed, configuration and library
versions; timings depend on the machine (here: Windows 10, Intel Core i5-3470, Python 3.13). The metric functions and
the benchmark generator are covered by `tests/unit/test_monitoring_eval.py`.

---

# Part 1b. Production-oriented additions (money-mule indicators, triage, alert quality)

> Same caveat as everywhere on this page: synthetic data, scenarios written by the people who wrote the detectors, one run per
> seed, no confidence intervals. These are behaviours of this software on a generated bank, **not** real-world performance.

## What changed in the benchmark

The independent benchmark generator gained a money-mule family (`backend/app/synthetic/monitoring_benchmark.py`). Scenario code
writes transactions and labels; **nothing in detection, triage or the mule view reads the labels** (the mule module is checked for
label-related identifiers by a unit test, and the evaluator is the only reader of `scenario_labels.json`).

| Scenario | Label | Designed so that |
|---|---|---|
| mule_to_collector | suspicious (mule) | 6 to 10 senders pay in, 85-95% is forwarded within hours to a shared *collector* account |
| mule_collector | suspicious (mule network) | the individual that receives pooled funds from several mules; no behaviour of its own that a single-account detector sees |
| mule_slow_evasive | suspicious (mule) | the same fan-in, but the money leaves 30 to 60 hours later, **outside the 24 h rapid-movement window**; evasive by construction |
| fan_in_collection | suspicious (mule) | 12 to 18 senders in two days, bulk cash-out 36 to 60 hours later |
| rapid_single_pass_through | suspicious | one large inbound transfer leaves again within three hours, no fan-in |
| legit_marketplace_seller | benign | many small customer payments, funds mostly retained |
| legit_family_collection | benign | relatives pool money for a gift and it is forwarded within a day: **ambiguous on purpose** |
| legit_rent_split | benign | three housemates pay in, the full rent leaves within hours: **ambiguous on purpose** |

The scenarios already in the base bank (`mule_account`, `circular_transfer`, `device_sharing_ring`, `account_takeover`,
`dormant_reactivation`, `transaction_burst`, the structuring/fan-out pairs and the payroll/invoice look-alikes) are unchanged.
Customer counts per scenario scale with the bank size. Because the new scenarios change the random stream and add customers, the
v3 numbers are **not comparable** with Part 1. To regenerate the v2 benchmark exactly use
`python -m app.synthetic.monitoring_benchmark ... --without-mule-family` (seed 7 with 3,000 customers reproduces the 77,469
transactions of Part 1).

## How the design was tuned, and what was held out

1. A first implementation of triage and the mule view was run on the development benchmark (seed **2025**, 4,000 customers).
   Result: triage was worse than the customer risk score it was built on (alert-level ROC-AUC 0.65 vs 0.87), its confirmed rate
   was not monotonic in priority and nothing reached CRITICAL; in the mule view a "connected to flagged customers" indicator fired
   for 258 of 400 ordinary customers because it followed shared merchants and devices, and an unrelated-spending effect made
   "low retention" fire for ordinary salaried customers. (`evaluation/results/monitoring_v3_dev_seed2025_first_design.md`,
   produced with an earlier version of the code.)
2. **One revision** followed, on that same development data: triage weights (customer score 15 → 25, detector tier 10 → 5,
   severity 15 → 13, amount 13 → 10), triage thresholds 75/55/35 → 65/50/30 (so every band is reachable; the highest score
   was 70), mule inbound restricted to account-to-account transfers, "flagged network" restricted to transfer counterparties
   within two hops, "low retention" restricted to outflow during and shortly after the collection period.
3. The **holdout** (seed **7**, 4,000 customers) was generated and run *after* that revision with nothing changed. Treat the
   development figures as tuned and the holdout figures as the fair ones. The detector and risk configuration were not touched.

## Customer-level detection (any alert = positive; v3 benchmark)

| | Dev (seed 2025, tuned on) | **Holdout (seed 7)** |
|---|---|---|
| Customers / transactions / suspicious | 4,000 / 103,073 / 173 | 4,000 / 105,566 / 175 |
| Precision / recall / F1 | 0.800 / 0.809 / 0.805 | **0.838 / 0.829 / 0.833** |
| False-positive rate | 0.0091 | **0.0073** |
| PR-AUC (no-skill 0.043) / ROC-AUC | 0.787 / 0.903 | **0.818 / 0.913** |
| TP / FP / FN / TN | 140 / 35 / 33 / 3,792 | 145 / 28 / 30 / 3,797 |
| Alerts (alerts per 1,000 window transactions) | 475 (24.9) | 463 (24.3) |

Where recall is lost on the holdout (alert rate by scenario, from `monitoring_v3_holdout_seed7.md`):

| scenario | customers | alerted | note |
|---|---|---|---|
| structuring_evasive | 10 | 0 | by construction |
| fan_out_evasive | 8 | 2 | by construction |
| mule_collector | 4 | 0 | the collector has no single-account behaviour the detectors look for |
| rapid_single_pass_through | 8 | 1 | **a real detector gap**: the RAPID_PASS_THROUGH detector requires at least three inbound transfers (`min_inbound_count: 3`); it caught 1 of 8 |
| mule_slow_evasive | 8 | 5 | fan-in is detected; the forwarding delay is not |
| every other suspicious scenario | | all | |

Benign look-alikes that alerted (false positives by design): legit_family_collection 5 of 6, legit_first_payroll 4 of 4,
legit_new_business_invoices 4 of 6, legit_marketplace_seller 5 of 8, legit_rent_split 3 of 8. The recurring-payment, payroll,
high-frequency, high-value and travel look-alikes produced no alert.

## Money-mule indicators

The mule view is **not an alert generator**: it is an evidence view for an investigator (indicators, a 0 to 100 score, bands
HIGH ≥ 60, MEDIUM ≥ 35, LOW ≥ 15), described in [TRANSACTION_MONITORING_ARCHITECTURE.md](TRANSACTION_MONITORING_ARCHITECTURE.md).
Evaluated on every labelled customer plus 400 randomly sampled unlabelled customers (assumed benign): 559 labelled + 400
sampled on the holdout, 44 of them mule typologies (`mule_account`, `mule_to_collector`, `mule_collector`, `mule_slow_evasive`,
`fan_in_collection`). True negatives are defined here, so FPR is valid, but only over this sampled universe.

| rule | precision | recall | F1 | FPR | TP / FP / FN / TN |
|---|---|---|---|---|---|
| mule band MEDIUM or HIGH (holdout) | 0.543 | 0.568 | 0.556 | 0.023 | 25 / 21 / 19 / 894 |
| mule band HIGH (holdout) | 1.000 | 0.182 | 0.308 | 0.000 | 8 / 0 / 36 / 915 |
| baseline: FAN_IN or RAPID_PASS_THROUGH alert (holdout) | 0.627 | 0.841 | 0.718 | 0.024 | 37 / 22 / 7 / 893 |
| mule band MEDIUM or HIGH (dev) | 0.500 | 0.545 | 0.522 | 0.026 | 24 / 24 / 20 / 889 |

ROC-AUC of the mule score as a ranking: **0.971** holdout, 0.968 dev.

What this says, plainly:

* The score *ranks* mule typologies far above ordinary customers (AUC 0.97), but **the band cut-offs are not a good classifier**:
  MEDIUM-or-above is right about half the time and finds about half the mules, and the existing FAN_IN / RAPID_PASS_THROUGH alerts
  have a higher F1 (0.72) on the same universe. The mule view adds *explanation and flow context*, not detection accuracy.
* HIGH is precise on this data (8 of 8 are mules) but rare: it finds 18% of them.
* Band counts on the holdout: `mule_account` 8 HIGH + 4 MEDIUM of 12; `mule_to_collector` 11 MEDIUM + 1 LOW of 12 (the forwarded
  amount lands in a collector, so "low retention" scores but the fan-out indicator does not); `mule_slow_evasive` 6 LOW + 2 MEDIUM
  of 8 (as designed: the rapid-movement indicator did not fire); `fan_in_collection` 8 of 8 LOW; `mule_collector` 3 NONE + 1 LOW of 4.
* False MEDIUM-or-above: `circular_transfer` 9 of 15 (circular flows genuinely look like layering), `legit_marketplace_seller`
  5 of 8, `legit_rent_split` 3 of 8, `legit_family_collection` 1 of 6, one `normal` customer of 300 and one sampled customer of 400.
* The indicator "connected to customers with open alerts" still fires for 178 of the 400 sampled ordinary customers (it is
  capped at 10 points): in a dense synthetic bank most people transact within two hops of someone who has an open alert.

## Triage quality (oracle dispositions from the labels)

The disposition of an alert is the generator's label for its customer (suspicious → confirmed, benign → false positive): a
*simulated* investigator that always agrees with the label. This tests whether the ranking puts label-confirmed alerts first,
nothing more. 92% of holdout alerts are label-confirmed, so there is little room to discriminate.

| priority | holdout alerts | confirmed | confirmed rate | dev alerts | confirmed rate |
|---|---|---|---|---|---|
| CRITICAL | 18 | 18 | 1.000 | 19 | 1.000 |
| HIGH | 274 | 269 | 0.982 | 272 | 0.941 |
| MEDIUM | 169 | 138 | 0.817 | 182 | 0.797 |
| LOW | 2 | 0 | 0.000 | 2 | 0.000 |

* The confirmed rate is monotonic in priority on both seeds: higher-priority alerts do have a higher oracle-confirmed rate.
  (It is the first-design failure above that this revised design repaired.) LOW has two alerts on each seed: no conclusion.
* **The triage score is not a better ranking than the customer risk score:** alert-level ROC-AUC 0.828 vs 0.917 (holdout),
  0.762 vs 0.868 (dev). Top-k precision (k = 10, 25, 50, 100) is 1.0 for both, against 1.0 / 0.92 / 0.92 / 0.90 for a random order.
  Triage's value is that its points can be read, challenged and adjusted, not that it predicts better.

## Alert-quality metrics

Operational metrics use only denominators that exist (confirmation rate, false-discovery rate, closure rate); FPR and recall
appear **only** in the labelled evaluations above. See [ALERT_QUALITY.md](ALERT_QUALITY.md).

## Data-quality gate

Evaluated by tests, not by a benchmark: `tests/unit/test_data_quality.py` injects each fault type (22 reason codes across
malformed, duplicate, invalid and referential groups, late arrival, missing expected rows, storage failure) and asserts the
specific code, the batch accounting identity `received = processed + rejected + failed`, and that rejected rows are kept. The
benchmark run measured the gate at `docs/PERFORMANCE.md` (rows per second with 10% invalid rows).

## Limitations specific to Part 1b

1. Oracle dispositions are labels, not people. Real investigators disagree with labels, with each other and with themselves.
2. The mule evaluation universe is partly sampled; the sample is random (seed 7 for sampling) but small (400).
3. The evaluator's timings in the result files were taken while other work ran on the same machine; the clean timings are in
   [PERFORMANCE.md](PERFORMANCE.md).
4. One run per seed, no confidence intervals. With 44 mule customers a single customer moves recall by 2.3 points.
5. The development figures are tuned (see above); only the holdout is a fair read, and it is also synthetic.

## Reproduce

```bash
cd backend
python -m app.synthetic.monitoring_benchmark --out ../data/benchmark_holdout --customers 4000 --seed 7
python -m app.evaluation.monitoring_eval --dataset ../data/benchmark_holdout --out ../evaluation/results/monitoring_v3_holdout_seed7
python -m app.synthetic.monitoring_benchmark --out ../data/benchmark_dev --customers 4000 --seed 2025
python -m app.evaluation.monitoring_eval --dataset ../data/benchmark_dev --out ../evaluation/results/monitoring_v3_dev_seed2025
```

Counts and rates are deterministic for a given seed, configuration and library versions; each run takes about 12 minutes on the
development machine (the monitoring run over 4,000 customers dominates).

---

# Part 2. Original risk benchmark (risk configuration `default-1`)

Produced before the monitoring upgrade. Risk configuration is now `default-2` (adds STRUCTURING and FAN_OUT); the
numbers below were **not** regenerated and describe `default-1`.


```bash
make evaluate-local                     # in-process: risk + retrieval + agent
python -m app.evaluation.runner risk    # or retrieval | agent | all  [--per-scenario N] [--agent-sample N]
```

Each run is stored in `evaluation_runs` (shown on the UI's Evaluation page) and written to
`evaluation/results/*.json` with a Markdown summary. Admins can also trigger a run from the UI or
with `POST /api/evaluation/run`.

## Benchmarks and metrics

| Benchmark | Data | Metrics |
|---|---|---|
| Risk detection | every labelled subject (or N per scenario): 8 suspicious typologies, 3 false-positive traps, 300 normal customers | precision, recall, F1, FPR, FNR, ROC-AUC; per-scenario flag rate and **expected-signal recall**; per-signal precision |
| Retrieval | 25 hand-labelled queries (`evaluation/retrieval_qrels.json`; a chunk is relevant if document and section match) | P@1/3/5, R@1/3/5, MRR — for keyword, semantic and hybrid |
| Agent | full agent runs on sampled labelled subjects | tool-selection recall and precision against the plan, task completion (status plus all report sections), evidence coverage (expected signals present in the report), unsupported-claim rate |
| RAG | the same runs | context relevance (retrieved passages relevant per qrels to the signal they were retrieved for), groundedness (1 − unsupported rate), citation correctness (cited evidence shares content with the claim) |
| System | the same runs | latency p50/p95, token usage, cost, tool-call count, tool-failure rate, timeout rate |

## Results

**Dev dataset** (seed 42, 10,000 customers, all 631 labelled subjects; ML model enabled):

| precision | recall | F1 | FPR | FNR | AUC |
|---|---|---|---|---|---|
| 0.991 | 0.890 | 0.938 | 0.005 | 0.110 | 0.998 |

**Holdout dataset** (seed 7, 5,000 customers, 467 labelled subjects, never used for tuning):

| precision | recall | F1 | FPR | FNR | AUC |
|---|---|---|---|---|---|
| 0.991 | 0.912 | 0.950 | 0.003 | 0.088 | 0.999 |

**Population alert rate** (1,000 random dev customers): 2.5% flagged overall, 0.10% of customers
without an injected suspicious scenario.

Per scenario (dev): circular transfers, impossible travel and mule accounts are caught 100% of the
time. Account takeover 93%, dormant reactivation 84%, device rings 83%, high-risk merchant 80% and
card-testing bursts 70% are the weakest; misses are scenarios where only one moderate signal fired.
False-positive traps: high-frequency legitimate businesses 0%, legitimate one-off high-value
payments 0%, legitimate travel 8% (new country plus large foreign card spend).

Agent (42 runs, deterministic narrative): tool-selection recall 1.00, precision 0.99, task
completion 1.00, evidence coverage 0.98, unsupported-claim rate 0.00, context relevance 0.77,
citation correctness 0.98, latency p50 0.5 s and p95 0.8 s (in-process stores), 0 tool failures
and 0 timeouts.

Retrieval: see RAG_ARCHITECTURE.md (hybrid R@5 0.95, MRR 0.95).

## How to read these numbers — caveats

1. **Synthetic, co-designed data.** The detectors and the generator were written by the same team,
   and the injected typologies are clean. High scores show that the pipeline is wired correctly
   end to end and that the scoring design separates the scenarios it was designed for. They say
   **nothing** about performance on real customers, where typologies are noisier and labels are
   incomplete.
2. **Tuning leakage.** Three weights were adjusted after the first dev-set run. The holdout uses a
   different seed but the same generator, so it guards against seed-specific overfitting only.
3. **ML precision 1.0** on labelled subjects reflects the evaluation sample (37% suspicious), not
   population precision.
4. **Agent metrics with the deterministic narrative** measure the evidence pipeline and validator.
   With an LLM configured, the same benchmark measures LLM groundedness; the validator removes
   unsupported claims, and their rate is reported.
5. The **retrieval qrels** were written by the corpus author.

### Evaluating on real data

Load real or anonymised data with `app/db/loader.py` (see DATA_MODEL.md) and labelled outcomes into
`scenario_labels` (entity, scenario/typology, `is_suspicious`, `expected_signals` if known). The
runner then reports the same metrics. Use a time-based split (tune on older periods, test on newer
ones), and track alert volume per signal alongside precision and recall.

## Regression gate for configuration changes

`app/evaluation/improvement.py::regress` compares a candidate configuration with the active one on
the labelled set. A candidate is accepted only if recall drops by at most 0.02, FPR rises by at
most 0.01, and F1 does not get worse. `tests/e2e` checks both directions: a mild change passes,
and disabling the strongest detectors is rejected.

## Tests

| Suite | Count | Covers |
|---|---|---|
| `tests/unit` | 63 | metrics, geo, statistics, scoring and group caps, every detector against ground truth, graph algorithms, parsers (MD/PDF/OCR/DOCX), chunk provenance, BM25/LSA/vector store, retrieval quality floor, validator, auth/JWT/masking/rate limits/roles, tool registry error envelopes, generator integrity and determinism |
| `tests/agent` | 11 | tool selection per subject type, lookback parsing, missing subject, unknown entity, unsupported merchant, malformed tool output, conflicting history, hallucinated LLM claims removed, retry then fallback, LLM errors, episode content, LangGraph parity (CI) |
| `tests/e2e` | 2 | the full acceptance workflow; controlled improvement with four-eyes approval |
| `tests/integration` | 8 | PostgreSQL load and reads equal the in-process store; risk engine identical on both stores; write round-trips; Neo4j vs NetworkX parity; Qdrant; MCP; HTTP acceptance flow |
