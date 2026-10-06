# FIRA transaction-monitoring evaluation (2026-10-03)

SYNTHETIC data only. These numbers describe how the pipeline behaves on a generated bank whose scenarios were written by the same authors as the detectors. They are not evidence of real-world AML or fraud detection performance.

- dataset: fira-monitoring-benchmark, seed 2025, 4,000 customers, 103,073 transactions, 173 suspicious customers (prevalence 4.325%)
- configuration: risk `default-2` (2b6a54079660), monitoring `monitoring-1` (81b6f95261e4), lookback 30 d, baseline 90 d, window end 2026-09-30
- thresholds tuned on this data: **False**; supporting-detector gate: default (investigation threshold)
- environment: Python 3.13.7, Windows-10-10.0.19045-SP0

## Customer-level detection (a customer is positive if any alert was raised)

| precision | recall | F1 | FPR | PR-AUC | ROC-AUC | TP | FP | FN | TN |
|---|---|---|---|---|---|---|---|---|---|
| 0.8000 | 0.8092 | 0.8046 | 0.0091 | 0.7871 | 0.9028 | 140 | 35 | 33 | 3792 |

PR-AUC ranks customers by their highest alert risk score (no alert = 0). The no-skill PR-AUC equals the prevalence, 0.0432.

### Operating points (descriptive; the shipped configuration alerts at any score)

| min alert risk score | precision | recall | F1 | FPR | TP | FP | FN |
|---|---|---|---|---|---|---|---|
| 0 | 0.8000 | 0.8092 | 0.8046 | 0.0091 | 140 | 35 | 33 |
| 20 | 0.8485 | 0.8092 | 0.8284 | 0.0065 | 140 | 25 | 33 |
| 30 | 0.9041 | 0.7630 | 0.8276 | 0.0037 | 132 | 14 | 41 |
| 40 | 0.9316 | 0.6301 | 0.7517 | 0.0021 | 109 | 8 | 64 |
| 50 | 0.9857 | 0.3988 | 0.5679 | 0.0003 | 69 | 1 | 104 |
| 60 | 1.0000 | 0.1561 | 0.2700 | 0.0000 | 27 | 0 | 146 |

## Per scenario

| scenario | suspicious | customers | alerted | alert rate |
|---|---|---|---|---|
| (unlabelled, assumed benign) | False | 3443 | 6 | 0.002 |
| account_takeover | True | 12 | 12 | 1.000 |
| circular_transfer | True | 14 | 14 | 1.000 |
| device_sharing_ring | True | 15 | 15 | 1.000 |
| dormant_reactivation | True | 10 | 10 | 1.000 |
| fan_in_collection | True | 8 | 8 | 1.000 |
| fan_out_detectable | True | 8 | 8 | 1.000 |
| fan_out_evasive | True | 8 | 0 | 0.000 |
| geographic_anomaly | True | 12 | 12 | 1.000 |
| high_frequency_legit | False | 12 | 0 | 0.000 |
| high_risk_merchant | True | 10 | 10 | 1.000 |
| legit_family_collection | False | 6 | 5 | 0.833 |
| legit_first_payroll | False | 4 | 4 | 1.000 |
| legit_high_value | False | 12 | 0 | 0.000 |
| legit_marketplace_seller | False | 8 | 5 | 0.625 |
| legit_new_business_invoices | False | 6 | 6 | 1.000 |
| legit_payroll | False | 8 | 0 | 0.000 |
| legit_recurring_large_payments | False | 10 | 0 | 0.000 |
| legit_rent_split | False | 8 | 6 | 0.750 |
| mule_account | True | 12 | 12 | 1.000 |
| mule_collector | True | 4 | 0 | 0.000 |
| mule_slow_evasive | True | 8 | 5 | 0.625 |
| mule_to_collector | True | 12 | 12 | 1.000 |
| normal | False | 300 | 3 | 0.010 |
| rapid_single_pass_through | True | 8 | 0 | 0.000 |
| structuring_detectable | True | 10 | 10 | 1.000 |
| structuring_evasive | True | 10 | 0 | 0.000 |
| transaction_burst | True | 12 | 12 | 1.000 |
| travel_legit | False | 10 | 0 | 0.000 |

For suspicious scenarios the alert rate is recall; for benign ones it is the false-positive rate.

### Did the *expected* detector fire?

| scenario | customers | expected detector alerted | rate |
|---|---|---|---|
| account_takeover | 12 | 12 | 1.000 |
| circular_transfer | 14 | 14 | 1.000 |
| device_sharing_ring | 15 | 15 | 1.000 |
| dormant_reactivation | 10 | 10 | 1.000 |
| fan_in_collection | 8 | 8 | 1.000 |
| fan_out_detectable | 8 | 8 | 1.000 |
| geographic_anomaly | 12 | 12 | 1.000 |
| high_risk_merchant | 10 | 10 | 1.000 |
| mule_account | 12 | 12 | 1.000 |
| mule_to_collector | 12 | 12 | 1.000 |
| rapid_single_pass_through | 8 | 0 | 0.000 |
| structuring_detectable | 10 | 10 | 1.000 |
| transaction_burst | 12 | 12 | 1.000 |

