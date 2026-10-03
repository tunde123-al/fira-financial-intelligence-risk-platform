"""Embedding providers.

`LSAEmbedder` (default) is a fully local, deterministic embedding: TF-IDF over
word uni/bi-grams followed by truncated SVD (latent semantic analysis), L2
normalised. It needs no network and no GPU, which keeps the whole stack runnable
offline and reproducible in CI. Its semantic quality is below neural embedding
models; the retrieval benchmark (EVALUATION.md) quantifies it, and the provider
can be switched to an API model with EMBEDDING_PROVIDER=openai.

Because LSA is fitted on the corpus, re-fitting changes the vector space: the
ingestion pipeline therefore always re-embeds and re-indexes every chunk when it
refits, and stores the fitted model next to the index.
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any, Protocol

import joblib
import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer

from app.core.http import require_http_url


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray: ...


def _normalize(x: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(x, axis=1, keepdims=True)
    return x / np.where(n == 0, 1.0, n)


class LSAEmbedder:
    name = "lsa"

    def __init__(self, vectorizer: TfidfVectorizer, svd: TruncatedSVD | None):
        self.vectorizer = vectorizer
        self.svd = svd
        self.dim = int(svd.n_components) if svd is not None else len(vectorizer.vocabulary_)

    @classmethod
    def fit(cls, texts: list[str], dim: int = 128, seed: int = 13) -> LSAEmbedder:
        vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=1, stop_words="english",
                              token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z0-9\-]+\b")  # noqa: S106
        X = vec.fit_transform(texts)
        k = min(dim, X.shape[0] - 1, X.shape[1] - 1)
        svd = TruncatedSVD(n_components=k, random_state=seed).fit(X) if k >= 2 else None
        return cls(vec, svd)

    def embed(self, texts: list[str]) -> np.ndarray:
        X = self.vectorizer.transform(texts)
        Z = self.svd.transform(X) if self.svd is not None else X.toarray()
        return _normalize(np.asarray(Z, dtype=np.float32))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"vectorizer": self.vectorizer, "svd": self.svd}, path)

    @classmethod
    def load(cls, path: Path) -> LSAEmbedder | None:
        if not Path(path).exists():
            return None
        d = joblib.load(path)
        return cls(d["vectorizer"], d["svd"])


class OpenAIEmbedder:  # pragma: no cover - requires network + key
    """OpenAI-compatible /v1/embeddings endpoint (works with many local servers too)."""

    name = "openai"

    def __init__(self, model: str, api_key: str, base_url: str = "https://api.openai.com/v1", dim: int = 1536,
                 timeout: int = 60):
        self.model, self.api_key, self.dim, self.timeout = model, api_key, dim, timeout
        self.base_url = require_http_url(base_url).rstrip("/")

    def embed(self, texts: list[str]) -> np.ndarray:
        out: list[Any] = []
        for i in range(0, len(texts), 64):
            body = json.dumps({"model": self.model, "input": texts[i:i + 64]}).encode()
            req = urllib.request.Request(f"{self.base_url}/embeddings", data=body, method="POST",  # noqa: S310 (scheme validated)
                                         headers={"Authorization": f"Bearer {self.api_key}",
                                                  "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310  # nosec B310
                data = json.loads(r.read())
            out.extend(d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"]))
        arr = _normalize(np.asarray(out, dtype=np.float32))
        self.dim = arr.shape[1]
        return arr


def build_embedder(provider: str, model_dir: Path, model: str | None = None) -> Embedder | None:
    if provider == "openai":
        key = os.environ.get("EMBEDDING_API_KEY") or os.environ.get("LLM_API_KEY")
        if not key:
            raise RuntimeError("EMBEDDING_PROVIDER=openai requires EMBEDDING_API_KEY")
        return OpenAIEmbedder(model or "text-embedding-3-small", key,
                              os.environ.get("EMBEDDING_BASE_URL", "https://api.openai.com/v1"))
    return LSAEmbedder.load(Path(model_dir) / "lsa_embedder.joblib")
