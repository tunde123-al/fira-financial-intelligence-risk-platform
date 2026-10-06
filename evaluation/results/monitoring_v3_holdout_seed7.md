# FIRA transaction-monitoring evaluation (2026-10-03)

SYNTHETIC data only. These numbers describe how the pipeline behaves on a generated bank whose scenarios were written by the same authors as the detectors. They are not evidence of real-world AML or fraud detection performance.

- dataset: fira-monitoring-benchmark, seed 7, 4,000 customers, 105,566 transactions, 175 suspicious customers (prevalence 4.375%)
- configuration: risk `default-2` (2b6a54079660), monitoring `monitoring-1` (be213f147c11), lookback 30 d, baseline 90 d, window end 2026-09-30
- thresholds tuned on this data: **False**; supporting-detector gate: default (investigation threshold)
- environment: Python 3.13.7, Windows-10-10.0.19045-SP0

## Customer-level detection (a customer is positive if any alert was raised)

| precision | recall | F1 | FPR | PR-AUC | ROC-AUC | TP | FP | FN | TN |
|---|---|---|---|---|---|---|---|---|---|
| 0.8382 | 0.8286 | 0.8333 | 0.0073 | 0.8182 | 0.9131 | 145 | 28 | 30 | 3797 |

PR-AUC ranks customers by their highest alert risk score (no alert = 0). The no-skill PR-AUC equals the prevalence, 0.0437.

### Operating points (descriptive; the shipped configuration alerts at any score)

| min alert risk score | precision | recall | F1 | FPR | TP | FP | FN |
|---|---|---|---|---|---|---|---|
| 0 | 0.8382 | 0.8286 | 0.8333 | 0.0073 | 145 | 28 | 30 |
| 20 | 0.8889 | 0.8229 | 0.8546 | 0.0047 | 144 | 18 | 31 |
| 30 | 0.9241 | 0.7657 | 0.8375 | 0.0029 | 134 | 11 | 41 |
| 40 | 0.9739 | 0.6400 | 0.7724 | 0.0008 | 112 | 3 | 63 |
| 50 | 0.9844 | 0.3600 | 0.5272 | 0.0003 | 63 | 1 | 112 |
| 60 | 1.0000 | 0.2057 | 0.3412 | 0.0000 | 36 | 0 | 139 |

## Per scenario

| scenario | suspicious | customers | alerted | alert rate |
|---|---|---|---|---|
| (unlabelled, assumed benign) | False | 3441 | 5 | 0.002 |
| account_takeover | True | 12 | 12 | 1.000 |
| circular_transfer | True | 15 | 15 | 1.000 |
| device_sharing_ring | True | 16 | 16 | 1.000 |
| dormant_reactivation | True | 10 | 10 | 1.000 |
| fan_in_collection | True | 8 | 8 | 1.000 |
| fan_out_detectable | True | 8 | 8 | 1.000 |
| fan_out_evasive | True | 8 | 2 | 0.250 |
| geographic_anomaly | True | 12 | 12 | 1.000 |
| high_frequency_legit | False | 12 | 0 | 0.000 |
| high_risk_merchant | True | 10 | 10 | 1.000 |
| legit_family_collection | False | 6 | 5 | 0.833 |
| legit_first_payroll | False | 4 | 4 | 1.000 |
| legit_high_value | False | 12 | 0 | 0.000 |
| legit_marketplace_seller | False | 8 | 5 | 0.625 |
| legit_new_business_invoices | False | 6 | 4 | 0.667 |
| legit_payroll | False | 8 | 0 | 0.000 |
| legit_recurring_large_payments | False | 10 | 0 | 0.000 |
| legit_rent_split | False | 8 | 3 | 0.375 |
| mule_account | True | 12 | 12 | 1.000 |
| mule_collector | True | 4 | 0 | 0.000 |
| mule_slow_evasive | True | 8 | 5 | 0.625 |
| mule_to_collector | True | 12 | 12 | 1.000 |
| normal | False | 300 | 2 | 0.007 |
| rapid_single_pass_through | True | 8 | 1 | 0.125 |
| structuring_detectable | True | 10 | 10 | 1.000 |
| structuring_evasive | True | 10 | 0 | 0.000 |
| transaction_burst | True | 12 | 12 | 1.000 |
| travel_legit | False | 10 | 0 | 0.000 |

For suspicious scenarios the alert rate is recall; for benign ones it is the false-positive rate.

### Did the *expected* detector fire?

| scenario | customers | expected detector alerted | rate |
|---|---|---|---|
| account_takeover | 12 | 12 | 1.000 |
| circular_transfer | 15 | 15 | 1.000 |
| device_sharing_ring | 16 | 16 | 1.000 |
| dormant_reactivation | 10 | 10 | 1.000 |
| fan_in_collection | 8 | 8 | 1.000 |
| fan_out_detectable | 8 | 8 | 1.000 |
| geographic_anomaly | 12 | 12 | 1.000 |
| high_risk_merchant | 10 | 10 | 1.000 |
| mule_account | 12 | 12 | 1.000 |
| mule_to_collector | 12 | 12 | 1.000 |
| rapid_single_pass_through | 8 | 1 | 0.125 |
| structuring_detectable | 10 | 10 | 1.000 |
| transaction_burst | 12 | 12 | 1.000 |

