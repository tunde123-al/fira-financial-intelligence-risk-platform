"""Controlled improvement loop.

    agent output -> human feedback -> evaluation -> failure classification
      -> improvement proposal -> offline testing -> regression evaluation
      -> approval (a different, admin user) -> new active configuration

Nothing here changes production behaviour by itself. `propose()` creates a
*candidate* configuration and `regress()` evaluates it offline against the
benchmark; only `approve()` — called by an admin who is not the proposer —
activates it, and the previous active version is retired (and can be restored).
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any

from app.data.store import new_id, utcnow
from app.evaluation.benchmarks import risk_benchmark
from app.risk.config import RiskConfig
from app.risk.engine import RiskEngine
from app.security.principal import Principal

FAILURE_CATEGORIES = {"false_positive", "false_negative", "missing_evidence", "wrong_citation", "unclear_report",
                      "wrong_subject", "other"}

# acceptance criteria for a candidate vs the current configuration (see STD-MRM-008 in the corpus)
MAX_RECALL_DROP = 0.02
MAX_FPR_INCREASE = 0.01


class ApprovalError(PermissionError):
    pass


def classify_failures(store: Any, threshold: float) -> dict[str, Any]:
    """Join human decisions with the investigation's signals/score and classify outcomes."""
    decisions = store.list_decisions(limit=5000)
    per_signal: dict[str, dict[str, int]] = defaultdict(lambda: {"fp": 0, "tp": 0, "fn": 0})
    cases = []
    for d in decisions:
        inv = store.get_investigation(d.investigation_id)
        if inv is None or inv.risk_score is None:
            continue
        flagged = inv.risk_score >= threshold
        cats = set(d.failure_categories)
        if d.decision == "reject" and flagged:
            cats.add("false_positive")
        if d.decision in ("confirm", "escalate") and not flagged:
            cats.add("false_negative")
        for s in inv.signals:
            if "false_positive" in cats:
                per_signal[s]["fp"] += 1
            elif d.decision in ("confirm", "escalate"):
                per_signal[s]["tp"] += 1
            if "false_negative" in cats:
                per_signal[s]["fn"] += 1
        cases.append({"investigation_id": inv.investigation_id, "decision": d.decision, "score": inv.risk_score,
                      "signals": list(inv.signals), "categories": sorted(cats & FAILURE_CATEGORIES)})
    counts: dict[str, int] = defaultdict(int)
    for c in cases:
        for cat in c["categories"]:
            counts[cat] += 1
    return {"n_decisions": len(cases), "category_counts": dict(counts), "per_signal": dict(per_signal), "cases": cases}


def propose(cfg: RiskConfig, failures: dict[str, Any], min_cases: int = 3, fp_share: float = 0.6,
            step_down: float = 0.25, step_up: float = 0.15) -> tuple[RiskConfig, list[dict[str, Any]]]:
    """Rule-based proposals: down-weight signals dominated by false positives,
    up-weight signals present in false negatives. Bounded steps, never to zero."""
    new = deepcopy(cfg)
    changes = []
    for sig, st in failures["per_signal"].items():
        sc = new.signals.get(sig)
        if sc is None:
            continue
        n = st["fp"] + st["tp"]
        if n >= min_cases and st["fp"] / n >= fp_share:
            old = sc.weight
            sc.weight = round(max(old * (1 - step_down), 1.0), 2)
            changes.append({"signal": sig, "param": "weight", "old": old, "new": sc.weight,
                            "reason": f"{st['fp']}/{n} reviewed cases with this signal were rejected as false positives"})
        elif st["fn"] >= min_cases:
            old = sc.weight
            sc.weight = round(min(old * (1 + step_up), 60.0), 2)
            changes.append({"signal": sig, "param": "weight", "old": old, "new": sc.weight,
                            "reason": f"present in {st['fn']} confirmed cases scored below the threshold"})
    new.version = f"candidate-{utcnow():%Y%m%d%H%M%S}"
    return new, changes


def regress(store: Any, graph: Any, current: RiskConfig, candidate: RiskConfig, labels: list[dict[str, Any]],
            ml_model: Any = None) -> dict[str, Any]:
    base = risk_benchmark(RiskEngine(store, graph, current, ml_model), labels)
    cand = risk_benchmark(RiskEngine(store, graph, candidate, ml_model), labels)
    b, c = base["overall"], cand["overall"]
    checks = {
        "recall_drop_ok": (b["recall"] - c["recall"]) <= MAX_RECALL_DROP,
        "fpr_increase_ok": (c["false_positive_rate"] - b["false_positive_rate"]) <= MAX_FPR_INCREASE,
        "f1_not_worse": c["f1"] >= b["f1"] - 1e-9,
    }
    return {"baseline": {k: b[k] for k in ("precision", "recall", "f1", "false_positive_rate", "auc")},
            "candidate": {k: c[k] for k in ("precision", "recall", "f1", "false_positive_rate", "auc")},
            "checks": checks, "passed": all(checks.values()), "n_labels": len(labels)}


def submit_proposal(store: Any, graph: Any, current: RiskConfig, labels: list[dict[str, Any]], proposer: Principal,
                    candidate: RiskConfig | None = None, rationale: str | None = None,
                    ml_model: Any = None) -> dict[str, Any]:
    proposer.require("analyst")
    failures = classify_failures(store, current.score.investigation_threshold)
    changes: list[dict[str, Any]] = []
    if candidate is None:
        candidate, changes = propose(current, failures)
    if candidate.fingerprint() == current.fingerprint():
        return {"status": "no_change", "failures": {k: v for k, v in failures.items() if k != "cases"}}
    result = regress(store, graph, current, candidate, labels, ml_model)
    rec = {"version_id": new_id("CFG"), "created_at": utcnow(), "created_by": proposer.user_id,
           "status": "validated" if result["passed"] else "rejected",
           "rationale": rationale or "; ".join(f"{c['signal']} {c['param']} {c['old']}->{c['new']}: {c['reason']}"
                                               for c in changes) or "manual candidate",
           "config": candidate.model_dump(), "evaluation": {"regression": result, "changes": changes,
                                                            "failure_summary": failures["category_counts"]},
           "approved_by": None, "approved_at": None}
    store.save_config_version(rec)
    return rec


def approve(store: Any, version_id: str, approver: Principal) -> dict[str, Any]:
    approver.require("admin")
    recs = {r["version_id"]: r for r in store.list_config_versions()}
    rec = recs.get(version_id)
    if rec is None:
        raise LookupError(f"config version {version_id} not found")
    if rec["status"] != "validated":
        raise ApprovalError(f"only validated candidates can be approved (status is {rec['status']})")
    if rec["created_by"] == approver.user_id:
        raise ApprovalError("the approver must be different from the proposer (four-eyes principle)")
    for r in recs.values():
        if r["status"] == "active":
            store.update_config_version(r["version_id"], status="retired")
    store.update_config_version(version_id, status="active", approved_by=approver.user_id, approved_at=utcnow())
    return {**rec, "status": "active", "approved_by": approver.user_id}