## Per detector (alerts raised)

| detector | alerts | on suspicious customers | alert precision |
|---|---|---|---|
| AMOUNT_DEVIATION | 44 | 40 | 0.909 |
| CIRCULAR_FLOW | 14 | 14 | 1.000 |
| DEVICE_SHARING | 15 | 15 | 1.000 |
| DORMANT_REACTIVATION | 10 | 10 | 1.000 |
| FAN_IN | 51 | 37 | 0.726 |
| FAN_OUT | 28 | 20 | 0.714 |
| GEO_NEW_COUNTRY | 84 | 79 | 0.941 |
| HIGH_RISK_MERCHANT | 11 | 11 | 1.000 |
| IMPOSSIBLE_TRAVEL | 12 | 12 | 1.000 |
| NEW_DEVICE | 49 | 47 | 0.959 |
| PEER_AMOUNT_DEVIATION | 44 | 42 | 0.955 |
| RAPID_PASS_THROUGH | 33 | 25 | 0.758 |
| STRUCTURING | 16 | 10 | 0.625 |
| TRANSACTION_BURST | 28 | 24 | 0.857 |
| VELOCITY_SPIKE | 36 | 34 | 0.944 |

## Triage quality (oracle dispositions from the labels)

disposition = label of the alert's customer (suspicious -> confirmed, benign -> false positive).

475 alerts, overall confirmed rate 0.884. Confirmed rate falls monotonically with priority: **False**.

| priority | alerts | confirmed | confirmed rate | false-discovery rate |
|---|---|---|---|---|
| HIGH | 124 | 114 | 0.919 | 0.081 |
| MEDIUM | 344 | 299 | 0.869 | 0.131 |
| LOW | 7 | 7 | 1.000 | 0.000 |

Top-k precision (share of the k highest-ranked alerts that the oracle confirms):

| ranking | top 10 | top 25 | top 50 | top 100 |
|---|---|---|---|---|
| triage_score | 1.000 | 1.000 | 0.960 | 0.950 |
| customer_risk_score | 1.000 | 1.000 | 1.000 | 1.000 |
| random_order | 1.000 | 0.880 | 0.860 | 0.880 |

Alert-level ROC-AUC: triage score 0.6510, customer risk score 0.8675.

## Money-mule indicators

Universe: 557 labelled customers + 400 sampled unlabelled (assumed benign); 44 are mule typologies (fan_in_collection, mule_account, mule_collector, mule_slow_evasive, mule_to_collector). FPR is valid here because every customer in the universe has a label or is an assumed-benign random sample.

| rule | precision | recall | F1 | FPR | TP | FP | FN | TN |
|---|---|---|---|---|---|---|---|---|
| mule band MEDIUM or HIGH | 0.4615 | 0.5455 | 0.5000 | 0.0307 | 24 | 28 | 20 | 885 |
| mule band HIGH | 1.0000 | 0.2045 | 0.3396 | 0.0000 | 9 | 0 | 35 | 913 |
| baseline: FAN_IN or RAPID_PASS_THROUGH alert | 0.6167 | 0.8409 | 0.7115 | 0.0252 | 37 | 23 | 7 | 890 |

ROC-AUC of the mule score: 0.9588.

