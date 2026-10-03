"""Vector stores: Qdrant (REST) and an in-process store.

The Qdrant client talks to the documented REST API directly with the standard
library, avoiding the gRPC dependency chain of `qdrant-client`. Point ids are
UUIDv5 values derived from chunk ids so re-ingestion is idempotent; the chunk id
and provenance live in the payload.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from app.core.http import require_http_url

NAMESPACE = uuid.UUID("6f1f7a4e-2b6c-4c1e-9a55-0f1ea1f1a000")


class VectorHit(dict):
    """{'chunk_id', 'score', 'payload'}"""


class VectorStore(Protocol):
    name: str

    def recreate(self, dim: int) -> None: ...
    def upsert(self, ids: list[str], vectors: np.ndarray, payloads: list[dict[str, Any]]) -> None: ...
    def search(self, vector: np.ndarray, k: int, filters: dict[str, Any] | None = None) -> list[VectorHit]: ...
    def count(self) -> int: ...
    def ping(self) -> bool: ...


def _match(payload: dict[str, Any], filters: dict[str, Any] | None) -> bool:
    if not filters:
        return True
    for k, v in filters.items():
        pv = payload.get(k)
        if isinstance(v, (list, tuple, set)):
            if pv not in v:
                return False
        elif pv != v:
            return False
    return True


class MemoryVectorStore:
    name = "memory"

    def __init__(self, persist_path: Path | None = None):
        self.persist_path = persist_path
        self.ids: list[str] = []
        self.payloads: list[dict[str, Any]] = []
        self.matrix = np.zeros((0, 0), dtype=np.float32)
        if persist_path and Path(persist_path).exists():
            d = np.load(persist_path, allow_pickle=False)
            self.matrix = d["matrix"]
            self.ids = list(d["ids"])
            self.payloads = json.loads(str(d["payloads"]))

    def recreate(self, dim: int) -> None:
        self.ids, self.payloads = [], []
        self.matrix = np.zeros((0, dim), dtype=np.float32)

    def upsert(self, ids: list[str], vectors: np.ndarray, payloads: list[dict[str, Any]]) -> None:
        index = {cid: i for i, cid in enumerate(self.ids)}
        rows = list(self.matrix) if len(self.matrix) else []
        for cid, v, p in zip(ids, vectors, payloads):
            if cid in index:
                rows[index[cid]] = v
                self.payloads[index[cid]] = p
            else:
                index[cid] = len(self.ids)
                self.ids.append(cid)
                self.payloads.append(p)
                rows.append(v)
        self.matrix = np.vstack(rows).astype(np.float32) if rows else self.matrix
        if self.persist_path:
            Path(self.persist_path).parent.mkdir(parents=True, exist_ok=True)
            np.savez(self.persist_path, matrix=self.matrix, ids=np.array(self.ids),
                     payloads=np.array(json.dumps(self.payloads)))

    def search(self, vector: np.ndarray, k: int, filters: dict[str, Any] | None = None) -> list[VectorHit]:
        if not self.ids:
            return []
        sims = self.matrix @ np.asarray(vector, dtype=np.float32)
        order = np.argsort(-sims)
        out: list[VectorHit] = []
        for i in order:
            if _match(self.payloads[i], filters):
                out.append(VectorHit(chunk_id=self.ids[i], score=float(sims[i]), payload=self.payloads[i]))
                if len(out) >= k:
                    break
        return out

    def count(self) -> int:
        return len(self.ids)

    def ping(self) -> bool:
        return True


class QdrantVectorStore:
    name = "qdrant"

    def __init__(self, url: str, collection: str, api_key: str | None = None, timeout: float = 10.0):
        self.url = require_http_url(url).rstrip("/")
        self.collection = collection
        self.api_key = api_key
        self.timeout = timeout

    def _req(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.url}{path}", data=data, method=method, headers=headers)  # noqa: S310 (scheme validated)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310  # nosec B310
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise RuntimeError(f"Qdrant {method} {path} failed: HTTP {e.code} {detail}") from None

    def recreate(self, dim: int) -> None:
        try:
            self._req("DELETE", f"/collections/{self.collection}")
        except RuntimeError:
            pass
        self._req("PUT", f"/collections/{self.collection}", {"vectors": {"size": int(dim), "distance": "Cosine"}})
        for field in ("doc_type", "document_id", "jurisdiction"):
            self._req("PUT", f"/collections/{self.collection}/index",
                      {"field_name": field, "field_schema": "keyword"})

    def upsert(self, ids: list[str], vectors: np.ndarray, payloads: list[dict[str, Any]]) -> None:
        for i in range(0, len(ids), 128):
            points: list[dict[str, Any]] = [{"id": str(uuid.uuid5(NAMESPACE, cid)), "vector": [float(x) for x in v],
                       "payload": {**p, "chunk_id": cid}}
                      for cid, v, p in zip(ids[i:i + 128], vectors[i:i + 128], payloads[i:i + 128])]
            self._req("PUT", f"/collections/{self.collection}/points?wait=true", {"points": points})

    def search(self, vector: np.ndarray, k: int, filters: dict[str, Any] | None = None) -> list[VectorHit]:
        body: dict[str, Any] = {"vector": [float(x) for x in vector], "limit": int(k), "with_payload": True}
        if filters:
            must = []
            for key, v in filters.items():
                if isinstance(v, (list, tuple, set)):
                    must.append({"key": key, "match": {"any": list(v)}})
                else:
                    must.append({"key": key, "match": {"value": v}})
            body["filter"] = {"must": must}
        res = self._req("POST", f"/collections/{self.collection}/points/search", body)
        return [VectorHit(chunk_id=h["payload"]["chunk_id"], score=float(h["score"]), payload=h["payload"])
                for h in res.get("result", [])]

    def count(self) -> int:
        try:
            res = self._req("POST", f"/collections/{self.collection}/points/count", {"exact": True})
            return int(res.get("result", {}).get("count", 0))
        except RuntimeError:
            return 0

    def ping(self) -> bool:
        try:
            self._req("GET", "/collections")
            return True
        except Exception:
            return False
