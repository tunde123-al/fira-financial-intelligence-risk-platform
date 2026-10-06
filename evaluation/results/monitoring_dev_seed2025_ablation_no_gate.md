# FIRA transaction-monitoring evaluation (2026-10-03)

SYNTHETIC data only. These numbers describe how the pipeline behaves on a generated bank whose scenarios were written by the same authors as the detectors. They are not evidence of real-world AML or fraud detection performance.

- dataset: fira-monitoring-benchmark, seed 2025, 4,000 customers, 102,556 transactions, 133 suspicious customers (prevalence 3.325%)
- configuration: risk `default-2` (2b6a54079660), monitoring `monitoring-1` (23f6bc272d1a), lookback 30 d, baseline 90 d, window end 2026-09-30
- thresholds tuned on this data: **False**; supporting-detector gate: 0.0
- environment: Python 3.13.7, Windows-10-10.0.19045-SP0

## Customer-level detection (a customer is positive if any alert was raised)

| precision | recall | F1 | FPR | PR-AUC | ROC-AUC | TP | FP | FN | TN |
|---|---|---|---|---|---|---|---|---|---|
| 0.1611 | 0.9850 | 0.2770 | 0.1764 | 0.9182 | 0.9841 | 131 | 682 | 2 | 3185 |

PR-AUC ranks customers by their highest alert risk score (no alert = 0). The no-skill PR-AUC equals the prevalence, 0.0333.

### Operating points (descriptive; the shipped configuration alerts at any score)

| min alert risk score | precision | recall | F1 | FPR | TP | FP | FN |
|---|---|---|---|---|---|---|---|
| 0 | 0.1611 | 0.9850 | 0.2770 | 0.1764 | 131 | 682 | 2 |
| 20 | 0.7246 | 0.9098 | 0.8067 | 0.0119 | 121 | 46 | 12 |
| 30 | 0.9469 | 0.8045 | 0.8699 | 0.0016 | 107 | 6 | 26 |
| 40 | 0.9787 | 0.6917 | 0.8106 | 0.0005 | 92 | 2 | 41 |
| 50 | 1.0000 | 0.3985 | 0.5699 | 0.0000 | 53 | 0 | 80 |
| 60 | 1.0000 | 0.1729 | 0.2949 | 0.0000 | 23 | 0 | 110 |

## Per scenario

| scenario | suspicious | customers | alerted | alert rate |
|---|---|---|---|---|
| (unlabelled, assumed benign) | False | 3505 | 590 | 0.168 |
| account_takeover | True | 12 | 12 | 1.000 |
| circular_transfer | True | 14 | 14 | 1.000 |
| device_sharing_ring | True | 15 | 15 | 1.000 |
| dormant_reactivation | True | 10 | 10 | 1.000 |
| fan_out_detectable | True | 8 | 8 | 1.000 |
| fan_out_evasive | True | 8 | 6 | 0.750 |
| geographic_anomaly | True | 12 | 12 | 1.000 |
| high_frequency_legit | False | 12 | 4 | 0.333 |
| high_risk_merchant | True | 10 | 10 | 1.000 |
| legit_first_payroll | False | 4 | 4 | 1.000 |
| legit_high_value | False | 12 | 7 | 0.583 |
| legit_new_business_invoices | False | 6 | 6 | 1.000 |
| legit_payroll | False | 8 | 8 | 1.000 |
| legit_recurring_large_payments | False | 10 | 0 | 0.000 |
| mule_account | True | 12 | 12 | 1.000 |
| normal | False | 300 | 54 | 0.180 |
| structuring_detectable | True | 10 | 10 | 1.000 |
| structuring_evasive | True | 10 | 10 | 1.000 |
| transaction_burst | True | 12 | 12 | 1.000 |
| travel_legit | False | 10 | 9 | 0.900 |

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
| AMOUNT_DEVIATION | 258 | 40 | 0.155 |
| CIRCULAR_FLOW | 14 | 14 | 1.000 |
| DEVICE_SHARING | 15 | 15 | 1.000 |
| DORMANT_REACTIVATION | 10 | 10 | 1.000 |
| FAN_IN | 16 | 12 | 0.750 |
| FAN_OUT | 28 | 20 | 0.714 |
| GEO_NEW_COUNTRY | 241 | 66 | 0.274 |
| HIGH_RISK_MERCHANT | 11 | 11 | 1.000 |
| IMPOSSIBLE_TRAVEL | 12 | 12 | 1.000 |
| NEW_DEVICE | 351 | 50 | 0.142 |
| PEER_AMOUNT_DEVIATION | 53 | 44 | 0.830 |
| RAPID_PASS_THROUGH | 18 | 16 | 0.889 |
| STRUCTURING | 16 | 10 | 0.625 |
| TRANSACTION_BURST | 28 | 24 | 0.857 |
| VELOCITY_SPIKE | 62 | 48 | 0.774 |

## Alert volume

- alerts: 1,133 on 813 customers; severity {'high': 677, 'low': 307, 'medium': 135, 'critical': 14}
- **alerts per 1,000 transactions** (transactions in the 30-day window, 18,123): 62.517
- alerts per 1,000 customers: 283.25

## Speed on this machine (single process, in-memory store)

- monitoring run over all 4,000 customers: 413.54 s (9.67 customers/s, 43.8 window transactions/s)
- detection latency per customer (assessment + detector results, n=300): mean 99.2 ms, p50 100.27 ms, p95 121.41 ms, p99 149.5 ms

Latency here is processing time. The benchmark runs in batch mode, so dataset-time lag between a transaction and its alert is not measured.

## How to reproduce

```bash
cd backend
python -m app.synthetic.monitoring_benchmark --out ../data/benchmark --customers 4000 --seed 2025
python -m app.evaluation.monitoring_eval --dataset ../data/benchmark --out ../evaluation/results/monitoring
```

Counts and rates are deterministic for a given seed, configuration and library versions. Timings vary by machine.
