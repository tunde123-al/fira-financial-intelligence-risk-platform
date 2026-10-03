"""Shared test fixtures (unittest-compatible; pytest collects these TestCases too).

A small synthetic dataset (600 customers) is generated once per test process into
a temporary directory, and documents are ingested into a temporary model dir, so
tests never touch developer data.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from app.config import BACKEND_ROOT, REPO_ROOT, Settings

_LOCK = threading.RLock()
_CACHE: dict[str, Any] = {}


def small_dataset() -> Path:
    with _LOCK:
        if "dataset" not in _CACHE:
            env = os.environ.get("FIRA_TEST_DATASET")
            if env and (Path(env) / "manifest.json").exists():
                _CACHE["dataset"] = Path(env)
            else:
                from app.synthetic.generator import generate_dataset

                d = Path(tempfile.mkdtemp(prefix="fira-test-data-"))
                generate_dataset(d, n_customers=600, seed=123, history_days=180)
                _CACHE["dataset"] = d
        return _CACHE["dataset"]


def labels() -> list[dict[str, Any]]:
    return json.loads((small_dataset() / "scenario_labels.json").read_text(encoding="utf-8"))


def first_of(scenario: str) -> str:
    return next(lb["entity_id"] for lb in labels() if lb["scenario"] == scenario)


def make_settings(**over: Any) -> Settings:
    model_dir = _CACHE.setdefault("model_dir", Path(tempfile.mkdtemp(prefix="fira-test-models-")))
    base = dict(environment="test", data_backend="frames", dataset_dir=small_dataset(), graph_backend="networkx",
                vector_backend="memory", documents_dir=REPO_ROOT / "documents", model_dir=model_dir,
                risk_config_path=BACKEND_ROOT / "app" / "risk" / "default_config.yaml", llm_provider="none",
                jwt_secret="test-secret-test-secret-test-secret-123", pii_masking=True, tool_timeout_s=30.0)
    base.update(over)
    return Settings(**base)


def container(fresh: bool = False, **kw: Any) -> Any:
    """A fully wired container on the small dataset with the policy corpus ingested."""
    from app.documents.pipeline import discover, investigation_notes_document
    from app.services.container import build_container

    with _LOCK:
        if fresh or "container" not in _CACHE or kw:
            s = make_settings()
            c = build_container(s, load_ml=False, **kw)
            if not (Path(s.model_dir) / "lsa_embedder.joblib").exists() or "ingested" not in _CACHE:
                notes = investigation_notes_document(c.store.list_investigations(status="closed", limit=5000))
                c.ingestion.ingest_paths(discover(s.documents_dir), root=s.documents_dir, extra_documents=[notes])
                _CACHE["ingested"] = True
            c.refresh_retriever()
            if fresh or kw:
                return c
            _CACHE["container"] = c
        return _CACHE["container"]
