"""Metric functions (pure, unit-tested)."""
from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np


def confusion(y_true: Sequence[bool], y_pred: Sequence[bool]) -> dict[str, int]:
    tp = sum(1 for t, p in zip(y_true, y_pred) if t and p)
    fp = sum(1 for t, p in zip(y_true, y_pred) if not t and p)
    fn = sum(1 for t, p in zip(y_true, y_pred) if t and not p)
    tn = sum(1 for t, p in zip(y_true, y_pred) if not t and not p)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


def classification_metrics(y_true: Sequence[bool], y_pred: Sequence[bool]) -> dict[str, float]:
    c = confusion(y_true, y_pred)
    tp, fp, fn, tn = c["tp"], c["fp"], c["fn"], c["tn"]
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {**c, "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
            "false_positive_rate": round(fp / (fp + tn), 4) if fp + tn else 0.0,
            "false_negative_rate": round(fn / (fn + tp), 4) if fn + tp else 0.0}


def precision_at_k(ranked_relevance: Sequence[bool], k: int) -> float:
    top = list(ranked_relevance)[:k]
    return sum(top) / k if k else 0.0


def recall_at_k(ranked_relevance: Sequence[bool], k: int, n_relevant: int) -> float:
    return min(sum(list(ranked_relevance)[:k]), n_relevant) / n_relevant if n_relevant else 0.0


def reciprocal_rank(ranked_relevance: Sequence[bool]) -> float:
    for i, r in enumerate(ranked_relevance, start=1):
        if r:
            return 1.0 / i
    return 0.0


def percentile(values: Iterable[float], q: float) -> float:
    v = list(values)
    return float(np.percentile(v, q)) if v else 0.0


def roc_auc(y_true: Sequence[bool], scores: Sequence[float]) -> float:
    """Rank-based AUC (probability a random positive outranks a random negative)."""
    pos = [s for t, s in zip(y_true, scores) if t]
    neg = [s for t, s in zip(y_true, scores) if not t]
    if not pos or not neg:
        return 0.0
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return round(wins / (len(pos) * len(neg)), 4)
