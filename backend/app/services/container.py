"""Service container: builds and wires every component from Settings.

One place decides which implementation backs each port (store, graph, vector
store, embedder, LLM), so the rest of the code depends only on interfaces.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.data.store import DataStore
from app.risk.config import RiskConfig, load_risk_config

log = logging.getLogger("fira.container")


def build_store(settings: Settings) -> DataStore:
    if settings.data_backend == "frames":
        from app.data.frame_store import FrameStore

        return FrameStore(settings.dataset_dir)
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required when DATA_BACKEND=postgres")
    from app.data.sql_store import SqlStore

    return SqlStore(settings.database_url)


def active_risk_config(settings: Settings, store: DataStore) -> RiskConfig:
    """The approved active configuration if one exists, otherwise the shipped defaults."""
    try:
        for rec in store.list_config_versions():
            if rec.get("status") == "active":
                return RiskConfig(**rec["config"])
    except Exception as e:  # table may not exist yet on first boot
        log.warning("could not read config versions: %s", type(e).__name__)
    return load_risk_config(settings.risk_config_path)


@dataclass
class Container:
    settings: Settings
    store: DataStore
    graph: Any
    risk_config: RiskConfig
    risk_engine: Any
    doc_repo: Any
    vectors: Any
    embedder: Any
    retriever: Any
    ingestion: Any
    llm: Any
    registry: Any
    agent: Any
    ml_model: Any = None
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def list_customer_ids(self) -> list[str]:
        return self.store.list_customer_ids()

    def reload_risk_config(self, cfg: RiskConfig | None = None) -> None:
        """Swap the active risk configuration (after an approved change)."""
        from app.risk.engine import RiskEngine

        with self._lock:
            self.risk_config = cfg or active_risk_config(self.settings, self.store)
            self.risk_engine = RiskEngine(self.store, self.graph, self.risk_config, self.ml_model)

    def rebuild_graph(self) -> None:
        from app.graph.networkx_backend import NetworkXGraph
        from app.risk.engine import RiskEngine

        if self.graph is not None and getattr(self.graph, "name", "") == "networkx":
            with self._lock:
                self.graph = NetworkXGraph.from_store(self.store)
                self.risk_engine = RiskEngine(self.store, self.graph, self.risk_config, self.ml_model)

    def refresh_retriever(self) -> None:
        from app.retrieval.embeddings import build_embedder
        from app.retrieval.hybrid import HybridRetriever

        self.embedder = build_embedder(self.settings.embedding_provider, self.settings.model_dir,
                                       self.settings.embedding_model)
        self.retriever = HybridRetriever(self.doc_repo, self.vectors, self.embedder)


def build_container(settings: Settings | None = None, load_ml: bool = True, build_graph: bool = True,
                    store: DataStore | None = None, llm: Any = None, agent_engine: str = "auto") -> Container:
    from app.agents.workflow import InvestigationAgent
    from app.documents.pipeline import IngestionPipeline
    from app.llm.provider import build_provider
    from app.retrieval.embeddings import build_embedder
    from app.retrieval.hybrid import HybridRetriever
    from app.retrieval.repository import FileDocumentRepository, SqlDocumentRepository
    from app.retrieval.vector_store import MemoryVectorStore, QdrantVectorStore
    from app.risk.engine import RiskEngine
    from app.risk.ml import AnomalyModel
    from app.tools.definitions import build_registry

    settings = settings or get_settings()
    store = store or build_store(settings)

    graph: Any = None
    if build_graph:
        if settings.graph_backend == "neo4j":
            from app.graph.neo4j_backend import Neo4jGraph

            graph = Neo4jGraph(settings.neo4j_uri or "", settings.neo4j_user or "", settings.neo4j_password or "")
        else:
            from app.graph.networkx_backend import NetworkXGraph

            graph = NetworkXGraph.from_store(store)

    risk_config = active_risk_config(settings, store)
    ml_model = AnomalyModel.load(settings.model_dir) if load_ml else None
    risk_engine = RiskEngine(store, graph, risk_config, ml_model)

    model_dir = Path(settings.model_dir)
    if settings.data_backend == "postgres":
        doc_repo: Any = SqlDocumentRepository(store.engine)  # type: ignore[attr-defined]
    else:
        doc_repo = FileDocumentRepository(model_dir / "documents_index.json")
    if settings.vector_backend == "qdrant" and settings.qdrant_url:
        vectors: Any = QdrantVectorStore(settings.qdrant_url, settings.qdrant_collection, settings.qdrant_api_key)
    else:
        vectors = MemoryVectorStore(model_dir / "vectors.npz")
    embedder = build_embedder(settings.embedding_provider, model_dir, settings.embedding_model)
    retriever = HybridRetriever(doc_repo, vectors, embedder)
    ingestion = IngestionPipeline(doc_repo, vectors, model_dir, embedder)
    llm = llm or build_provider(settings)
    registry = build_registry(settings.tool_timeout_s)
    c = Container(settings=settings, store=store, graph=graph, risk_config=risk_config, risk_engine=risk_engine,
                  doc_repo=doc_repo, vectors=vectors, embedder=embedder, retriever=retriever, ingestion=ingestion,
                  llm=llm, registry=registry, agent=None, ml_model=ml_model)
    c.agent = InvestigationAgent(c, registry, llm, engine=agent_engine,
                                 max_report_attempts=settings.agent_max_report_attempts)
    return c
