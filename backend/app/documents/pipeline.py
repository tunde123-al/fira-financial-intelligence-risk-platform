"""Ingestion pipeline:

    file -> parse (+OCR) -> classify -> structure -> metadata -> chunk -> embed -> vector store
                                                                     \\-> repository (text + provenance)

    python -m app.documents.pipeline ingest [--dir ../documents]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.documents.chunking import chunk_blocks
from app.documents.classify import classify, extract_metadata
from app.documents.models import Block, Chunk, ParsedDocument
from app.documents.parsers import SUPPORTED_SUFFIXES, parse_file
from app.retrieval.embeddings import Embedder, LSAEmbedder
from app.retrieval.repository import DocumentRepository
from app.retrieval.vector_store import VectorStore


def _doc_id_for(path: Path, declared: str | None) -> str:
    if declared:
        return str(declared)
    stem = re.sub(r"[^A-Za-z0-9]+", "-", path.stem).strip("-").upper()
    return f"DOC-{stem[:40]}"


def parse_document(path: Path, root: Path | None = None) -> ParsedDocument:
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    pr = parse_file(path)
    full_text = "\n".join(b.text for b in pr.blocks)
    declared_type = pr.metadata.get("doc_type")
    first_heading = next((b.text for b in pr.blocks if b.kind == "heading"), "")
    doc_type, conf, _ = classify(full_text, declared_type, str(pr.metadata.get("title") or first_heading))
    meta = extract_metadata(full_text, {k: v for k, v in pr.metadata.items()
                                        if k not in {"doc_type", "document_id", "title", "source"}})
    title = pr.metadata.get("title") or next((b.text for b in pr.blocks if b.kind == "heading"), path.stem)
    rel = str(path.relative_to(root)) if root and path.is_relative_to(root) else path.name
    return ParsedDocument(
        document_id=_doc_id_for(path, pr.metadata.get("document_id")), title=str(title), doc_type=doc_type,
        doc_type_confidence=conf, source=str(pr.metadata.get("source") or rel), path=rel, sha256=sha,
        page_count=pr.page_count, parser=pr.parser, ocr_pages=pr.ocr_pages, metadata=meta, blocks=pr.blocks)


def payload_for(doc_meta: dict[str, Any], chunk: Chunk) -> dict[str, Any]:
    return {"document_id": chunk.document_id, "doc_type": doc_meta["doc_type"], "title": doc_meta["title"],
            "page": chunk.page, "section": chunk.section, "source": doc_meta["source"],
            "jurisdiction": doc_meta.get("jurisdiction") or "unspecified"}


class IngestionPipeline:
    def __init__(self, repo: DocumentRepository, vectors: VectorStore, model_dir: Path,
                 embedder: Embedder | None = None, embedding_dim: int = 128):
        self.repo = repo
        self.vectors = vectors
        self.model_dir = Path(model_dir)
        self.embedder = embedder
        self.embedding_dim = embedding_dim

    def ingest_paths(self, paths: list[Path], root: Path | None = None,
                     extra_documents: list[tuple[ParsedDocument, list[Chunk]]] | None = None) -> dict[str, Any]:
        report: dict[str, Any] = {"documents": [], "errors": [], "warnings": []}
        for p in sorted(paths):
            try:
                doc = parse_document(p, root)
                chunks = chunk_blocks(doc.document_id, doc.blocks)
                self.repo.save(doc, chunks)
                report["documents"].append({"document_id": doc.document_id, "title": doc.title,
                                            "doc_type": doc.doc_type, "parser": doc.parser,
                                            "ocr_pages": doc.ocr_pages, "pages": doc.page_count,
                                            "chunks": len(chunks)})
                if not chunks or (doc.metadata or {}).get("ocr_errors"):
                    report["warnings"].append({
                        "path": str(p), "chunks": len(chunks),
                        "warning": "no text was extracted or OCR failed; scanned PDFs need the Tesseract binary "
                                   "installed, otherwise they contribute no searchable chunks"})
            except Exception as exc:  # one bad file must not stop the batch
                report["errors"].append({"path": str(p), "error": f"{type(exc).__name__}: {exc}"})
        for doc, chunks in extra_documents or []:
            self.repo.save(doc, chunks)
            report["documents"].append({"document_id": doc.document_id, "title": doc.title, "doc_type": doc.doc_type,
                                        "parser": doc.parser, "chunks": len(chunks)})
        report["index"] = self.reindex()
        return report

    def reindex(self) -> dict[str, Any]:
        """(Re)embed every chunk. LSA is refitted on the whole corpus, so all vectors are rebuilt."""
        chunks = self.repo.all_chunks()
        if not chunks:
            return {"chunks": 0}
        docs = {d["document_id"]: d for d in self.repo.documents()}
        texts = [f"{c.section or ''}\n{c.text}" for c in chunks]
        if self.embedder is None or isinstance(self.embedder, LSAEmbedder):
            emb = LSAEmbedder.fit(texts, dim=self.embedding_dim)
            emb.save(self.model_dir / "lsa_embedder.joblib")
            self.embedder = emb
        vecs = self.embedder.embed(texts)
        self.vectors.recreate(vecs.shape[1])
        self.vectors.upsert([c.chunk_id for c in chunks], vecs, [payload_for(docs[c.document_id], c) for c in chunks])
        return {"chunks": len(chunks), "dim": int(vecs.shape[1]), "embedder": self.embedder.name,
                "vector_store": self.vectors.name}


def investigation_notes_document(investigations: list[Any]) -> tuple[ParsedDocument, list[Chunk]]:
    """Index closed investigation summaries (historical case information) for semantic recall."""
    blocks = []
    for inv in investigations:
        if inv.status != "closed" or not inv.summary:
            continue
        sec = f"{inv.investigation_id} ({inv.subject_type} {inv.subject_id})"
        blocks.append(Block(kind="heading", text=sec, level=2, section_path=[sec]))
        blocks.append(Block(kind="paragraph", section_path=[sec],
                            text=f"{inv.summary} Signals: {', '.join(inv.signals) or 'none recorded'}. "
                                 f"Conclusion: {inv.conclusion or 'n/a'}."))
    raw = json.dumps([b.text for b in blocks]).encode()
    doc = ParsedDocument(document_id="CASE-NOTES", title="Historical investigation case notes",
                         doc_type="investigation_report", source="investigations table", path="db://investigations",
                         sha256=hashlib.sha256(raw).hexdigest(), parser="database", metadata={"jurisdiction": "internal"},
                         blocks=blocks)
    chunks = chunk_blocks(doc.document_id, blocks, max_chars=600, overlap_chars=0)
    for c in chunks:
        c.metadata["investigation_id"] = (c.section or "").split(" ")[0]
    return doc, chunks


def discover(directory: Path) -> list[Path]:
    return [p for p in Path(directory).rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
            and not p.name.startswith(".") and p.name.lower() != "readme.md"]


def main() -> None:
    from app.services.container import build_container

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["ingest", "list"])
    ap.add_argument("--dir", type=Path, default=None)
    a = ap.parse_args()
    c = build_container(load_ml=False, build_graph=False)
    if a.cmd == "list":
        print(json.dumps(c.doc_repo.documents(), indent=2, default=str))
        return
    directory = a.dir or c.settings.documents_dir
    notes = investigation_notes_document(c.store.list_investigations(status="closed", limit=5000))
    rep = c.ingestion.ingest_paths(discover(directory), root=directory, extra_documents=[notes])
    print(json.dumps(rep, indent=2, default=str))


if __name__ == "__main__":
    main()