## Per detector (alerts raised)

| detector | alerts | on suspicious customers | alert precision |
|---|---|---|---|
| AMOUNT_DEVIATION | 45 | 42 | 0.933 |
| CIRCULAR_FLOW | 15 | 15 | 1.000 |
| DEVICE_SHARING | 16 | 16 | 1.000 |
| DORMANT_REACTIVATION | 10 | 10 | 1.000 |
| FAN_IN | 50 | 36 | 0.720 |
| FAN_OUT | 29 | 22 | 0.759 |
| GEO_NEW_COUNTRY | 80 | 78 | 0.975 |
| HIGH_RISK_MERCHANT | 12 | 12 | 1.000 |
| IMPOSSIBLE_TRAVEL | 12 | 12 | 1.000 |
| NEW_DEVICE | 43 | 43 | 1.000 |
| PEER_AMOUNT_DEVIATION | 45 | 45 | 1.000 |
| RAPID_PASS_THROUGH | 31 | 28 | 0.903 |
| STRUCTURING | 14 | 10 | 0.714 |
| TRANSACTION_BURST | 28 | 24 | 0.857 |
| VELOCITY_SPIKE | 33 | 32 | 0.970 |

## Triage quality (oracle dispositions from the labels)

disposition = label of the alert's customer (suspicious -> confirmed, benign -> false positive).

463 alerts, overall confirmed rate 0.918. Confirmed rate falls monotonically with priority: **True**.

| priority | alerts | confirmed | confirmed rate | false-discovery rate |
|---|---|---|---|---|
| CRITICAL | 18 | 18 | 1.000 | 0.000 |
| HIGH | 274 | 269 | 0.982 | 0.018 |
| MEDIUM | 169 | 138 | 0.817 | 0.183 |
| LOW | 2 | 0 | 0.000 | 1.000 |

Top-k precision (share of the k highest-ranked alerts that the oracle confirms):

| ranking | top 10 | top 25 | top 50 | top 100 |
|---|---|---|---|---|
| triage_score | 1.000 | 1.000 | 1.000 | 1.000 |
| customer_risk_score | 1.000 | 1.000 | 1.000 | 1.000 |
| random_order | 1.000 | 0.920 | 0.920 | 0.900 |

Alert-level ROC-AUC: triage score 0.8279, customer risk score 0.9167.

## Money-mule indicators

Universe: 559 labelled customers + 400 sampled unlabelled (assumed benign); 44 are mule typologies (fan_in_collection, mule_account, mule_collector, mule_slow_evasive, mule_to_collector). FPR is valid here because every customer in the universe has a label or is an assumed-benign random sample.

| rule | precision | recall | F1 | FPR | TP | FP | FN | TN |
|---|---|---|---|---|---|---|---|---|
| mule band MEDIUM or HIGH | 0.5435 | 0.5682 | 0.5556 | 0.0230 | 25 | 21 | 19 | 894 |
| mule band HIGH | 1.0000 | 0.1818 | 0.3077 | 0.0000 | 8 | 0 | 36 | 915 |
| baseline: FAN_IN or RAPID_PASS_THROUGH alert | 0.6271 | 0.8409 | 0.7184 | 0.0240 | 37 | 22 | 7 | 893 |

ROC-AUC of the mule score: 0.9711.

