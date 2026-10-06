# Deployment

## Local (Docker Compose)

```bash
cp .env.example .env      # set POSTGRES_PASSWORD, NEO4J_PASSWORD, JWT_SECRET, BOOTSTRAP_* passwords
docker compose up --build
```

| Service | Image | Port (localhost only) | Memory limit |
|---|---|---|---|
| postgres | postgis/postgis:16-3.4 | 5432 | 1 GB |
| neo4j | neo4j:5.24-community | 7474 (browser), 7687 | 1.5 GB |
| qdrant | qdrant/qdrant:v1.12.4 | 6333 | 512 MB |
| backend | `infrastructure/docker/backend.Dockerfile` (python:3.12-slim + tesseract) | 8000 | 2 GB |
| frontend | node build → nginx:1.27-alpine | 8080 | — |

On start the backend runs `python -m app.db.bootstrap`, which is idempotent: it waits for
PostgreSQL, applies Alembic migrations, generates the dataset if missing (`SEED_CUSTOMERS`), loads
it if the database is empty, projects Neo4j if it is empty, ingests documents if the index is empty,
and optionally trains the ML model (`TRAIN_ML_ON_START=true`). It then starts uvicorn. Data lives
in named volumes. `docker compose down -v` resets everything.

Optional settings:

- Docling: `docker compose build --build-arg WITH_DOCLING=true backend`, then set `DOCUMENT_PARSER=docling`.
- LLM: set `LLM_PROVIDER`, `LLM_MODEL` and `LLM_API_KEY` in `.env`. For a local model, run Ollama
  and set `LLM_PROVIDER=ollama LLM_BASE_URL=http://host.docker.internal:11434`.

Health endpoints: `GET /health` (liveness) and `GET /health/ready` (database, graph, vector store,
indexed chunks, source documents, LLM provider, agent engine; returns 503 when degraded). Prometheus metrics are at
`GET /metrics` (admin token). Logs are JSON on stdout, with `request_id`, `user_id`,
`investigation_id` and `agent_run_id` on every line.

## Environments: development, test, production

One codebase, one setting (`FIRA_ENV`), three behaviours. Nothing about an environment is inferred from the host.

| | development (default) | test | production |
|---|---|---|---|
| purpose | local work, demos | automated tests, CI | a deployment that is exposed to other people |
| data store | PostgreSQL, or the in-memory frames store | frames store or throwaway PostgreSQL databases | **PostgreSQL only**: the process refuses to start on the in-memory store or without `DATABASE_URL` |
| `JWT_SECRET` | random per process if unset | set by tests | **required**, at least 32 characters, not a well-known word |
| bootstrap passwords | optional, 10+ characters | test values | 12+ characters, not well-known |
| API docs (`/docs`, `/redoc`, `/openapi.json`) | on | on | **off** unless `ENABLE_API_DOCS=true`, which the production check then reports |
| `Strict-Transport-Security` | not sent | not sent | sent (TLS must terminate in front of the app) |
| CORS | localhost origins by default | n/a | explicit origins only; `*` refused |
| `LOG_LEVEL=DEBUG` | allowed | allowed | refused |
| debug / auto-reload | none of the images run `uvicorn --reload`; there is no debug flag | | |
| secrets | `.env` (git-ignored) | generated in tests | the platform's secret store; never in the repository |

The checks live in `app/security/hardening.py::production_problems` and run before the app starts, so a misconfigured production
deployment fails immediately and says which variable is wrong (never its value). Separate environments should use **separate
databases and separate secrets**; the same `JWT_SECRET` must not be shared between them.

Database roles: run migrations (`alembic upgrade head`) with the owner role and run the API with the restricted role from
`infrastructure/sql/least_privilege_role.sql` (no DDL, no `TRUNCATE`, no changes to the append-only tables). The role was
exercised end to end in `tests/integration/test_least_privilege_postgres.py`; the Render blueprint still uses the single
connection string Render provides, so it runs as the owner (a known gap for that demo).

## Operations

* **Readiness** `GET /health/ready` also reports `database_latency_ms`, `schema_revision` and `schema_current`; a database that is
  behind the code (a missing migration) makes the service `503 degraded`. `GET /health` is liveness only.
* **Metrics** `GET /metrics` (admin JWT, or `X-Metrics-Token` when `METRICS_TOKEN` is set): HTTP counters and latency sums,
  login failures and lockouts, ingestion rows by outcome, triage re-scores, mule assessments, plus gauges computed at scrape time
  from real data: `fira_alerts_open{priority=…}`, `fira_alerts_total`, `fira_alerts_decided_total`, `fira_alerts_confirmed_total`,
  `fira_cases_open`, `fira_last_monitoring_run_duration_ms`, `fira_last_monitoring_run_age_seconds`,
  `fira_ingest_rows{kind=…}`, `fira_data_quality_score`, `fira_ingest_processing_success`, `fira_db_ping_ms`.
* **Logs** are one JSON object per line on stdout with `service` (`fira-api`), `env`, `logger`, `msg`, the request id, user id and,
  for requests, `operation` (`METHOD route`), `duration_ms`, `status`. Errors that reach a client carry the request id so a report
  can be matched to a log line.
* **Backups and recovery**: [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md). A free database tier has no backups you can rely on.

## CI/CD (GitHub Actions — `.github/workflows/ci.yml`)

