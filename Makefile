# FIRA developer commands. Backend commands run from backend/.
PY ?= python3
BACKEND = cd backend &&
FRAMES = DATA_BACKEND=frames DATASET_DIR=../data/seeds MODEL_DIR=../data/models

.PHONY: help up down logs generate ingest train-ml evaluate evaluate-local test test-unit test-integration lint typecheck api-local frontend-dev mcp sample-docs clean

help:          ## show targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

up:            ## start the full stack (postgres, neo4j, qdrant, backend, frontend)
	docker compose up --build -d && echo "UI: http://localhost:8080  API docs: http://localhost:8000/docs"
down:          ## stop the stack
	docker compose down
logs:          ## follow backend logs
	docker compose logs -f backend

generate:      ## generate the synthetic dataset into data/seeds
	$(BACKEND) $(PY) -m app.synthetic.generator --out ../data/seeds --customers 10000
ingest:        ## ingest documents (local, frames backend)
	$(BACKEND) $(FRAMES) $(PY) -m app.documents.pipeline ingest
train-ml:      ## train the isolation-forest anomaly model (local)
	$(BACKEND) $(FRAMES) $(PY) -m app.risk.ml train --sample 2000
evaluate-local: ## run all benchmarks in-process (no infrastructure needed)
	$(BACKEND) $(FRAMES) $(PY) -m app.evaluation.runner all --per-scenario 25 --agent-sample 3
evaluate:      ## run all benchmarks inside the running backend container
	docker compose exec backend python -m app.evaluation.runner all

api-local:     ## run the API without docker on the in-process stack
	$(BACKEND) $(FRAMES) FIRA_ENV=development uvicorn app.main:app --reload --port 8000
frontend-dev:  ## run the Vite dev server (proxies /api to :8000)
	cd frontend && npm install && npm run dev
mcp:           ## run the MCP server (stdio)
	$(BACKEND) $(PY) -m app.mcp.server

test:          ## all tests (integration tests skip without services)
	$(BACKEND) $(PY) -m pytest -q
test-unit:
	$(BACKEND) $(PY) -m pytest -q tests/unit tests/agent tests/e2e
test-integration:
	$(BACKEND) $(PY) -m pytest -q tests/integration
lint:
	$(BACKEND) ruff check app tests
typecheck:
	$(BACKEND) mypy app && cd frontend && npm run typecheck
sample-docs:   ## regenerate the PDF/DOCX/scanned sample documents
	$(PY) infrastructure/scripts/make_sample_documents.py
clean:
	rm -rf data/models backend/.pytest_cache
