# syntax=docker/dockerfile:1
# Single-image build for a public synthetic-data demo (API + built UI in one web service).
# The API serves the UI at "/" when FRONTEND_DIST_DIR is set. Neo4j, Qdrant and LLMs are NOT needed.
FROM node:20-alpine AS ui
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/fira/backend
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt
COPY backend/ ./
COPY documents/ /srv/fira/documents/
COPY evaluation/ /srv/fira/evaluation/
COPY --from=ui /app/dist /srv/fira/frontend-dist
RUN useradd --create-home --uid 10001 fira && mkdir -p /srv/fira/data && chown -R fira /srv/fira
USER fira
ENV FRONTEND_DIST_DIR=/srv/fira/frontend-dist \
    DATASET_DIR=/srv/fira/data/seeds MODEL_DIR=/srv/fira/data/models DOCUMENTS_DIR=/srv/fira/documents
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=240s --retries=5 \
  CMD curl -fs http://localhost:${PORT:-8000}/health || exit 1
# bootstrap is idempotent: migrations, dataset generation/load (SEED_CUSTOMERS), document indexing
CMD ["sh", "-c", "python -m app.db.bootstrap && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${UVICORN_WORKERS:-1} --proxy-headers --forwarded-allow-ips='*'"]
