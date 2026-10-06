"""Presentation of a risk assessment for investigators: explanation, detector results, breakdown."""
from __future__ import annotations

from typing import Any

from app.monitoring.detectors import detector_results
from app.risk.models import RiskAssessment


def explanation(a: RiskAssessment) -> str:
    """Deterministic plain-language explanation of the score (no model involved)."""
    if not a.contributors:
        return (f"Risk score {a.score:.1f} ({a.band}): no detector crossed its threshold in the window "
                f"{a.window_start:%Y-%m-%d} to {a.window_end:%Y-%m-%d}.")
    top = ", ".join(f"{c.signal_type} {c.points:+.1f}" for c in a.contributors[:4])
    cats = ", ".join(f"{x.label} {x.points:.1f}" for x in a.category_breakdown[:3] if x.category != "cap_adjustment")
    flagged = "above" if a.flagged else "below"
    return (f"Risk score {a.score:.1f} ({a.band}), {flagged} the investigation threshold of "
            f"{a.investigation_threshold:g}. Largest contributions: {top}. By category: {cats}. The score is the "
            f"sum of weight x strength per detector with group caps, using risk configuration {a.config_version}; "
            f"it is deterministic and contains no customer-profile or document component.")


def supporting_transactions(a: RiskAssessment, limit: int = 200) -> list[str]:
    seen: dict[str, None] = {}
    for s in a.signals:
        for e in s.evidence:
            if e.kind == "transaction":
                seen[e.id] = None
    return list(seen)[:limit]


def risk_view(a: RiskAssessment) -> dict[str, Any]:
    """The assessment plus detector results, an explanation and the transactions that support it."""
    out = a.model_dump(mode="json")
    out["detector_results"] = [r.model_dump(mode="json") for r in detector_results(a)]
    out["explanation"] = explanation(a)
    out["supporting_transactions"] = supporting_transactions(a)
    out["score_components"] = [{"category": x.category, "label": x.label, "points": x.points}
                               for x in a.category_breakdown]
    out["not_in_score"] = ["customer profile", "document evidence"]
    return out
