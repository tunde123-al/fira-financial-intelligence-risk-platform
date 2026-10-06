"""Idempotent first-start bootstrap used by the container entrypoint.

    python -m app.db.bootstrap

1. wait for PostgreSQL, apply migrations (alembic upgrade head)
2. if the database is EMPTY: generate the synthetic dataset if its files are missing (DATASET_DIR), then load it;
   a populated database is never regenerated or reloaded and needs no dataset files
4. project the graph into Neo4j if GRAPH_BACKEND=neo4j and Neo4j is empty
5. ingest the document corpus if the vector store is empty
6. train the ML anomaly model if it is missing (optional, TRAIN_ML_ON_START=true)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from app.config import BACKEND_ROOT, get_settings


def log(msg: str, **kw: object) -> None:
    print(json.dumps({"bootstrap": msg, **kw}, default=str), flush=True)


def wait_for(fn, what: str, attempts: int = 60, delay: float = 2.0) -> None:
    for i in range(attempts):
        try:
            if fn():
                return
        except Exception as e:  # service still starting
            if i == attempts - 1:
                raise RuntimeError(f"{what} not reachable: {type(e).__name__}") from e
        time.sleep(delay)
    raise RuntimeError(f"{what} not ready")


def database_is_seeded(engine) -> bool:
    """The database, not the local disk, says whether the dataset has been loaded."""
    from sqlalchemy import text

    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT count(*) FROM transactions")).scalar())


def seed_if_empty(engine, s, generate=None, load=None) -> str:
    """Generate (only if the files are missing) and load the synthetic dataset, but only into an EMPTY database.

    A populated database is left alone and no dataset files are generated: on hosts with an ephemeral disk the
    files vanish on every restart while PostgreSQL keeps the data, so regenerating them would be pure waste.
    Returns "loaded" or "already_loaded".
    """
    if database_is_seeded(engine):
        log("database already loaded; dataset files not needed")
        return "already_loaded"
    if generate is None:
        from app.synthetic.generator import generate_dataset as generate
    if load is None:
        from app.db.loader import load
    if not (Path(s.dataset_dir) / "manifest.json").exists():
        n = int(os.environ.get("SEED_CUSTOMERS", "10000"))
        log("generating synthetic dataset", customers=n)
        generate(Path(s.dataset_dir), n_customers=n)
    log("loading dataset into PostgreSQL", counts=load(s.database_url, Path(s.dataset_dir)))
    return "loaded"


def main() -> None:
    s = get_settings()
    if s.data_backend != "postgres":
        log("frames backend: nothing to bootstrap")
        return
    from sqlalchemy import create_engine, text

    engine = create_engine(s.database_url)  # type: ignore[arg-type]
    wait_for(lambda: engine.connect().execute(text("SELECT 1")).scalar() == 1, "PostgreSQL")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND_ROOT, check=True)
    log("migrations applied")

    seed_if_empty(engine, s)

    from app.services.container import build_container

    c = build_container(s, load_ml=False)
    if s.graph_backend == "neo4j":
        wait_for(c.graph.ping, "Neo4j")
        if c.graph.stats()["nodes"] == 0:
            log("projecting graph into Neo4j", counts=c.graph.load_projection(c.store.graph_edges(None, None)))
        else:
            log("graph already projected")
    if s.vector_backend == "qdrant":
        wait_for(c.vectors.ping, "Qdrant")
    if c.vectors.count() == 0 or not c.doc_repo.documents():
        from app.documents.pipeline import discover, investigation_notes_document

        notes = investigation_notes_document(c.store.list_investigations(status="closed", limit=5000))
        rep = c.ingestion.ingest_paths(discover(s.documents_dir), root=s.documents_dir, extra_documents=[notes])
        log("documents ingested", index=rep["index"], errors=rep["errors"], warnings=rep["warnings"])
    else:
        log("documents already indexed", chunks=c.vectors.count())

    if os.environ.get("TRAIN_ML_ON_START", "false").lower() == "true" and not (Path(s.model_dir) / "ml_anomaly.joblib").exists():
        import numpy as np

        from app.risk.ml import train

        ids = c.list_customer_ids()
        sample = list(np.random.default_rng(7).choice(ids, size=min(2000, len(ids)), replace=False))
        path = train(c.risk_engine, sample).save(Path(s.model_dir))
        log("ML anomaly model trained", path=str(path))
    log("bootstrap complete")


if __name__ == "__main__":
    main()
