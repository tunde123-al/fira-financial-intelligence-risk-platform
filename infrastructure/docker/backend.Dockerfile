# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/fira/backend
COPY backend/requirements.txt ./
ARG WITH_DOCLING=false
COPY backend/requirements-docling.txt ./
RUN pip install -r requirements.txt && if [ "$WITH_DOCLING" = "true" ]; then pip install -r requirements-docling.txt; fi
COPY backend/ ./
COPY documents/ /srv/fira/documents/
COPY evaluation/ /srv/fira/evaluation/
RUN useradd --create-home --uid 10001 fira && mkdir -p /srv/fira/data && chown -R fira /srv/fira
USER fira
ENV DATASET_DIR=/srv/fira/data/seeds MODEL_DIR=/srv/fira/data/models DOCUMENTS_DIR=/srv/fira/documents
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=5 CMD curl -fs http://localhost:8000/health || exit 1
CMD ["sh", "-c", "python -m app.db.bootstrap && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-1} --proxy-headers"]
