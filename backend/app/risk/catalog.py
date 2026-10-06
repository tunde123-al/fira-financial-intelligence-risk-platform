"""Detector catalogue: metadata for every risk signal.

The detection logic lives in `RiskEngine`; this module only describes each detector
(name, category, default severity, whether it may raise an alert on its own) so the
monitoring layer, the API and the UI can present them consistently.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DetectorInfo:
    detector_id: str
    name: str
    description: str
    category: str
    default_severity: str = "medium"
    # standalone: a typology detector that raises an alert on its own.
    # supporting: a deviation from the customer's own baseline that is common in benign behaviour (new device,
    #             new country, large purchase); it raises an alert only when the customer's combined risk score
    #             reaches the investigation threshold.
    # context:    contributes to the score, never alerts
    tier: str = "standalone"

    @property
    def alerting(self) -> bool:
        return self.tier != "context"


CATEGORIES = {
    "transaction_behaviour": "Transaction behaviour",
    "fund_flow": "Fund flow",
    "geographic": "Geographic",
    "device_identity": "Device and identity",
    "network": "Network",
    "history": "Historical alerts",
    "anomaly_model": "Anomaly model",
}

_D = DetectorInfo
CATALOG: dict[str, DetectorInfo] = {d.detector_id: d for d in [
    _D("TRANSACTION_BURST", "Transaction burst", "Many outbound transactions within a short window", "transaction_behaviour", "medium"),
    _D("VELOCITY_SPIKE", "Velocity spike", "Daily outbound count far above the customer's baseline", "transaction_behaviour", "medium", tier="supporting"),
    _D("AMOUNT_DEVIATION", "Unusual amount", "Largest outbound amount far above the customer's baseline", "transaction_behaviour", "medium", tier="supporting"),
    _D("PEER_AMOUNT_DEVIATION", "Unusual amount vs peers", "Largest outbound amount far above the peer segment", "transaction_behaviour", "medium", tier="supporting"),
    _D("STRUCTURING", "Structuring", "Repeated transactions just below a reporting threshold within a short window", "transaction_behaviour", "high"),
    _D("HIGH_RISK_MERCHANT", "High-risk merchant", "Payments to merchants rated high-risk", "transaction_behaviour", "medium"),
    _D("DORMANT_REACTIVATION", "Dormant account activation", "Long-inactive account suddenly moves value", "transaction_behaviour", "high"),
    _D("BEHAVIOURAL_SHIFT", "Behavioural shift", "Transaction type/channel mix diverges from baseline", "transaction_behaviour", "low", tier="context"),
    _D("RAPID_PASS_THROUGH", "Rapid movement of funds", "Inbound value leaves again within hours", "fund_flow", "high"),
    _D("FAN_IN", "Fan-in", "Many distinct senders into one customer", "fund_flow", "high"),
    _D("FAN_OUT", "Fan-out", "Many distinct beneficiaries paid by one customer", "fund_flow", "medium"),
    _D("CIRCULAR_FLOW", "Circular flow", "Funds return to the origin through a time-ordered cycle", "fund_flow", "high"),
    _D("GEO_NEW_COUNTRY", "New country", "Activity in countries not seen in the baseline", "geographic", "medium", tier="supporting"),
    _D("IMPOSSIBLE_TRAVEL", "Impossible travel", "Card-present transactions implying impossible speed", "geographic", "high"),
    _D("NEW_DEVICE", "New device", "Value moved from a device not seen in the baseline", "device_identity", "medium", tier="supporting"),
    _D("DEVICE_SHARING", "Device sharing", "One device used by several customers", "device_identity", "high"),
    _D("SHARED_IDENTIFIER", "Shared identifier", "Phone, e-mail or address shared across customers", "device_identity", "low", tier="supporting"),
    _D("NETWORK_EXPOSURE", "Network exposure", "Counterparties with open alerts or confirmed cases", "network", "low", tier="context"),
    _D("HISTORICAL_ALERTS", "Historical alerts", "Alerts raised before the window", "history", "low", tier="context"),
    _D("ML_ANOMALY", "ML anomaly", "Isolation-forest anomaly percentile", "anomaly_model", "low", tier="context"),
]}


def category_of(signal_type: str) -> str:
    info = CATALOG.get(signal_type)
    return info.category if info else "other"


def info(detector_id: str) -> DetectorInfo | None:
    return CATALOG.get(detector_id)
