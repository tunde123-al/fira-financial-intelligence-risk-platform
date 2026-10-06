# FIRA transaction-monitoring evaluation (2026-10-03)

SYNTHETIC data only. These numbers describe how the pipeline behaves on a generated bank whose scenarios were written by the same authors as the detectors. They are not evidence of real-world AML or fraud detection performance.

- dataset: fira-monitoring-benchmark, seed 2025, 4,000 customers, 102,556 transactions, 133 suspicious customers (prevalence 3.325%)
- configuration: risk `default-2` (2b6a54079660), monitoring `monitoring-1` (466b8efd1761), lookback 30 d, baseline 90 d, window end 2026-09-30
- thresholds tuned on this data: **False**
- environment: Python 3.13.7, Windows-10-10.0.19045-SP0

## Customer-level detection (a customer is positive if any alert was raised)

| precision | recall | F1 | FPR | PR-AUC | ROC-AUC | TP | FP | FN | TN |
|---|---|---|---|---|---|---|---|---|---|
| 0.8582 | 0.8647 | 0.8614 | 0.0049 | 0.8590 | 0.9317 | 115 | 19 | 18 | 3848 |

PR-AUC ranks customers by their highest alert risk score (no alert = 0). The no-skill PR-AUC equals the prevalence, 0.0333.

### Operating points (descriptive; the shipped configuration alerts at any score)

| min alert risk score | precision | recall | F1 | FPR | TP | FP | FN |
|---|---|---|---|---|---|---|---|
| 0 | 0.8582 | 0.8647 | 0.8614 | 0.0049 | 115 | 19 | 18 |
| 20 | 0.9274 | 0.8647 | 0.8949 | 0.0023 | 115 | 9 | 18 |
| 30 | 0.9640 | 0.8045 | 0.8770 | 0.0010 | 107 | 4 | 26 |
| 40 | 0.9787 | 0.6917 | 0.8106 | 0.0005 | 92 | 2 | 41 |
| 50 | 1.0000 | 0.3985 | 0.5699 | 0.0000 | 53 | 0 | 80 |
| 60 | 1.0000 | 0.1729 | 0.2949 | 0.0000 | 23 | 0 | 110 |

## Per scenario

| scenario | suspicious | customers | alerted | alert rate |
|---|---|---|---|---|
| (unlabelled, assumed benign) | False | 3505 | 8 | 0.002 |
| account_takeover | True | 12 | 12 | 1.000 |
| circular_transfer | True | 14 | 14 | 1.000 |
| device_sharing_ring | True | 15 | 15 | 1.000 |
| dormant_reactivation | True | 10 | 10 | 1.000 |
| fan_out_detectable | True | 8 | 8 | 1.000 |
| fan_out_evasive | True | 8 | 0 | 0.000 |
| geographic_anomaly | True | 12 | 12 | 1.000 |
| high_frequency_legit | False | 12 | 0 | 0.000 |
| high_risk_merchant | True | 10 | 10 | 1.000 |
| legit_first_payroll | False | 4 | 4 | 1.000 |
| legit_high_value | False | 12 | 0 | 0.000 |
| legit_new_business_invoices | False | 6 | 6 | 1.000 |
| legit_payroll | False | 8 | 0 | 0.000 |
| legit_recurring_large_payments | False | 10 | 0 | 0.000 |
| mule_account | True | 12 | 12 | 1.000 |
| normal | False | 300 | 1 | 0.003 |
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
| fan_out_detectable | 8 | 8 | 1.000 |
| geographic_anomaly | 12 | 12 | 1.000 |
| high_risk_merchant | 10 | 10 | 1.000 |
| mule_account | 12 | 12 | 1.000 |
| structuring_detectable | 10 | 10 | 1.000 |
| transaction_burst | 12 | 12 | 1.000 |

## Per detector (alerts raised)

| detector | alerts | on suspicious customers | alert precision |
|---|---|---|---|
| AMOUNT_DEVIATION | 30 | 30 | 1.000 |
| CIRCULAR_FLOW | 14 | 14 | 1.000 |
| DEVICE_SHARING | 15 | 15 | 1.000 |
| DORMANT_REACTIVATION | 10 | 10 | 1.000 |
| FAN_IN | 16 | 12 | 0.750 |
| FAN_OUT | 28 | 20 | 0.714 |
| GEO_NEW_COUNTRY | 60 | 60 | 1.000 |
| HIGH_RISK_MERCHANT | 11 | 11 | 1.000 |
| IMPOSSIBLE_TRAVEL | 12 | 12 | 1.000 |
| NEW_DEVICE | 44 | 44 | 1.000 |
| PEER_AMOUNT_DEVIATION | 27 | 27 | 1.000 |
| RAPID_PASS_THROUGH | 18 | 16 | 0.889 |
| STRUCTURING | 16 | 10 | 0.625 |
| TRANSACTION_BURST | 28 | 24 | 0.857 |
| VELOCITY_SPIKE | 36 | 34 | 0.944 |

## Alert volume

- alerts: 365 on 134 customers; severity {'low': 55, 'high': 236, 'critical': 14, 'medium': 60}
- **alerts per 1,000 transactions** (transactions in the 30-day window, 18,123): 20.14
- alerts per 1,000 customers: 91.25

## Speed on this machine (single process, in-memory store)

- monitoring run over all 4,000 customers: 425.57 s (9.4 customers/s, 42.6 window transactions/s)
- detection latency per customer (assessment + detector results, n=300): mean 103.97 ms, p50 103.86 ms, p95 133.06 ms, p99 165.17 ms

Latency here is processing time. The benchmark runs in batch mode, so dataset-time lag between a transaction and its alert is not measured.

## How to reproduce

```bash
cd backend
python -m app.synthetic.monitoring_benchmark --out ../data/benchmark --customers 4000 --seed 2025
python -m app.evaluation.monitoring_eval --dataset ../data/benchmark --out ../evaluation/results/monitoring
```

Counts and rates are deterministic for a given seed, configuration and library versions. Timings vary by machine.
