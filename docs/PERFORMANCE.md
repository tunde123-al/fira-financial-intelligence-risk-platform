# Performance

Everything here was **measured** by `python -m app.monitoring.benchmark` (and `infrastructure/scripts/index_review.py`) on one
development machine on 2026-10-03. Nothing is extrapolated and nothing is a capacity guarantee. Raw output:
[`benchmarks/perf_v3_2026-10-03.md`](benchmarks/perf_v3_2026-10-03.md) / `.json`, the committed regression baseline
[`benchmarks/perf_baseline.json`](benchmarks/perf_baseline.json), and [`benchmarks/index_review_2026-10-03.md`](benchmarks/index_review_2026-10-03.md).
The earlier v2 run is kept at `benchmarks/perf_2026-10-03.md`.

**Machine:** Windows 10, Intel Core i5-3470 (4 logical CPUs, 2012-era), 7.9 GB RAM, Python 3.13, PostgreSQL 15 in a Docker
container on the same machine, single process, synthetic data from the standard generator. The run was the only heavy job.

**Sizes run:** 10,446, 101,323 and 251,346 transactions (394, 3,937 and 9,843 customers). **1,000,000 transactions was not
run.** The machine had about 1.9 GB of free memory with Docker running; the generator and in-memory store hold every transaction in
RAM (the 251k run peaked at 677 MB in memory mode and 806 MB with PostgreSQL), and a 1M run would exceed what is safely available.
No 1M figure is extrapolated here.

## What dominates: risk assessment per customer

Monitoring cost follows the **number of customers screened**, not the total transaction count.

| Transactions | Store | Risk scoring per customer (p50) | Daily batch: customers screened | Batch time | Throughput |
|---|---|---|---|---|---|
| 10,446 | in-memory | 100 ms | 43 | 4.7 s | 9.2 customers/s |
| 10,446 | PostgreSQL | 115 ms | 43 | 9.0 s | 4.8 customers/s |
| 101,323 | in-memory | 104 ms | 538 | 61.4 s | 8.8 customers/s |
| 101,323 | PostgreSQL | 118 ms | 538 | 73.0 s | 7.4 customers/s |
| 251,346 | in-memory | 106 ms | 1,153 | 130.0 s | 8.9 customers/s |
| 251,346 | PostgreSQL | 113 ms | 1,153 | 154.8 s | 7.5 customers/s |

The batch screens customers with a transaction in the last day of the dataset (14, 32 and 103 alerts created). Re-running the same
batch creates nothing and costs about as much (merge path: 4.7 / 60.5 / 129.0 s in memory). At roughly 0.1 s per customer, screening a
million customers in one process would take more than a day (arithmetic, not a measurement). This is **not** production throughput.

## Load and ingestion

| Transactions | Bulk load into PostgreSQL (`COPY`) | Load + graph build, in-memory |
|---|---|---|
| 10,446 | 1.4 s (7,651 tx/s) | 4.6 s |
| 101,323 | 10.6 s (9,552 tx/s) | 3.3 s |
| 251,346 | 24.7 s (10,168 tx/s) | 8.0 s |

Batch ingestion through the data-quality gate (`POST /api/monitoring/transactions`, 5,000 valid rows, no monitoring) and the same
gate with ~10% invalid rows (2,000 rows: negative amounts, unknown accounts, repeated ids):

| Transactions already stored | clean batch, in-memory | clean batch, PostgreSQL | dirty batch, in-memory | dirty batch, PostgreSQL |
|---|---|---|---|---|
| 10,446 | 1,048 rows/s | 815 rows/s | 962 rows/s | 637 rows/s |
| 101,323 | 785 rows/s | 578 rows/s | 534 rows/s | 394 rows/s |
| 251,346 | 554 rows/s | 453 rows/s | 293 rows/s | 237 rows/s |

Accounting balanced (`received = processed + rejected + failed`) in every dirty run. Ingestion is **slower than in v2**
(v2: 2,139 / 1,452 / 764 rows/s in memory) because the gate now looks up account status, checks near-duplicate parties against recent
transactions, writes the batch ledger and the quarantine rows, and the validation allocates per-row state. **Known weakness
(unchanged): every accepted batch rebuilds the in-memory graph projection**, which is why throughput falls as the bank grows. An
incremental projection would remove it; it is not built.

## Investigator and operator queries (p95 latency)

