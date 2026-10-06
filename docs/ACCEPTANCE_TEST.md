# Final acceptance test (2026-10-03)

One end-to-end pass over the whole production-oriented workflow, on **synthetic data**, against a real API process backed by a
real PostgreSQL 15 (Docker, throwaway password) on the development machine. It is evidence that the pieces work together here. It
is not a certification, not an operational readiness review and not a statement about real-world behaviour.

Part A is a script (`infrastructure/scripts/acceptance_test.py`) that drives the live API over HTTP and asserts every claim; its raw
report is [`acceptance/acceptance_run_2026-10-03.md`](acceptance/acceptance_run_2026-10-03.md) (with `.json`). Part B lists the
steps that are too long or too different for that script and how each was run.

## Setup

```bash
# a fresh synthetic bank with the money-mule family and benign look-alikes (1,500 customers, 40,296 transactions, seed 31)
python -m app.synthetic.monitoring_benchmark --out <dir> --customers 1500 --seed 31
# a new database, then migrations + data load + document index
FIRA_ENV=development DATA_BACKEND=postgres DATABASE_URL=... DATASET_DIR=<dir> ... python -m app.db.bootstrap
uvicorn app.main:app --port 8765
python infrastructure/scripts/acceptance_test.py --url http://localhost:8765 --dataset <dir> --admin-password ... \
    --analyst-password ... --db-url postgresql://...@localhost:55432/fira_accept --docker-container fira-test-pg --out ...
```

The harness reads the dataset's `scenario_labels.json` **only to choose subjects** (a mule, a benign look-alike, normal customers)
and to confirm outcomes; product code never reads labels (`tests/unit/test_label_leakage.py`).

## Part A. Scripted journey (15 of 15 steps passed)

| # | Requested step | What the script did and checked | Result |
|---|---|---|---|
| 1 | generate / load synthetic data | API ready; schema revision 0004 current; the loaded dataset registered as a baseline batch whose *expected* count comes from the generator manifest (40,296 expected = 40,296 received, coverage 100%) | PASS |
| 2 | validate data | **data-quality failure demo**: a 19-row batch with a duplicate id, an unparseable timestamp, a negative amount, an unknown account, an unknown field, a missing field and a row 20 days behind. Accounting `19 = 13 + 6 + 0`; the six rejections carry six different reason codes and are drillable by batch and code; 1 row counted late; with `expected: 25` the batch reports 6 missing; overall coverage 99.985%, quality score 99.98 | PASS |
| 3 | monitor, alert | 20 customers screened (mules, benign look-alikes, structuring, normal), 28 alerts created, 0 errors; every alert has 12 triage factors summing to its score (±0.1) and a priority consistent with the thresholds; queue sorted by triage score. 4 CRITICAL, 18 HIGH, 6 MEDIUM | PASS |
| 4 | risk and triage score | the factor breakdown for the top alert (FAN_OUT, 67.8, CRITICAL) with the method statement "not a probability" | PASS |
| 5 | **money-mule scenario** | `CUST-11490`: band HIGH, score 66.8; indicators fan-in, fan-out, rapid movement, low retention, flagged network; inbound USD 11,930, outbound 10,201; flow graph with 60 accounts and 61 transfers (amounts, times, direction, depth); the account appears in the fan-in pattern search; disclaimer "not proof" present | PASS |
| 6 | **false-positive scenario** | a benign family-collection customer (`CUST-11325`) alerted; its mule band was LOW; the analyst assigned and resolved it `FALSE_POSITIVE` with a reason; the feedback record shows decision, investigator, reason and timestamp | PASS |
| 7 | assign | assigned to the analyst; My Work lists it ordered by triage score; counts consistent; a backward transition and an assignment to someone else were refused (409/403) | PASS |
| 8 | case, investigate customer / transactions / graph, evidence, note | case `FC-2026-000001` created; workbench loaded; customer profile, 49 transactions, 99 counterparties (degree 2); a real transaction attached as evidence; an unknown transaction refused; note added; priority raised with a reason; OPEN → PENDING_REVIEW refused (409) | PASS |
| 9 | decision, close | `CONFIRMED_SUSPICIOUS` with a reason closed the case and resolved its alert; the timeline (11 events from case, alert and note sources) is in real-timestamp order; a closed case refuses further transitions | PASS |
| 10 | audit | 91 audit rows; contains login, monitoring run, ingestion, assignment, case creation, priority change, decision, closure, mule assessment; PUT/PATCH/DELETE on the audit endpoint refused; an analyst sees only their own trail | PASS |
| 11 | dashboard / operations | decided alerts 2, confirmed 1, false positive 1; confirmation and false-discovery rates sum to 1; the response says FPR and recall are *not computed*; the KPI no longer carries a false-positive rate; business gauges present in `/metrics` (`fira_alerts_open`, `fira_cases_open`, `fira_ingest_rows`, `fira_data_quality_score`, `fira_db_ping_ms`) | PASS |
| (12) | configuration governance | a detector switch and a threshold change recorded with who, why, old (50.0) and new (52.0) value and source; an out-of-order threshold refused (422); an analyst refused (403); both restored | PASS |
| 13 | security tests (behaviour) | 401 without token; 403 for an analyst on admin endpoints and `/metrics`; an oversized request refused; the 6th failed login for a username locked (429); logout revokes the token; security headers present | PASS |
| 14 | backup | encrypted `pg_dump` archive; checksum and `pg_restore --list` verified; **restore test** into a scratch database: all 13 row counts equal the manifest, revision matches, all 4 append-only triggers present | PASS |
| 15 | restore in a safe environment, verify the restored app | restored into a named database; the application started against it: ready, schema 0004 current, alert/case/batch counts equal the live ones, `UPDATE audit_log` refused, new writes accepted | PASS |

