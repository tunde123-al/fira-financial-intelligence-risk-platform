# FIRA transaction-monitoring evaluation (2026-10-03)

SYNTHETIC data only. These numbers describe how the pipeline behaves on a generated bank whose scenarios were written by the same authors as the detectors. They are not evidence of real-world AML or fraud detection performance.

- dataset: fira-monitoring-benchmark, seed 7, 3,000 customers, 77,469 transactions, 100 suspicious customers (prevalence 3.333%)
- configuration: risk `default-2` (2b6a54079660), monitoring `monitoring-1` (466b8efd1761), lookback 30 d, baseline 90 d, window end 2026-09-30
- thresholds tuned on this data: **False**
- environment: Python 3.13.7, Windows-10-10.0.19045-SP0

## Customer-level detection (a customer is positive if any alert was raised)

| precision | recall | F1 | FPR | PR-AUC | ROC-AUC | TP | FP | FN | TN |
|---|---|---|---|---|---|---|---|---|---|
| 0.8776 | 0.8600 | 0.8687 | 0.0041 | 0.8552 | 0.9295 | 86 | 12 | 14 | 2888 |

PR-AUC ranks customers by their highest alert risk score (no alert = 0). The no-skill PR-AUC equals the prevalence, 0.0333.

### Operating points (descriptive; the shipped configuration alerts at any score)

| min alert risk score | precision | recall | F1 | FPR | TP | FP | FN |
|---|---|---|---|---|---|---|---|
| 0 | 0.8776 | 0.8600 | 0.8687 | 0.0041 | 86 | 12 | 14 |
| 20 | 0.9247 | 0.8600 | 0.8912 | 0.0024 | 86 | 7 | 14 |
| 30 | 0.9540 | 0.8300 | 0.8877 | 0.0014 | 83 | 4 | 17 |
| 40 | 0.9718 | 0.6900 | 0.8070 | 0.0007 | 69 | 2 | 31 |
| 50 | 1.0000 | 0.4300 | 0.6014 | 0.0000 | 43 | 0 | 57 |
| 60 | 1.0000 | 0.2100 | 0.3471 | 0.0000 | 21 | 0 | 79 |

## Per scenario

| scenario | suspicious | customers | alerted | alert rate |
|---|---|---|---|---|
| (unlabelled, assumed benign) | False | 2553 | 4 | 0.002 |
| account_takeover | True | 9 | 9 | 1.000 |
| circular_transfer | True | 11 | 11 | 1.000 |
| device_sharing_ring | True | 9 | 9 | 1.000 |
| dormant_reactivation | True | 8 | 8 | 1.000 |
| fan_out_detectable | True | 6 | 6 | 1.000 |
| fan_out_evasive | True | 6 | 0 | 0.000 |
| geographic_anomaly | True | 9 | 9 | 1.000 |
| high_frequency_legit | False | 9 | 0 | 0.000 |
| high_risk_merchant | True | 8 | 8 | 1.000 |
| legit_first_payroll | False | 3 | 3 | 1.000 |
| legit_high_value | False | 9 | 0 | 0.000 |
| legit_new_business_invoices | False | 4 | 4 | 1.000 |
| legit_payroll | False | 6 | 0 | 0.000 |
| legit_recurring_large_payments | False | 8 | 0 | 0.000 |
| mule_account | True | 9 | 9 | 1.000 |
| normal | False | 300 | 0 | 0.000 |
| structuring_detectable | True | 8 | 8 | 1.000 |
| structuring_evasive | True | 8 | 0 | 0.000 |
| transaction_burst | True | 9 | 9 | 1.000 |
| travel_legit | False | 8 | 1 | 0.125 |

For suspicious scenarios the alert rate is recall; for benign ones it is the false-positive rate.

### Did the *expected* detector fire?

| scenario | customers | expected detector alerted | rate |
|---|---|---|---|
| account_takeover | 9 | 9 | 1.000 |
| circular_transfer | 11 | 11 | 1.000 |
| device_sharing_ring | 9 | 9 | 1.000 |
| dormant_reactivation | 8 | 8 | 1.000 |
| fan_out_detectable | 6 | 6 | 1.000 |
| geographic_anomaly | 9 | 9 | 1.000 |
| high_risk_merchant | 8 | 8 | 1.000 |
| mule_account | 9 | 9 | 1.000 |
| structuring_detectable | 8 | 8 | 1.000 |
| transaction_burst | 9 | 9 | 1.000 |

## Per detector (alerts raised)

| detector | alerts | on suspicious customers | alert precision |
|---|---|---|---|
| AMOUNT_DEVIATION | 23 | 23 | 1.000 |
| CIRCULAR_FLOW | 11 | 11 | 1.000 |
| DEVICE_SHARING | 9 | 9 | 1.000 |
| DORMANT_REACTIVATION | 8 | 8 | 1.000 |
| FAN_IN | 10 | 9 | 0.900 |
| FAN_OUT | 19 | 15 | 0.789 |
| GEO_NEW_COUNTRY | 50 | 49 | 0.980 |
| HIGH_RISK_MERCHANT | 8 | 8 | 1.000 |
| IMPOSSIBLE_TRAVEL | 10 | 9 | 0.900 |
| NEW_DEVICE | 30 | 30 | 1.000 |
| PEER_AMOUNT_DEVIATION | 21 | 21 | 1.000 |
| RAPID_PASS_THROUGH | 15 | 14 | 0.933 |
| STRUCTURING | 12 | 8 | 0.667 |
| TRANSACTION_BURST | 21 | 18 | 0.857 |
| VELOCITY_SPIKE | 23 | 22 | 0.957 |

## Alert volume

- alerts: 270 on 98 customers; severity {'high': 173, 'critical': 19, 'medium': 50, 'low': 28}
- **alerts per 1,000 transactions** (transactions in the 30-day window, 13,686): 19.728
- alerts per 1,000 customers: 90.0

## Speed on this machine (single process, in-memory store)

- monitoring run over all 3,000 customers: 316.91 s (9.47 customers/s, 43.2 window transactions/s)
- detection latency per customer (assessment + detector results, n=300): mean 100.64 ms, p50 101.14 ms, p95 126.29 ms, p99 147.17 ms

Latency here is processing time. The benchmark runs in batch mode, so dataset-time lag between a transaction and its alert is not measured.

## How to reproduce

```bash
cd backend
python -m app.synthetic.monitoring_benchmark --out ../data/benchmark --customers 3000 --seed 7
python -m app.evaluation.monitoring_eval --dataset ../data/benchmark --out ../evaluation/results/monitoring
```

Counts and rates are deterministic for a given seed, configuration and library versions. Timings vary by machine.
