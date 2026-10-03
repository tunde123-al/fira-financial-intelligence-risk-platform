"""Okapi BM25 keyword index (in-process).

Used for keyword retrieval when documents are kept in the file-backed repository.
With PostgreSQL, keyword search uses the `document_chunks.tsv` full-text index
instead (see SqlDocumentRepository).
"""
from __future__ import annotations

import math
import re
from collections import Counter

STOP = set("""a an the and or of to in on for by with at from as is are was were be been this that these those it its
which who whom whose what when where why how not no but if then than so such may must should can could would will
shall into over under about after before between within without per via""".split())
TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    toks = [t for t in TOKEN_RE.findall(text.lower()) if t not in STOP and len(t) > 1]
    return [_stem(t) for t in toks]


def _stem(t: str) -> str:
    for suf in ("ing", "edly", "ed", "ies", "es", "s"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[: -len(suf)] + ("y" if suf == "ies" else "")
    return t


class BM25:
    def __init__(self, k1: float = 1.4, b: float = 0.75):
        self.k1, self.b = k1, b
        self.ids: list[str] = []
        self.tfs: list[Counter[str]] = []
        self.lens: list[int] = []
        self.df: Counter[str] = Counter()

    def add(self, doc_id: str, text: str) -> None:
        toks = tokenize(text)
        tf = Counter(toks)
        self.ids.append(doc_id)
        self.tfs.append(tf)
        self.lens.append(len(toks))
        self.df.update(tf.keys())

    def search(self, query: str, k: int = 10, allowed: set[str] | None = None) -> list[tuple[str, float]]:
        q = tokenize(query)
        if not q or not self.ids:
            return []
        n = len(self.ids)
        avg = sum(self.lens) / n
        scores = []
        for i, tf in enumerate(self.tfs):
            if allowed is not None and self.ids[i] not in allowed:
                continue
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if not f:
                    continue
                idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                s += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.lens[i] / avg))
            if s > 0:
                scores.append((self.ids[i], s))
        scores.sort(key=lambda x: -x[1])
        return scores[:k]
