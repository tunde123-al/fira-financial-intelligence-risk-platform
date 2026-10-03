"""Graph explorer and document intelligence endpoints."""
from __future__ import annotations

import re
from pathlib import Path as FsPath
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Path, Query, UploadFile

from app.api.deps import audit, get_container, get_principal, present, require_role, run_tool
from app.security.principal import Principal

router = APIRouter(prefix="/api")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_SUFFIXES = {".pdf", ".docx", ".md", ".txt"}


@router.get("/graph/stats", tags=["graph"])
def graph_stats(c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return c.graph.stats()


@router.get("/graph/path", tags=["graph"])
def graph_path(source: str = Query(pattern=r"^ACC-\d{1,10}$"), target: str = Query(pattern=r"^ACC-\d{1,10}$"),
               max_hops: int = Query(6, ge=1, le=8), c: Any = Depends(get_container),
               p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return present(c, p, run_tool(c, p, "trace_transaction_path", {"source_account": source, "target_account": target,
                                                                     "max_hops": max_hops}))


@router.get("/graph/cluster/{customer_id}", tags=["graph"])
def graph_cluster(customer_id: str = Path(pattern=r"^CUST-\d{1,10}$"), c: Any = Depends(get_container),
                  p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return present(c, p, run_tool(c, p, "find_suspicious_cluster", {"customer_id": customer_id}))


@router.get("/graph/trace/{account_id}", tags=["graph"])
def graph_trace(account_id: str = Path(pattern=r"^ACC-\d{1,10}$"), direction: Literal["in", "out"] = "out",
                max_hops: int = Query(3, ge=1, le=5), c: Any = Depends(get_container),
                p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return present(c, p, run_tool(c, p, "trace_funds", {"account_id": account_id, "direction": direction,
                                                        "max_hops": max_hops}))


@router.get("/graph/{kind}/{entity_id}", tags=["graph"], summary="Bounded neighbourhood for the graph explorer")
def graph_neighbourhood(kind: Literal["customer", "account", "device", "merchant"],
                        entity_id: str = Path(pattern=r"^(CUST|ACC|DEV|MER)-\d{1,10}$"),
                        depth: int = Query(2, ge=1, le=3), limit: int = Query(150, ge=10, le=500),
                        c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return present(c, p, run_tool(c, p, "get_related_entities", {"kind": kind, "entity_id": entity_id,
                                                                   "depth": depth, "limit": limit}))


@router.get("/documents", tags=["documents"])
def list_documents(c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> list[dict[str, Any]]:
    return present(c, p, [{k: (str(v) if k == "ingested_at" else v) for k, v in d.items()} for d in c.doc_repo.documents()])


@router.get("/documents/search", tags=["documents"])
def search_documents(q: str = Query(min_length=2, max_length=500), k: int = Query(5, ge=1, le=20),
                     mode: Literal["hybrid", "semantic", "keyword"] = "hybrid",
                     doc_type: str | None = Query(default=None, pattern="^[a-z_]{2,40}$"),
                     c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return present(c, p, run_tool(c, p, "search_documents", {"query": q, "k": k, "mode": mode,
                                                             "doc_types": [doc_type] if doc_type else None}))


@router.post("/documents/ingest", tags=["documents"], summary="Re-ingest the documents directory (admin)")
def ingest(c: Any = Depends(get_container), p: Principal = Depends(require_role("admin"))) -> dict[str, Any]:
    from app.documents.pipeline import discover, investigation_notes_document

    notes = investigation_notes_document(c.store.list_investigations(status="closed", limit=5000))
    rep = c.ingestion.ingest_paths(discover(c.settings.documents_dir), root=c.settings.documents_dir,
                                   extra_documents=[notes])
    c.refresh_retriever()
    audit(c, p, "documents_ingest", "ok" if not rep["errors"] else "partial", details_count=len(rep["documents"]))
    return rep


@router.post("/documents/upload", tags=["documents"], summary="Upload and ingest one document (admin)")
async def upload(file: UploadFile = File(...), c: Any = Depends(get_container),
                 p: Principal = Depends(require_role("admin"))) -> dict[str, Any]:
    from app.documents.pipeline import discover, investigation_notes_document

    name = FsPath(file.filename or "").name
    suffix = FsPath(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES or not re.fullmatch(r"[A-Za-z0-9._ -]{1,120}", name):
        raise HTTPException(422, f"unsupported file name or type (allowed: {sorted(ALLOWED_SUFFIXES)})")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large")
    target_dir = FsPath(c.settings.documents_dir) / "uploads"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / name).write_bytes(data)
    notes = investigation_notes_document(c.store.list_investigations(status="closed", limit=5000))
    rep = c.ingestion.ingest_paths(discover(c.settings.documents_dir), root=c.settings.documents_dir,
                                   extra_documents=[notes])
    c.refresh_retriever()
    audit(c, p, "document_upload", "ok", "document", name, size=len(data))
    return rep