| Query | in-memory 251k | PostgreSQL 251k |
|---|---|---|
| alert queue, page of 50 | 9.9 ms | 28 ms |
| alert queue, filtered and sorted | 9.5 ms | 30 ms |
| alert detail | 43 ms | 36 ms |
| customer transactions, 90 days | 2.9 ms | 11.6 ms |
| graph: counterparties degree 2 / neighbourhood depth 2 | 0.4 / 1.7 ms | 0.5 / 1.1 ms |
| full case-workbench assembly | 262 ms | 581 ms |
| money-mule assessment per customer (p50 / p95) | 116 / 205 ms | 151 / 254 ms |
| money-mule flow subgraph, depth 2 | 0.9 ms | 3.2 ms |
| data-quality summary | 0.3 ms | 13.5 ms |
| alert-quality metrics | 20.7 ms | 23.3 ms |
| My Work | 0.5 ms | 17.5 ms |
| triage re-score of **all** unresolved alerts | 103 alerts in 71 ms | **103 alerts in 5.9 s** |

Reading it: the mule view costs about as much as one risk assessment because it reads the same transactions. Triage re-scoring on
PostgreSQL is slow (about 57 ms per alert: one history query and one transaction each); it is an admin batch operation, and it
scales linearly with open alerts. The PostgreSQL `kpis` query had one 286 ms outlier at 101k (the other sizes were 9 ms): a single
slow sample, not investigated further. Alert quality and KPIs read every alert, so they grow with alert count (unmeasured beyond
a few hundred alerts here).

## Memory

Resident memory of the benchmark process after the run: 240 MB (10k), 386 MB (101k), 621 MB (251k) in memory mode, with peak working
sets of 243, 414 and 677 MB; with PostgreSQL 263, 358 and 552 MB (peaks 265, 465, 806 MB). Growth is roughly linear in
transactions held in memory.

## Index review (PostgreSQL, 300,000 synthetic alerts, 100,000 cases)

Only indexes that a real application query uses were added, and each was measured alone against the schema without it
(median of 7 `EXPLAIN (ANALYZE)` runs; synthetic skew: 63% resolved alerts, open ones mostly assigned across 20 users, 92% closed cases).

| Query the application issues | Before | With the shipped indexes | Index used |
|---|---|---|---|
| my open alerts by triage score | 10.0 ms | 1.05 ms | `ix_malerts_assignee_triage` |
| queue top-50 by triage score | 60.5 ms | 0.16 ms | `ix_malerts_open_triage` |
| top-50 CRITICAL/HIGH by triage score | 61.0 ms | 0.16 ms | `ix_malerts_open_triage` |
| recently resolved (7 days) | 118.6 ms | 0.57 ms | `ix_malerts_resolved_at` |
| my open cases | 0.60 ms | 0.04 ms | `ix_cases_assigned_status` |

What the review changed:

* `NULLS LAST` is part of the index definition: without it the planner does not use the index for the repository's `ORDER BY`.
* A **non-partial** triage index (`ix_malerts_open_triage_all`) was 14.3 MB against 2.4 MB for the partial one and gave no extra
  benefit, so it was not shipped.
* `ix_malerts_open_triage` *alone* made "my open alerts" **slower** (11.9 → 23.5 ms) because the planner walked it in triage order;
  with `ix_malerts_assignee_triage` present the planner chooses the right index. The shipped set was re-measured together
  (table above) to confirm.
* `ix_cases_assigned_status (assigned_to, status)` makes the single-column `ix_cases_assigned` redundant; migration 0004 drops it
  (downgrade recreates it). Absolute case timings are tiny either way.
* Write cost of the four extra indexes was **not** measured.

## Regression baseline

`docs/benchmarks/perf_baseline.json` is this run. `python -m app.monitoring.benchmark --sizes 10000 --compare docs/benchmarks/perf_baseline.json --tolerance 0.5`
re-runs and exits 1 if a tracked metric (risk scoring p50, daily-batch ms per customer, batch-ingestion rows/s, gate rows/s with
rejections, alert-queue p95, workbench p95, mule-assessment p95) is worse than baseline by more than the tolerance. A baseline is only meaningful on the **same machine**: CI
runs it as an advisory job with a 4x tolerance because shared runners are noisy and differ from this machine. Re-baseline
deliberately (commit a new file) when a change is expected to move performance.

## Not measured

1,000,000 transactions; concurrent users or concurrent monitoring runs; behaviour while a backup runs; write amplification from
the new indexes; API latency over the network (the figures are in-process calls); PostgreSQL tuning (defaults); Neo4j; any
production hardware.

## Reproduce

```bash
cd backend
python -m app.monitoring.benchmark --sizes 10000 100000 250000 \
    --database-url postgresql+psycopg://USER:PASSWORD@HOST:5432/postgres --out ../docs/benchmarks/perf_v3
python ../infrastructure/scripts/index_review.py --url postgresql+psycopg://USER:PASSWORD@HOST:5432/postgres \
    --alerts 300000 --cases 100000 --out ../docs/benchmarks/index_review
```

The database URL must point at a server where a scratch database can be created (it is dropped afterwards). Run on an otherwise
idle machine; the 250k run takes about 25 minutes here.
