# Data pipeline

How data gets into FIRA and what happens to it. All data is **synthetic**.

## Current implementation

```text
generator (seeded) --> data/seeds/*.csv.gz + manifest.json + scenario_labels.json
        |                                   (labels are read only by the evaluator)
        v
prepare_frames (types, hashing of identifiers, masking)  --> loader: COPY ... FROM STDIN  --> PostgreSQL tables
                                                                                               |
API batches: POST /api/monitoring/transactions --> ingestion gate --> accepted rows --> transactions
                                                        |                                    |
                                                        +--> rejected rows --> quarantine    +--> graph projection rebuilt
                                                        +--> batch ledger (accounting)
PostgreSQL --> risk engine (windows, baselines, peer stats) --> alerts, triage --> cases / investigations
PostgreSQL --> graph projection (NetworkX) --> clusters, flows, mule indicators
PostgreSQL --> dataset audit (docs/data-quality.md)
```

| Stage | What it does | Code |
|---|---|---|
| Generate | seeded bank: customers (individual/business, segments, cities), accounts, devices, merchants, ~25 transactions per customer, injected scenarios (mule, structuring, fan-out, circular, device-sharing ring, takeover, dormant, ...) and look-alikes; deterministic per seed | `app/synthetic/generator.py`, `monitoring_benchmark.py` |
| Load | gzip CSV -> typed frames -> `COPY` into PostgreSQL (about 7,600-10,200 transactions/s measured); identifiers stored as salted hashes plus masked display values | `app/db/loader.py` |
| Migrate | Alembic 0001-0004 apply SQL files; every migration transactional and repeatable | `backend/alembic/`, `app/db/*.sql` |
| Ingest (API) | rows validated one by one: 22 reason codes (malformed / duplicate / invalid / referential), late and missing accounting, near-duplicate detection, IP masking in quarantine | `app/monitoring/ingest.py` |
| Model | normalised relational model with CHECK constraints, foreign keys, partial unique indexes and append-only triggers | `app/db/schema*.sql`, `docs/DATA_MODEL.md` |
| Transform | per-customer windows, baselines, peer statistics, activity windows, counterparties computed in SQL/pandas at assessment time (ELT style: raw tables in place, derived views at read time) | `app/risk/`, `app/monitoring/activity.py` |
| Serve | FastAPI endpoints and the React UI | `app/api/` |

**Why "ELT"-style:** the data is stored once in its loaded form and every analytic (score, graph, quality audit) is derived from it on
demand, so a changed configuration changes results without re-loading. That keeps results reproducible and auditable; it trades
speed for simplicity (see PERFORMANCE.md for measured costs).

## Reproducing the data

```bash
cd backend
python -m app.synthetic.generator --out ../data/seeds --customers 10000 --seed 42
DATABASE_URL=postgresql://... python -m app.db.bootstrap      # migrations + load + document index (idempotent)
```

Start-up never regenerates or reloads a database that already holds data (the database decides, not the local disk).

## Production-scale future architecture (NOT implemented)

Streaming ingestion (Kafka), distributed transforms (Spark/Databricks) with tested models (dbt), an orchestrator (Airflow/Dagster),
partitioned storage, incremental graph updates and a managed warehouse. None of these exist here; the current pipeline is a single
process plus PostgreSQL, measured up to about 250,000 transactions on one small machine.