## Part B. Steps run separately

| Requested step | How | Result |
|---|---|---|
| evaluation | `python -m app.evaluation.monitoring_eval` on a 4,000-customer bank, holdout seed 7 and development seed 2025 | done; [EVALUATION.md](EVALUATION.md) Part 1b. Precision 0.838 / recall 0.829 / FPR 0.0073 (holdout); triage priority monotonic in oracle-confirmed rate; mule AUC 0.971 but band cut-offs weaker than the existing alerts |
| performance benchmark | `python -m app.monitoring.benchmark --sizes 10000 100000 250000` with PostgreSQL, plus the index review | done; [PERFORMANCE.md](PERFORMANCE.md). 1,000,000 transactions **not run** (memory) |
| security tests | `tests/unit/test_security_hardening.py`, `test_governance.py`, `tests/integration/test_least_privilege_postgres.py` | passed; see the counts below |
| CI checks (local equivalents) | `ruff`, `mypy` (105 files), `bandit -ll`, `pip-audit`, `npm ci`/`typecheck`/`test`/`build`/`audit` | all clean; **GitHub Actions itself has not run** |
| failure testing | `tests/unit/test_failure_modes.py`, `tests/integration/test_failure_modes_postgres.py` (database unreachable, connections killed, restart, schema behind, bad migration, Qdrant down, invalid config, isolated failures) | passed; it found the 136 s start-up hang, now fixed |
| unit / integration / frontend suites | see [README Testing](../README.md#testing) | see below |

## Defects found by this process

The acceptance and failure testing found three real defects in the product (all fixed, each with a test) beyond harness mistakes:

1. start-up against an unreachable database blocked for 136 s (no connect timeout);
2. the `fira_cases_open` gauge never appeared because it read a KPI key that does not exist (the earlier unit test checked other gauges);
3. a v2 dashboard KPI named "false-positive rate" was a share of resolved alerts, not an FPR.

Harness mistakes fixed along the way: case creation returns 201 (expected 200), a log line preceded the JSON in one script's output,
and a client may see a connection reset instead of a 413 when the server closes the socket on an oversized upload.

## What this does not show

Behaviour with real data or real investigators; deployment on Render or via Docker Compose; GitHub Actions; Neo4j and Qdrant in this
pass; load or concurrency; restore time at scale (the restore step took about 21 s for a 15,000-transaction database); that any RPO
or RTO target is met; a security review by anyone else.