```
lint-typecheck (ruff, mypy, tsc, Vitest, Vite build, npm audit) ─┐
unit-tests (unit, agent, e2e; coverage floor 75%)  ──────────────┤
integration-tests (Postgres+PostGIS, Neo4j, Qdrant services; alembic upgrade → downgrade 0003 → upgrade;
                   API, MCP, LangGraph parity; backup → verify → restore test)
security (bandit, pip-audit, secret scan) ─────────────────────────┘
        └─▶ build (both images) ─▶ publish (main only: push to GHCR)
performance-smoke (advisory, continue-on-error): 10,000-transaction benchmark vs docs/benchmarks/perf_baseline.json
```

Nothing is built or published unless every blocking check passes. The performance job is advisory because shared CI runners are
noisy; it uses a very wide tolerance (4x) and uploads its report as an artifact. The coverage floor is deliberately just under
the measured figure (see [TESTING in the README](../README.md#tests)); raise it when coverage rises.

## Public demo on Render (synthetic data only)

Target: `GitHub → CI → Render → FastAPI (serving the built React UI) → PostgreSQL`. One web service and one
database; **no Neo4j, Qdrant, Kafka, GPU or LLM**. Optional pieces stay behind configuration
(`GRAPH_BACKEND=networkx`, `VECTOR_BACKEND=memory`, `LLM_PROVIDER=none`).

**Status: NOT DEPLOYED. The blueprint and image are written but have not been run on Render.** What *was*
verified locally: the combined behaviour of the pieces they rely on (API serving a static UI with API routes
and `/docs` taking precedence; `postgres://` URL normalisation; bootstrap, migrations 0001 to 0003, data load
and the monitoring workflow against PostgreSQL 15; migration 0004 up/down/up, the backup and restore scripts, the least-privilege role and the failure tests also ran against a local PostgreSQL 15 container). The Docker image build itself
(`infrastructure/docker/render.Dockerfile`) has not been built in this repository's verification.

Files: [`render.yaml`](../render.yaml) (blueprint), [`infrastructure/docker/render.Dockerfile`](../infrastructure/docker/render.Dockerfile).

Steps:

1. Push the repository to GitHub and let CI pass.
2. In Render choose *New + > Blueprint* and select the repository. Provide `BOOTSTRAP_ADMIN_PASSWORD` and
   `BOOTSTRAP_ANALYST_PASSWORD` when prompted (at least 12 characters because the blueprint sets `FIRA_ENV=production`, not reused anywhere). `JWT_SECRET` is
   generated. Never commit these values.
3. On first start the container applies the migrations, generates `SEED_CUSTOMERS` (2,000 in the blueprint)
   synthetic customers, loads them into PostgreSQL and indexes the documents. This takes a few minutes; the
   health check allows for it.
4. Sign in as `admin`, open *Monitoring* and run monitoring (for example lookback 30, active in the last 30
   days). Alerts and cases are then available to the analyst. In a Render shell the equivalent is
   `python -m app.monitoring.run --active-days 30`.

Notes and caveats:

- The free Render PostgreSQL plan expires and a small web instance may run out of memory with pandas and
  scikit-learn loaded; the blueprint therefore names the `starter` web plan. These sizes are a judgement, not a
  measurement.
- The default dataset has 10,000 customers; the demo uses a smaller one, so customer ids differ from
  `docs/DEMO.md` (which uses the default seed). Find an interesting subject from the alert queue instead.
- Before exposing it: choose strong passwords, keep `PII_MASKING=true`, keep the rate limit, and remember that
  every analyst can read every case (no per-case access control). `/docs` is off in production unless `ENABLE_API_DOCS=true`. The data is
  synthetic; do not load real data into this deployment.
- The scanned-PDF OCR path needs Tesseract (installed in the image).

## AWS reference deployment

No Kubernetes is needed at this scale:

| Component | AWS service |
|---|---|
| backend, frontend | ECS Fargate services behind an ALB (TLS via ACM); images from ECR/GHCR |
| PostgreSQL + PostGIS | RDS for PostgreSQL 16 (PostGIS is supported); Multi-AZ, KMS encryption, automated backups |
| Neo4j | Neo4j AuraDB, or a single EC2 instance with EBS (Community edition has no clustering) |
| Qdrant | Qdrant Cloud, or ECS with an EFS/EBS volume |
| secrets | AWS Secrets Manager → ECS task environment (`JWT_SECRET`, DB credentials, `LLM_API_KEY`) |
| logs and metrics | CloudWatch Logs (JSON); scrape `/metrics` with Amazon Managed Prometheus or the CloudWatch agent |
| schedules | EventBridge Scheduler → ECS tasks for `app.evaluation.runner`, the graph reload and retention jobs |
| network | private subnets for data stores, security groups allowing only backend → stores, WAF on the ALB |

Run migrations as a one-off ECS task (`alembic upgrade head`) before a new backend revision
receives traffic. Bootstrap is idempotent and skips seeding when data exists. **Disable synthetic
seeding in real environments** by providing the dataset or loader.

## Sizing notes

The in-process risk assessment takes about 100 ms per customer on the 254k-transaction dataset. A
full agent run takes about 0.5 s without an LLM and adds roughly 5–30 s with one (provider
dependent). Agent runs are synchronous HTTP calls, so use several uvicorn workers
(`UVICORN_WORKERS`) or move runs to a job queue (see the roadmap) for concurrent analysts.
