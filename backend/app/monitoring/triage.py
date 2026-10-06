"""Explainable alert triage: a deterministic heuristic that ranks alerts for investigators.

The score is the sum of twelve capped factors (maxima add up to exactly 100). It is a *prioritisation aid*, not a
probability of fraud and not a model: every factor is a fixed rule over information that existed when the alert was
raised (plus the alert's age and the customer's earlier, already-closed outcomes). It never reads the alert's own
outcome or any ground-truth label. The thresholds and lookup tables live in `monitoring_config.yaml` (`triage:`) and
changes to them are recorded in the configuration change log.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# (factor id, label, maximum points). The maxima are part of the documented method and are asserted to sum to 100.
FACTORS: tuple[tuple[str, str, float], ...] = (
    ("detector_severity", "Detector severity", 13.0),
    ("customer_risk_score", "Customer risk score (0-100 engine score)", 25.0),
    ("detector_tier", "Detector tier (standalone typology vs supporting deviation)", 5.0),
    ("corroborating_categories", "Independent signal categories firing for the customer", 10.0),
    ("amount_at_risk", "Value of the supporting transactions (USD)", 10.0),
    ("evidence_volume", "Number of supporting transactions", 5.0),
    ("pattern_repetition", "Times the pattern has recurred while the alert was open", 5.0),
    ("network_exposure", "Exposure to counterparties with open alerts or confirmed cases", 7.0),
    ("identity_device_signals", "Device / identity signals firing for the customer", 5.0),
    ("prior_confirmed_outcomes", "Customer's earlier alerts confirmed suspicious", 5.0),
    ("no_prior_clearance", "No earlier clearance of the same detector for this customer", 5.0),
    ("alert_age", "Age of the unresolved alert (older work rises)", 5.0),
)
MAX_POINTS = {f[0]: f[2] for f in FACTORS}
PRIORITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
PRIORITY_RANK = {"CRITICAL": 3, "HIGH": 2, "MEDIUM": 1, "LOW": 0}


@dataclass
class TriageInputs:
    severity: str
    customer_score: float
    tier: str  # standalone | supporting | context
    categories: int = 0  # distinct risk categories with points > 0 for the customer
    amount_usd: float | None = None  # None = the alert carries no transaction evidence
    txn_count: int = 0
    occurrence_count: int = 1
    network_points: float = 0.0
    identity_signals: int = 0
    prior_confirmed: int = 0
    prior_cleared_same_detector: int = 0
    age_hours: float | None = None  # None = not applicable (resolved alerts are frozen)


@dataclass
class TriageResult:
    score: float
    priority: str
    factors: list[dict[str, Any]]


def _tier_lookup(table: list[list[float]], value: float, cap: float) -> float:
    """`table` is ascending [[upper_bound_exclusive, points], ...]; values above every bound get `cap`."""
    for bound, pts in table:
        if value < bound:
            return min(pts, cap)
    return cap


def priority_for(score: float, thresholds: dict[str, float]) -> str:
    if score >= thresholds["critical"]:
        return "CRITICAL"
    if score >= thresholds["high"]:
        return "HIGH"
    if score >= thresholds["medium"]:
        return "MEDIUM"
    return "LOW"


def compute_triage(i: TriageInputs, cfg: Any) -> TriageResult:
    """Score one alert. `cfg` is a `TriageConfig`. Pure function: same inputs and config give the same result."""
    out: list[dict[str, Any]] = []

    def add(fid: str, pts: float, value: Any, why: str) -> None:
        mx = MAX_POINTS[fid]
        label = next(f[1] for f in FACTORS if f[0] == fid)
        out.append({"id": fid, "label": label, "points": round(max(0.0, min(pts, mx)), 2), "max": mx,
                    "value": value, "reason": why})

    sev = cfg.severity_points.get(i.severity, 0.0)
    add("detector_severity", sev, i.severity, f"{i.severity} severity detector")
    add("customer_risk_score", i.customer_score / 100.0 * MAX_POINTS["customer_risk_score"], round(i.customer_score, 1),
        f"customer score {i.customer_score:.1f}/100 scaled to {MAX_POINTS['customer_risk_score']:.0f} points")
    add("detector_tier", cfg.tier_points.get(i.tier, 0.0), i.tier,
        "typology detector that alerts on its own" if i.tier == "standalone"
        else "baseline deviation that alerts only when the customer score is elevated")
    cat_pts = {0: 0.0, 1: 2.0, 2: 5.0, 3: 8.0}.get(i.categories, 10.0)
    add("corroborating_categories", cat_pts, i.categories, f"{i.categories} signal categor{'y' if i.categories == 1 else 'ies'} firing")
    if i.amount_usd is None:
        add("amount_at_risk", 0.0, None, "no transaction evidence attached to this alert")
    else:
        add("amount_at_risk", _tier_lookup(cfg.amount_tiers, i.amount_usd, MAX_POINTS["amount_at_risk"]),
            round(i.amount_usd, 2), f"USD {i.amount_usd:,.0f} across the supporting transactions")
    ev = 0.0 if i.txn_count == 0 else 1.0 if i.txn_count == 1 else 3.0 if i.txn_count < 5 else 5.0
    add("evidence_volume", ev, i.txn_count, f"{i.txn_count} supporting transaction(s)")
    rep = 1.0 if i.occurrence_count <= 1 else 3.0 if i.occurrence_count <= 3 else 5.0
    add("pattern_repetition", rep, i.occurrence_count, f"seen {i.occurrence_count} time(s)")
    add("network_exposure", min(i.network_points / cfg.network_points_full, 1.0) * MAX_POINTS["network_exposure"],
        round(i.network_points, 2),
        "no flagged counterparties" if i.network_points <= 0 else f"network-exposure signal of {i.network_points:.1f} points")
    ident = 0.0 if i.identity_signals == 0 else 3.0 if i.identity_signals == 1 else 5.0
    add("identity_device_signals", ident, i.identity_signals, f"{i.identity_signals} device/identity signal(s)")
    add("prior_confirmed_outcomes", 5.0 if i.prior_confirmed > 0 else 0.0, i.prior_confirmed,
        f"{i.prior_confirmed} earlier alert(s) for this customer confirmed suspicious" if i.prior_confirmed
        else "no earlier confirmed alert for this customer")
    add("no_prior_clearance", 0.0 if i.prior_cleared_same_detector > 0 else 5.0, i.prior_cleared_same_detector,
        f"this detector was cleared {i.prior_cleared_same_detector} time(s) before for the customer"
        if i.prior_cleared_same_detector else "no earlier clearance of this detector for the customer")
    if i.age_hours is None:
        add("alert_age", 0.0, None, "not applicable (alert resolved)")
    else:
        pts = 0.0
        for hours, p in cfg.age_hours:
            if i.age_hours >= hours:
                pts = p
        add("alert_age", pts, round(i.age_hours, 1), f"unresolved for {i.age_hours:.1f} h")
    score = round(sum(f["points"] for f in out), 1)
    return TriageResult(score=score, priority=priority_for(score, cfg.thresholds), factors=out)
