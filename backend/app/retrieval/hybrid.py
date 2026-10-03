"""Hybrid retrieval: semantic (vector) + keyword (BM25 / Postgres FTS) + metadata
filters, fused with Reciprocal Rank Fusion.

RRF is used because the two retrievers' scores are on incomparable scales; rank
fusion needs no score calibration. Every returned Passage carries full provenance
(document_id, page, section, chunk_id, source) for citation.
"""
from __future__ import annotations

from typing import Any

from app.documents.models import Passage
from app.retrieval.embeddings import Embedder
from app.retrieval.repository import DocumentRepository
from app.retrieval.vector_store import VectorStore

RRF_K = 60


class HybridRetriever:
    def __init__(self, repo: DocumentRepository, vectors: VectorStore | None, embedder: Embedder | None):
        self.repo = repo
        self.vectors = vectors
        self.embedder = embedder

    @property
    def semantic_available(self) -> bool:
        return self.vectors is not None and self.embedder is not None and self.vectors.count() > 0

    def search(self, query: str, k: int = 5, doc_types: list[str] | None = None, candidates: int = 20,
               mode: str = "hybrid") -> list[Passage]:
        sem: list[tuple[str, float]] = []
        kw: list[tuple[str, float]] = []
        if mode in ("hybrid", "semantic") and self.semantic_available:
            qv = self.embedder.embed([query])[0]  # type: ignore[union-attr]
            filters: dict[str, Any] | None = {"doc_type": doc_types} if doc_types else None
            sem = [(h["chunk_id"], h["score"]) for h in self.vectors.search(qv, candidates, filters)]  # type: ignore[union-attr]
        if mode in ("hybrid", "keyword"):
            kw = self.repo.keyword_search(query, candidates, doc_types)
        fused: dict[str, float] = {}
        for rank, (cid, _) in enumerate(sem, start=1):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank)
        for rank, (cid, _) in enumerate(kw, start=1):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank)
        top = sorted(fused.items(), key=lambda kv: -kv[1])[:k]
        meta = self.repo.chunks([cid for cid, _ in top])
        sem_rank = {cid: (r, s) for r, (cid, s) in enumerate(sem, start=1)}
        kw_rank = {cid: (r, s) for r, (cid, s) in enumerate(kw, start=1)}
        out = []
        for cid, score in top:
            m = meta.get(cid)
            if not m:
                continue
            out.append(Passage(
                chunk_id=cid, document_id=m["document_id"], title=m.get("title") or m["document_id"],
                doc_type=m.get("doc_type") or "unknown", page=m.get("page"), section=m.get("section"),
                source=m.get("source") or "", text=m["text"], score=round(score, 6),
                semantic_rank=sem_rank.get(cid, (None, None))[0], semantic_score=sem_rank.get(cid, (None, None))[1],
                keyword_rank=kw_rank.get(cid, (None, None))[0], keyword_score=kw_rank.get(cid, (None, None))[1],
                metadata=m.get("metadata") or {}))
        return out
