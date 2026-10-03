"""Document & chunk repositories (the source of truth for chunk text and provenance).

* FileDocumentRepository — JSON on disk + in-process BM25 keyword index.
* SqlDocumentRepository  — `document_registry` / `document_chunks` tables with the
  PostgreSQL full-text (`tsv`) index for keyword search.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Protocol

from app.documents.models import Chunk, ParsedDocument
from app.retrieval.bm25 import BM25

DOC_SQL = {
    "upsert_doc": (
        "INSERT INTO document_registry (document_id, title, doc_type, source, jurisdiction, version, sha256, "
        "page_count, metadata) VALUES (:document_id, :title, :doc_type, :source, :jurisdiction, :version, :sha256, "
        ":page_count, CAST(:metadata AS jsonb)) ON CONFLICT (document_id) DO UPDATE SET title = EXCLUDED.title, "
        "doc_type = EXCLUDED.doc_type, source = EXCLUDED.source, jurisdiction = EXCLUDED.jurisdiction, "
        "version = EXCLUDED.version, sha256 = EXCLUDED.sha256, page_count = EXCLUDED.page_count, "
        "metadata = EXCLUDED.metadata, ingested_at = now()"),
    "delete_chunks": "DELETE FROM document_chunks WHERE document_id = :document_id",
    "insert_chunk": ("INSERT INTO document_chunks (chunk_id, document_id, ordinal, page, section, text, metadata) "
                     "VALUES (:chunk_id, :document_id, :ordinal, :page, :section, :text, CAST(:metadata AS jsonb))"),
    "documents": "SELECT document_id, title, doc_type, source, jurisdiction, version, sha256, page_count, ingested_at, "
                 "metadata FROM document_registry ORDER BY document_id",
    "chunks_by_id": ("SELECT c.chunk_id, c.document_id, c.ordinal, c.page, c.section, c.text, c.metadata, "
                     "d.title, d.doc_type, d.source FROM document_chunks c JOIN document_registry d "
                     "ON d.document_id = c.document_id WHERE c.chunk_id = ANY(:ids)"),
    "all_chunks": ("SELECT c.chunk_id, c.document_id, c.ordinal, c.page, c.section, c.text, c.metadata "
                   "FROM document_chunks c ORDER BY c.document_id, c.ordinal"),
    "keyword": ("SELECT c.chunk_id, ts_rank_cd(c.tsv, q) AS score FROM document_chunks c "
                "JOIN document_registry d ON d.document_id = c.document_id, "
                "websearch_to_tsquery('english', :query) q WHERE c.tsv @@ q "
                "AND (:doc_types_all OR d.doc_type = ANY(:doc_types)) ORDER BY score DESC LIMIT :k"),
    "keyword_any": ("SELECT c.chunk_id, ts_rank_cd(c.tsv, q) AS score FROM document_chunks c "
                    "JOIN document_registry d ON d.document_id = c.document_id, "
                    "to_tsquery('english', :tsquery) q WHERE c.tsv @@ q "
                    "AND (:doc_types_all OR d.doc_type = ANY(:doc_types)) ORDER BY score DESC LIMIT :k"),
}


class DocumentRepository(Protocol):
    def save(self, doc: ParsedDocument, chunks: list[Chunk]) -> None: ...
    def documents(self) -> list[dict[str, Any]]: ...
    def chunks(self, ids: list[str]) -> dict[str, dict[str, Any]]: ...
    def all_chunks(self) -> list[Chunk]: ...
    def keyword_search(self, query: str, k: int, doc_types: list[str] | None = None) -> list[tuple[str, float]]: ...


def _doc_record(doc: ParsedDocument) -> dict[str, Any]:
    return {"document_id": doc.document_id, "title": doc.title, "doc_type": doc.doc_type, "source": doc.source,
            "jurisdiction": doc.metadata.get("jurisdiction"), "version": doc.metadata.get("version"),
            "sha256": doc.sha256, "page_count": doc.page_count,
            "metadata": {**doc.metadata, "parser": doc.parser, "ocr_pages": doc.ocr_pages, "path": doc.path,
                         "doc_type_confidence": doc.doc_type_confidence}}


class FileDocumentRepository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._docs: dict[str, dict[str, Any]] = {}
        self._chunks: dict[str, dict[str, Any]] = {}
        self._bm25: BM25 | None = None
        if self.path.exists():
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self._docs, self._chunks = d["documents"], d["chunks"]

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"documents": self._docs, "chunks": self._chunks}), encoding="utf-8")

    def save(self, doc: ParsedDocument, chunks: list[Chunk]) -> None:
        with self._lock:
            self._docs[doc.document_id] = _doc_record(doc)
            self._chunks = {k: v for k, v in self._chunks.items() if v["document_id"] != doc.document_id}
            for c in chunks:
                self._chunks[c.chunk_id] = c.model_dump()
            self._bm25 = None
            self._persist()

    def documents(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in sorted(self._docs.values(), key=lambda d: d["document_id"])]

    def chunks(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        with self._lock:
            out = {}
            for i in ids:
                c = self._chunks.get(i)
                if c:
                    d = self._docs.get(c["document_id"], {})
                    out[i] = {**c, "title": d.get("title"), "doc_type": d.get("doc_type"), "source": d.get("source")}
            return out

    def all_chunks(self) -> list[Chunk]:
        with self._lock:
            return [Chunk(**c) for c in sorted(self._chunks.values(), key=lambda c: (c["document_id"], c["ordinal"]))]

    def keyword_search(self, query: str, k: int, doc_types: list[str] | None = None) -> list[tuple[str, float]]:
        with self._lock:
            if self._bm25 is None:
                self._bm25 = BM25()
                for cid, c in self._chunks.items():
                    self._bm25.add(cid, f"{c.get('section') or ''}\n{c['text']}")
            allowed = None
            if doc_types:
                docs = {d for d, r in self._docs.items() if r["doc_type"] in doc_types}
                allowed = {cid for cid, c in self._chunks.items() if c["document_id"] in docs}
            return self._bm25.search(query, k, allowed)


class SqlDocumentRepository:
    def __init__(self, engine: Any):
        self.engine = engine

    def save(self, doc: ParsedDocument, chunks: list[Chunk]) -> None:
        from sqlalchemy import text

        rec = _doc_record(doc)
        rec["metadata"] = json.dumps(rec["metadata"], default=str)
        with self.engine.begin() as conn:
            conn.execute(text(DOC_SQL["upsert_doc"]), rec)
            conn.execute(text(DOC_SQL["delete_chunks"]), {"document_id": doc.document_id})
            for c in chunks:
                d = c.model_dump()
                d["metadata"] = json.dumps(d["metadata"], default=str)
                conn.execute(text(DOC_SQL["insert_chunk"]), d)

    def _rows(self, key: str, **params: Any) -> list[dict[str, Any]]:
        from sqlalchemy import text

        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(text(DOC_SQL[key]), params)]

    def documents(self) -> list[dict[str, Any]]:
        return self._rows("documents")

    def chunks(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        return {r["chunk_id"]: r for r in self._rows("chunks_by_id", ids=list(ids))} if ids else {}

    def all_chunks(self) -> list[Chunk]:
        return [Chunk(**r) for r in self._rows("all_chunks")]

    def keyword_search(self, query: str, k: int, doc_types: list[str] | None = None) -> list[tuple[str, float]]:
        # OR-semantics over stemmed terms (websearch_to_tsquery is AND-only, which is too strict for
        # investigator queries built from several signal names).
        from app.retrieval.bm25 import tokenize

        terms = sorted({t for t in tokenize(query) if t.isalnum()})
        if not terms:
            return []
        rows = self._rows("keyword_any", tsquery=" | ".join(terms), k=k, doc_types_all=not doc_types,
                          doc_types=list(doc_types or []))
        return [(r["chunk_id"], float(r["score"])) for r in rows]