| scenario | mule typology | customers | band counts | indicators that fired |
|---|---|---|---|---|
| (unlabelled sample, assumed benign) | False | 400 | {'NONE': 387, 'LOW': 13} | {'flagged_network': 258, 'shared_device_identity': 8, 'fan_out': 11, 'layering_chain': 7, 'low_retention': 7} |
| account_takeover | False | 12 | {'NONE': 11, 'LOW': 1} | {'flagged_network': 6, 'fan_out': 1} |
| circular_transfer | False | 14 | {'NONE': 2, 'MEDIUM': 7, 'LOW': 5} | {'flagged_network': 14, 'layering_chain': 14, 'rapid_movement': 7, 'low_retention': 11} |
| device_sharing_ring | False | 15 | {'LOW': 8, 'NONE': 7} | {'shared_device_identity': 15, 'flagged_network': 15, 'layering_chain': 2} |
| dormant_reactivation | False | 10 | {'NONE': 10} | {'new_or_dormant_account': 10, 'flagged_network': 1} |
| fan_in_collection | True | 8 | {'LOW': 6, 'MEDIUM': 2} | {'fan_in': 8, 'low_retention': 3, 'flagged_network': 8, 'layering_chain': 1} |
| fan_out_detectable | False | 8 | {'LOW': 7, 'MEDIUM': 1} | {'fan_out': 8, 'flagged_network': 6, 'low_retention': 2} |
| fan_out_evasive | False | 8 | {'LOW': 5, 'NONE': 3} | {'fan_out': 8, 'flagged_network': 6} |
| geographic_anomaly | False | 12 | {'NONE': 12} | {'flagged_network': 8} |
| high_frequency_legit | False | 12 | {'MEDIUM': 2, 'LOW': 10} | {'fan_out': 12, 'low_retention': 3, 'flagged_network': 5, 'layering_chain': 7, 'rapid_movement': 1} |
| high_risk_merchant | False | 10 | {'NONE': 10} | {'flagged_network': 7} |
| legit_family_collection | False | 6 | {'LOW': 6} | {'fan_in': 6, 'flagged_network': 6, 'low_retention': 3} |
| legit_first_payroll | False | 4 | {'LOW': 3, 'MEDIUM': 1} | {'fan_out': 4, 'flagged_network': 4, 'rapid_movement': 1, 'low_retention': 1, 'layering_chain': 2} |
| legit_high_value | False | 12 | {'NONE': 11, 'LOW': 1} | {'flagged_network': 10, 'low_retention': 1} |
| legit_marketplace_seller | False | 8 | {'MEDIUM': 6, 'LOW': 2} | {'fan_in': 8, 'fan_out': 5, 'low_retention': 7, 'flagged_network': 7} |
| legit_new_business_invoices | False | 6 | {'NONE': 2, 'LOW': 3, 'MEDIUM': 1} | {'fan_out': 4, 'flagged_network': 5, 'layering_chain': 2, 'low_retention': 1} |
| legit_payroll | False | 8 | {'LOW': 6, 'MEDIUM': 2} | {'fan_out': 8, 'flagged_network': 7, 'layering_chain': 5, 'low_retention': 2} |
| legit_recurring_large_payments | False | 10 | {'LOW': 7, 'NONE': 3} | {'fan_out': 6, 'flagged_network': 7, 'low_retention': 1, 'layering_chain': 4} |
| legit_rent_split | False | 8 | {'MEDIUM': 5, 'NONE': 2, 'LOW': 1} | {'rapid_movement': 6, 'low_retention': 6, 'flagged_network': 7, 'fan_out': 1} |
| mule_account | True | 12 | {'HIGH': 9, 'MEDIUM': 3} | {'fan_in': 12, 'fan_out': 12, 'rapid_movement': 10, 'low_retention': 9, 'flagged_network': 8} |
| mule_collector | True | 4 | {'NONE': 3, 'LOW': 1} | {'flagged_network': 4, 'fan_in': 1} |
| mule_slow_evasive | True | 8 | {'LOW': 7, 'MEDIUM': 1} | {'fan_in': 8, 'low_retention': 6, 'flagged_network': 8} |
| mule_to_collector | True | 12 | {'MEDIUM': 9, 'LOW': 3} | {'fan_in': 12, 'rapid_movement': 9, 'flagged_network': 12, 'low_retention': 8} |
| normal | False | 300 | {'NONE': 277, 'LOW': 22, 'MEDIUM': 1} | {'flagged_network': 197, 'layering_chain': 14, 'fan_out': 22, 'low_retention': 10, 'rapid_movement': 1} |
| rapid_single_pass_through | False | 8 | {'NONE': 6, 'LOW': 1, 'MEDIUM': 1} | {'flagged_network': 6, 'rapid_movement': 2, 'low_retention': 2, 'fan_out': 1} |
| structuring_detectable | False | 10 | {'NONE': 8, 'LOW': 2} | {'flagged_network': 10, 'low_retention': 3} |
| structuring_evasive | False | 10 | {'NONE': 8, 'MEDIUM': 1, 'LOW': 1} | {'low_retention': 4, 'rapid_movement': 1, 'flagged_network': 4} |
| transaction_burst | False | 12 | {'NONE': 12} | {'flagged_network': 6} |
| travel_legit | False | 10 | {'NONE': 10} | {'flagged_network': 6} |

## Alert volume

- alerts: 475 on 175 customers; severity {'medium': 75, 'high': 302, 'low': 78, 'critical': 20}
- **alerts per 1,000 transactions** (transactions in the 30-day window, 18,640): 25.483
- alerts per 1,000 customers: 118.75

## Speed on this machine (single process, in-memory store)

- monitoring run over all 4,000 customers: 403.18 s (9.92 customers/s, 46.2 window transactions/s)
- detection latency per customer (assessment + detector results, n=300): mean 100.68 ms, p50 101.28 ms, p95 125.44 ms, p99 159.56 ms

Latency here is processing time. The benchmark runs in batch mode, so dataset-time lag between a transaction and its alert is not measured.

## How to reproduce

```bash
cd backend
python -m app.synthetic.monitoring_benchmark --out ../data/benchmark --customers 4000 --seed 2025
python -m app.evaluation.monitoring_eval --dataset ../data/benchmark --out ../evaluation/results/monitoring
```

Counts and rates are deterministic for a given seed, configuration and library versions. Timings vary by machine.
