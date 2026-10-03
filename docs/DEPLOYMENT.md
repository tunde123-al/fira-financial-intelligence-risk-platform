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

## CI/CD (GitHub Actions — `.github/workflows/ci.yml`)

```
lint-typecheck (ruff, mypy, tsc) ─┐
unit-tests (unit, agent, e2e)  ───┤
integration-tests (Postgres+PostGIS, Neo4j, Qdrant services; alembic; API, MCP, LangGraph parity)
security (bandit, pip-audit, secret scan) ─┘
        └─▶ build (both images) ─▶ publish (main only: push to GHCR)
```

Nothing is built or published unless every check passes.

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
