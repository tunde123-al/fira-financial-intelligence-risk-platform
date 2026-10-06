"""Domain models for monitoring alerts, cases and monitoring runs."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AlertStatus = Literal["NEW", "TRIAGED", "INVESTIGATING", "ESCALATED", "RESOLVED"]
Resolution = Literal["CLEARED", "FALSE_POSITIVE", "CONFIRMED_SUSPICIOUS"]
CaseStatus = Literal["OPEN", "INVESTIGATING", "ESCALATED", "PENDING_REVIEW", "CLOSED"]
Decision = Literal["CLEARED", "FALSE_POSITIVE", "CONFIRMED_SUSPICIOUS", "ESCALATED"]
Severity = Literal["low", "medium", "high", "critical"]
Priority = Literal["low", "medium", "high", "critical"]
EvidenceClass = Literal["DATABASE_FACT", "RULE_RESULT", "GRAPH_RESULT", "DOCUMENT_EVIDENCE",
                        "LLM_GENERATED_SUMMARY"]

ID_PATTERNS = {"alert": r"^MAL-[0-9A-F]{6,16}$", "case": r"^CASE-[0-9A-F]{6,16}$"}


class _M(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DetectionResult(_M):
    """Structured outcome of one detector for one customer and window.

    `confidence` is deliberately absent: the engine's confidence constants are heuristics,
    not calibrated probabilities.
    """

    detector_id: str
    name: str
    category: str
    triggered: bool
    severity: Severity | None = None
    risk_contribution: float = 0.0
    reason: str = ""
    transaction_ids: list[str] = Field(default_factory=list)
    customer_id: str
    account_ids: list[str] = Field(default_factory=list)
    observed: float | str | None = None
    baseline: float | str | None = None
    threshold: float | str | None = None
    unit: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    data_quality: list[str] = Field(default_factory=list)

    def explain(self) -> dict[str, Any]:
        """Human-readable, evidence-backed explanation (no model involved)."""
        return {
            "detector": self.name, "category": self.category, "triggered": self.triggered,
            "summary": self.reason,
            "observed": self.observed, "baseline": self.baseline, "threshold": self.threshold, "unit": self.unit,
            "window": [self.window_start.isoformat() if self.window_start else None,
                       self.window_end.isoformat() if self.window_end else None],
            "supporting_transactions": self.transaction_ids, "risk_points": self.risk_contribution,
            "data_quality": self.data_quality,
        }


class MonitoringAlert(_M):
    alert_id: str
    run_id: str | None = None
    customer_id: str
    account_id: str | None = None
    transaction_id: str | None = None
    detector_id: str
    alert_type: str
    category: str
    severity: Severity
    risk_score: float
    risk_contribution: float
    status: AlertStatus = "NEW"
    description: str
    explanation: dict[str, Any] = Field(default_factory=dict)
    occurrence_count: int = 1
    window_start: datetime | None = None
    window_end: datetime | None = None
    triggered_at: datetime
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime
    assigned_to: str | None = None
    case_id: str | None = None
    resolution: Resolution | None = None
    resolution_reason: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    transaction_ids: list[str] = Field(default_factory=list)
    triage_score: float | None = None
    triage_priority: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"] | None = None
    triage_factors: list[dict[str, Any]] = Field(default_factory=list)
    triage_computed_at: datetime | None = None


class AlertEvent(_M):
    id: int | None = None
    alert_id: str
    ts: datetime
    actor: str | None = None
    event_type: str
    from_status: str | None = None
    to_status: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class Case(_M):
    case_id: str
    case_number: str = ""
    customer_id: str
    status: CaseStatus = "OPEN"
    priority: Priority = "medium"
    title: str
    assigned_to: str | None = None
    investigation_id: str | None = None
    opened_at: datetime
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    decision: Decision | None = None
    decision_reason: str | None = None
    decided_by: str | None = None
    created_by: str | None = None


class CaseEvent(_M):
    id: int | None = None
    case_id: str
    ts: datetime
    actor: str | None = None
    event_type: str
    from_status: str | None = None
    to_status: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class CaseNote(_M):
    note_id: str
    case_id: str
    author: str
    body: str
    created_at: datetime


class CaseEvidence(_M):
    evidence_id: str
    case_id: str
    alert_id: str | None = None
    transaction_id: str | None = None
    document_id: str | None = None
    chunk_id: str | None = None
    evidence_class: EvidenceClass
    title: str
    content: dict[str, Any] = Field(default_factory=dict)
    source: str
    added_by: str
    created_at: datetime


class MonitoringRun(_M):
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    mode: str = "batch"
    window_start: datetime | None = None
    window_end: datetime | None = None
    lookback_days: int = 30
    config_version: str = ""
    config_fingerprint: str = ""
    customers_evaluated: int = 0
    transactions_in_scope: int = 0
    alerts_created: int = 0
    alerts_updated: int = 0
    alerts_suppressed: int = 0
    errors: int = 0
    duration_ms: int = 0
    triggered_by: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class IngestionBatch(_M):
    """Accounting for one batch of incoming transactions (written once, when the batch is finished)."""

    batch_id: str
    source: str
    status: Literal["COMPLETED", "FAILED"] = "COMPLETED"
    started_at: datetime
    finished_at: datetime
    created_by: str | None = None
    expected_count: int | None = None
    received: int = 0
    processed: int = 0
    rejected: int = 0
    duplicates: int = 0
    malformed: int = 0
    late: int = 0
    failed: int = 0
    missing: int | None = None
    ids_generated: int = 0
    customers_affected: int = 0
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class RejectedRow(_M):
    id: int | None = None
    batch_id: str
    row_number: int
    transaction_id: str | None = None
    reason_code: str
    reason_group: Literal["malformed", "duplicate", "invalid", "referential"]
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ConfigChange(_M):
    id: int | None = None
    ts: datetime
    config_name: str
    path: str
    old_value: Any = None
    new_value: Any = None
    changed_by: str
    reason: str | None = None
    source: str