| scenario | mule typology | customers | band counts | indicators that fired |
|---|---|---|---|---|
| (unlabelled sample, assumed benign) | False | 400 | {'LOW': 10, 'NONE': 389, 'MEDIUM': 1} | {'fan_out': 12, 'flagged_network': 178, 'layering_chain': 7, 'shared_device_identity': 6, 'low_retention': 2} |
| account_takeover | False | 12 | {'NONE': 11, 'LOW': 1} | {'flagged_network': 7, 'fan_out': 2} |
| circular_transfer | False | 15 | {'MEDIUM': 9, 'LOW': 6} | {'rapid_movement': 9, 'low_retention': 12, 'flagged_network': 15, 'layering_chain': 15} |
| device_sharing_ring | False | 16 | {'LOW': 16} | {'shared_device_identity': 16, 'flagged_network': 16, 'layering_chain': 1} |
| dormant_reactivation | False | 10 | {'NONE': 7, 'LOW': 3} | {'new_or_dormant_account': 10, 'flagged_network': 3, 'fan_out': 1} |
| fan_in_collection | True | 8 | {'LOW': 8} | {'fan_in': 8, 'flagged_network': 8} |
| fan_out_detectable | False | 8 | {'LOW': 7, 'NONE': 1} | {'fan_out': 8, 'flagged_network': 4} |
| fan_out_evasive | False | 8 | {'NONE': 8} | {'fan_out': 8, 'flagged_network': 1} |
| geographic_anomaly | False | 12 | {'NONE': 12} | {'flagged_network': 5} |
| high_frequency_legit | False | 12 | {'LOW': 12} | {'fan_out': 12, 'flagged_network': 12, 'layering_chain': 5, 'low_retention': 1} |
| high_risk_merchant | False | 10 | {'NONE': 10} | {'flagged_network': 3} |
| legit_family_collection | False | 6 | {'LOW': 5, 'MEDIUM': 1} | {'fan_in': 6, 'flagged_network': 6, 'low_retention': 3} |
| legit_first_payroll | False | 4 | {'LOW': 3, 'MEDIUM': 1} | {'fan_out': 4, 'flagged_network': 4, 'low_retention': 1, 'layering_chain': 1} |
| legit_high_value | False | 12 | {'NONE': 11, 'LOW': 1} | {'flagged_network': 7, 'low_retention': 1} |
| legit_marketplace_seller | False | 8 | {'MEDIUM': 5, 'LOW': 3} | {'fan_in': 8, 'fan_out': 4, 'low_retention': 5, 'flagged_network': 8} |
| legit_new_business_invoices | False | 6 | {'LOW': 3, 'NONE': 2, 'MEDIUM': 1} | {'fan_out': 5, 'flagged_network': 6, 'layering_chain': 2, 'rapid_movement': 1, 'low_retention': 1} |
| legit_payroll | False | 8 | {'LOW': 8} | {'fan_out': 8, 'flagged_network': 8, 'layering_chain': 3} |
| legit_recurring_large_payments | False | 10 | {'LOW': 4, 'NONE': 6} | {'fan_out': 7, 'flagged_network': 7, 'layering_chain': 3, 'low_retention': 1} |
| legit_rent_split | False | 8 | {'NONE': 2, 'MEDIUM': 3, 'LOW': 3} | {'rapid_movement': 3, 'low_retention': 3, 'flagged_network': 6, 'fan_in': 3} |
| mule_account | True | 12 | {'MEDIUM': 4, 'HIGH': 8} | {'fan_in': 12, 'fan_out': 12, 'rapid_movement': 11, 'flagged_network': 12, 'low_retention': 8} |
| mule_collector | True | 4 | {'NONE': 3, 'LOW': 1} | {'flagged_network': 4, 'fan_in': 1} |
| mule_slow_evasive | True | 8 | {'LOW': 6, 'MEDIUM': 2} | {'fan_in': 8, 'low_retention': 7, 'flagged_network': 8} |
| mule_to_collector | True | 12 | {'MEDIUM': 11, 'LOW': 1} | {'fan_in': 12, 'rapid_movement': 11, 'low_retention': 9, 'flagged_network': 12} |
| normal | False | 300 | {'NONE': 288, 'LOW': 12} | {'flagged_network': 119, 'fan_out': 15, 'low_retention': 1, 'layering_chain': 5, 'fan_in': 1} |
| rapid_single_pass_through | False | 8 | {'LOW': 1, 'NONE': 7} | {'rapid_movement': 2, 'low_retention': 1, 'flagged_network': 5} |
| structuring_detectable | False | 10 | {'NONE': 9, 'LOW': 1} | {'flagged_network': 5, 'rapid_movement': 1, 'low_retention': 1} |
| structuring_evasive | False | 10 | {'NONE': 9, 'LOW': 1} | {'low_retention': 3, 'flagged_network': 4} |
| transaction_burst | False | 12 | {'NONE': 12} | {'flagged_network': 4} |
| travel_legit | False | 10 | {'NONE': 10} | {'flagged_network': 4} |

## Alert volume

- alerts: 463 on 173 customers; severity {'low': 59, 'medium': 91, 'high': 293, 'critical': 20}
- **alerts per 1,000 transactions** (transactions in the 30-day window, 19,062): 24.289
- alerts per 1,000 customers: 115.75

## Speed on this machine (single process, in-memory store)

- monitoring run over all 4,000 customers: 429.66 s (9.31 customers/s, 44.4 window transactions/s)
- detection latency per customer (assessment + detector results, n=300): mean 109.62 ms, p50 105.85 ms, p95 154.83 ms, p99 177.52 ms

Latency here is processing time. The benchmark runs in batch mode, so dataset-time lag between a transaction and its alert is not measured.

## How to reproduce

```bash
cd backend
python -m app.synthetic.monitoring_benchmark --out ../data/benchmark --customers 4000 --seed 7
python -m app.evaluation.monitoring_eval --dataset ../data/benchmark --out ../evaluation/results/monitoring
```

Counts and rates are deterministic for a given seed, configuration and library versions. Timings vary by machine.
