"""Unsupervised anomaly model (Isolation Forest) over the risk engine's raw metrics.

Why this design: labelled fraud data is scarce and biased by past investigation
choices, so the model is unsupervised and trained on the whole population's
metric vectors (observed/threshold ratios from every detector, plus volume
features). It never sees ground-truth labels. Its output is reported as an
`ML_ANOMALY` signal, clearly typed as an ML prediction, with a small default
weight; it can surface combinations of individually sub-threshold behaviour.

Train offline:  python -m app.risk.ml train --sample 3000
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import IsolationForest

RATIO_METRICS = [
    "TRANSACTION_BURST", "VELOCITY_SPIKE", "AMOUNT_DEVIATION", "PEER_AMOUNT_DEVIATION", "RAPID_PASS_THROUGH",
    "FAN_IN", "GEO_NEW_COUNTRY", "IMPOSSIBLE_TRAVEL", "NEW_DEVICE", "DEVICE_SHARING", "SHARED_IDENTIFIER",
    "CIRCULAR_FLOW", "DORMANT_REACTIVATION", "HIGH_RISK_MERCHANT", "BEHAVIOURAL_SHIFT", "NETWORK_EXPOSURE",
]
FEATURES = [f"{m.lower()}_ratio" for m in RATIO_METRICS] + [
    "log_window_outbound_n", "log_window_outbound_usd", "outbound_usd_per_day_vs_baseline",
]
MODEL_FILE = "ml_anomaly.joblib"


def featurize(metrics: dict[str, dict[str, Any]], base: dict[str, Any], win: dict[str, Any]) -> np.ndarray:
    v = []
    for m in RATIO_METRICS:
        d = metrics.get(m) or {}
        obs, thr = float(d.get("observed") or 0.0), float(d.get("threshold") or 0.0)
        r = obs / thr if thr > 0 else 0.0
        v.append(np.log1p(min(max(r, 0.0), 50.0)))
    w_usd = float((win.get("outbound_usd") or {}).get("total", 0.0))
    b_usd = float((base.get("outbound_usd") or {}).get("total", 0.0))
    w_days = max(float(win.get("days", 1)), 1.0)
    b_days = max(float(base.get("days", 1)), 1.0)
    v.append(np.log1p(float(win.get("n_outbound", 0))))
    v.append(np.log1p(w_usd))
    v.append(np.log1p((w_usd / w_days) / max(b_usd / b_days, 1.0)))
    return np.array(v, dtype=float)


class AnomalyModel:
    def __init__(self, forest: IsolationForest, train_scores: np.ndarray, mean: np.ndarray, std: np.ndarray,
                 meta: dict[str, Any]):
        self.forest = forest
        self.train_scores = np.sort(train_scores)
        self.mean, self.std = mean, std
        self.meta = meta

    def _score(self, x: np.ndarray) -> float:
        return float(-self.forest.score_samples(x.reshape(1, -1))[0])

    def percentile(self, metrics, base, win) -> tuple[float, float]:
        s = self._score(featurize(metrics, base, win))
        pct = float(np.searchsorted(self.train_scores, s, side="right") / len(self.train_scores))
        return round(pct, 4), round(s, 4)

    def top_features(self, metrics, base, win, k: int = 3) -> list[tuple[str, float]]:
        x = featurize(metrics, base, win)
        z = (x - self.mean) / np.where(self.std > 1e-9, self.std, 1.0)
        idx = np.argsort(-np.abs(z))[:k]
        return [(FEATURES[i], round(float(z[i]), 2)) for i in idx]

    def describe(self) -> dict[str, Any]:
        return {"type": "IsolationForest", **self.meta}

    def save(self, model_dir: Path) -> Path:
        model_dir.mkdir(parents=True, exist_ok=True)
        path = model_dir / MODEL_FILE
        joblib.dump({"forest": self.forest, "train_scores": self.train_scores, "mean": self.mean,
                     "std": self.std, "meta": self.meta}, path)
        return path

    @classmethod
    def load(cls, model_dir: Path) -> AnomalyModel | None:
        path = Path(model_dir) / MODEL_FILE
        if not path.exists():
            return None
        d = joblib.load(path)
        return cls(d["forest"], d["train_scores"], d["mean"], d["std"], d["meta"])


def train(engine: Any, customer_ids: list[str], seed: int = 7) -> AnomalyModel:
    """Fit on the deterministic engine's metric vectors for `customer_ids` (unlabelled)."""
    rows = []
    for cid in customer_ids:
        a = engine.assess_customer(cid)
        rows.append(featurize(a.metrics, a.baseline, a.window))
    X = np.vstack(rows)
    forest = IsolationForest(n_estimators=200, contamination="auto", random_state=seed).fit(X)
    scores = -forest.score_samples(X)
    meta = {"trained_at": datetime.now(timezone.utc).isoformat(), "n_train": int(len(X)), "features": FEATURES,
            "sklearn": sklearn.__version__, "risk_config": engine.config.fingerprint()}
    return AnomalyModel(forest, scores, X.mean(axis=0), X.std(axis=0), meta)


def main() -> None:
    from app.services.container import build_container

    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["train"])
    p.add_argument("--sample", type=int, default=3000)
    p.add_argument("--seed", type=int, default=7)
    a = p.parse_args()
    c = build_container(load_ml=False)
    ids = c.list_customer_ids()
    rng = np.random.default_rng(a.seed)
    sample = list(rng.choice(ids, size=min(a.sample, len(ids)), replace=False))
    model = train(c.risk_engine, sample, a.seed)
    path = model.save(c.settings.model_dir)
    print(json.dumps({"saved": str(path), **model.meta}, indent=2))


if __name__ == "__main__":
    main()
