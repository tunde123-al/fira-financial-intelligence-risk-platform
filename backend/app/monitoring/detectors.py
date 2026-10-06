"""Detector results derived from a risk assessment.

The detection logic itself is `RiskEngine`. This adapter turns an assessment into the
structured `DetectionResult` list the monitoring layer, the API and the UI use, so there
is a single implementation of every detector.
"""
from __future__ import annotations

from typing import Any

from app.monitoring.models import DetectionResult
from app.risk.catalog import CATALOG, DetectorInfo
from app.risk.config import RiskConfig
from app.risk.models import RiskAssessment, RiskSignal


def _txn_ids(signal: RiskSignal) -> list[str]:
    seen: dict[str, None] = {}
    for e in signal.evidence:
        if e.kind == "transaction":
            seen[e.id] = None
    return list(seen)


def result_from_signal(signal: RiskSignal, points: float, assessment: RiskAssessment) -> DetectionResult:
    info = CATALOG.get(signal.signal_type)
    account_ids = sorted({e.id for e in signal.evidence if e.kind == "account"})
    if signal.data.get("account_id"):
        account_ids = sorted(set(account_ids) | {str(signal.data["account_id"])})
    return DetectionResult(
        detector_id=signal.signal_type, name=info.name if info else signal.signal_type,
        category=info.category if info else "other", triggered=True, severity=signal.severity,
        risk_contribution=round(points, 2), reason=signal.description, transaction_ids=_txn_ids(signal),
        customer_id=assessment.customer_id or assessment.entity_id, account_ids=account_ids,
        observed=signal.observed_value, baseline=signal.baseline_value, threshold=signal.threshold,
        unit=signal.unit, window_start=signal.window_start, window_end=signal.window_end,
        data_quality=list(assessment.data_quality))


def detector_results(assessment: RiskAssessment, include_not_triggered: bool = True) -> list[DetectionResult]:
    """One result per detector: triggered ones with their evidence, the rest with observed/threshold."""
    points = {c.signal_type: c.points for c in assessment.contributors}
    out = [result_from_signal(s, points.get(s.signal_type, 0.0), assessment) for s in assessment.signals]
    if include_not_triggered:
        fired = {r.detector_id for r in out}
        not_eval = {n.signal_type: n.reason for n in assessment.not_evaluated}
        for det_id, info in CATALOG.items():
            if det_id in fired:
                continue
            m: dict[str, Any] = assessment.metrics.get(det_id, {})
            reason = (f"not evaluated: {not_eval[det_id]}" if det_id in not_eval
                      else "below threshold" if m else "no data")
            out.append(DetectionResult(
                detector_id=det_id, name=info.name, category=info.category, triggered=False, reason=reason,
                customer_id=assessment.customer_id or assessment.entity_id, observed=m.get("observed"),
                threshold=m.get("threshold"), window_start=assessment.window_start,
                window_end=assessment.window_end))
    return sorted(out, key=lambda r: (not r.triggered, -r.risk_contribution, r.detector_id))


def catalogue(config: RiskConfig, tier_of: Any) -> list[dict[str, Any]]:
    """Detector metadata merged with the active configuration (for the API/UI)."""
    rows = []
    for det_id, info in CATALOG.items():
        sc = config.signals.get(det_id)
        rows.append(_row(info, sc, tier_of(det_id)))
    return rows


def _row(info: DetectorInfo, sc: Any, tier: str) -> dict[str, Any]:
    return {
        "detector_id": info.detector_id, "name": info.name, "description": info.description,
        "category": info.category, "default_severity": info.default_severity,
        "enabled": bool(sc and sc.enabled), "weight": sc.weight if sc else None,
        "group": sc.group if sc else None, "parameters": sc.params if sc else {},
        "alert_tier": tier, "creates_alerts": tier != "context",
    }
