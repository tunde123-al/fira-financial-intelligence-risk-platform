"""Operational alert-quality metrics, computed only where the denominator is defined.

In day-to-day operation an alert that nobody investigated has no known truth, and a customer who never alerted is not
a "true negative" anyone verified. So the false-positive *rate* (FP / (FP + TN)) and *recall* (TP / (TP + FN)) cannot be
measured from the alert workflow and are deliberately not reported here. What can be measured honestly is:

* confirmation rate (precision among decided alerts) = confirmed / decided
* false-discovery rate = (cleared + false positive) / decided      (= 1 - confirmation rate)
* closure rate = decided / all alerts, and the open backlog
* disposition mix and time-to-decision

"Decided" means the alert reached RESOLVED with a recorded resolution. Rates over fewer than `LOW_SAMPLE` decided alerts
are flagged. FPR / recall / F1 appear only in the labelled synthetic evaluation (docs/EVALUATION.md), where the
ground truth for every customer is known by construction.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Any

from app.monitoring.models import MonitoringAlert

LOW_SAMPLE = 30
FALSE_POSITIVE_LIKE = ("CLEARED", "FALSE_POSITIVE")
NOT_COMPUTED = {
    "false_positive_rate": "needs verified true negatives; unreviewed alerts and non-alerting customers have no "
                           "known outcome in operation. Reported only in the labelled synthetic evaluation.",
    "recall": "needs the number of suspicious customers that never alerted, which is unknown in operation. "
              "Reported only in the labelled synthetic evaluation.",
}


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _group(alerts: list[MonitoringAlert]) -> dict[str, Any]:
    decided = [a for a in alerts if a.status == "RESOLVED" and a.resolution]
    c: Counter[str] = Counter(str(a.resolution) for a in decided)
    confirmed, fp_like = c.get("CONFIRMED_SUSPICIOUS", 0), sum(c.get(r, 0) for r in FALSE_POSITIVE_LIKE)
    return {
        "alerts": len(alerts), "decided": len(decided), "open": len(alerts) - len(decided),
        "confirmed": confirmed, "cleared": c.get("CLEARED", 0), "false_positive": c.get("FALSE_POSITIVE", 0),
        "confirmation_rate": _rate(confirmed, len(decided)),
        "false_discovery_rate": _rate(fp_like, len(decided)),
        "closure_rate": _rate(len(decided), len(alerts)),
        "low_sample": len(decided) < LOW_SAMPLE,
    }


def alert_quality(alerts: list[MonitoringAlert], since: datetime | None = None) -> dict[str, Any]:
    """Quality metrics over alerts triggered on or after `since` (all alerts when None)."""
    pool = [a for a in alerts if since is None or a.triggered_at >= since]
    overall = _group(pool)
    hours = [(a.resolved_at - a.triggered_at).total_seconds() / 3600.0 for a in pool
             if a.status == "RESOLVED" and a.resolved_at]
    by: dict[str, dict[str, list[MonitoringAlert]]] = {"priority": defaultdict(list), "detector": defaultdict(list),
                                                        "severity": defaultdict(list)}
    for a in pool:
        by["priority"][a.triage_priority or "UNSCORED"].append(a)
        by["detector"][a.detector_id].append(a)
        by["severity"][a.severity].append(a)
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNSCORED": 4}
    return {
        "since": since.isoformat() if since else None,
        "overall": overall,
        "status_counts": dict(Counter(a.status for a in pool)),
        "escalated_now": sum(1 for a in pool if a.status == "ESCALATED"),
        "time_to_decision_hours": {"median": round(median(hours), 2) if hours else None,
                                   "max": round(max(hours), 2) if hours else None, "n": len(hours)},
        "by_priority": {k: _group(v) for k, v in sorted(by["priority"].items(), key=lambda kv: order.get(kv[0], 9))},
        "by_detector": {k: _group(v) for k, v in sorted(by["detector"].items())},
        "by_severity": {k: _group(v) for k, v in sorted(by["severity"].items())},
        "not_computed": NOT_COMPUTED,
        "definitions": {
            "confirmation_rate": "confirmed suspicious / decided alerts (precision among alerts a human decided)",
            "false_discovery_rate": "(cleared + false positive) / decided alerts",
            "closure_rate": "decided alerts / all alerts in the window",
            "decided": "status RESOLVED with a recorded resolution",
            "low_sample": f"fewer than {LOW_SAMPLE} decided alerts: treat the rate as indicative only"},
        "note": "Decisions are human judgements recorded in the workflow; they are feedback for review of detector "
                "thresholds, never an automatic training signal.",
    }


def feedback_rows(alerts: list[MonitoringAlert], limit: int = 200) -> list[dict[str, Any]]:
    """Recorded investigator outcomes: alert, decision, reason, investigator and timestamp (most recent first)."""
    rows = [{"alert_id": a.alert_id, "customer_id": a.customer_id, "detector_id": a.detector_id,
             "decision": a.resolution, "reason": a.resolution_reason, "investigator": a.resolved_by,
             "decided_at": a.resolved_at, "triage_priority": a.triage_priority, "triage_score": a.triage_score}
            for a in alerts if a.status == "RESOLVED" and a.resolution]
    rows.sort(key=lambda r: r["decided_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows[:limit]
