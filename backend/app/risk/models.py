"""Risk signal and assessment models."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["low", "medium", "high"]


class EvidenceRef(BaseModel):
    kind: Literal["transaction", "account", "device", "customer", "merchant", "alert", "investigation", "cycle"]
    id: str
    note: str | None = None


class RiskSignal(BaseModel):
    signal_id: str
    signal_type: str
    entity_type: str
    entity_id: str
    observed_value: float | str | None
    baseline_value: float | str | None
    threshold: float | str | None
    unit: str
    severity: Severity
    strength: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    description: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    timestamp: datetime
    window_start: datetime
    window_end: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class NotEvaluated(BaseModel):
    signal_type: str
    reason: str


class Contribution(BaseModel):
    signal_type: str
    group: str | None
    weight: float
    strength: float
    raw_points: float
    points: float
    capped: bool = False


class CategoryPoints(BaseModel):
    category: str
    label: str
    points: float
    signals: list[str] = Field(default_factory=list)


class RiskAssessment(BaseModel):
    entity_type: str
    entity_id: str
    customer_id: str | None
    score: float
    band: str
    flagged: bool
    investigation_threshold: float
    contributors: list[Contribution]
    category_breakdown: list[CategoryPoints] = Field(default_factory=list)
    signals: list[RiskSignal]
    not_evaluated: list[NotEvaluated]
    metrics: dict[str, dict[str, Any]] = Field(default_factory=dict)
    baseline: dict[str, Any] = Field(default_factory=dict)
    window: dict[str, Any] = Field(default_factory=dict)
    data_quality: list[str] = Field(default_factory=list)
    config_version: str
    config_fingerprint: str
    computed_at: datetime
    window_start: datetime
    window_end: datetime
    baseline_start: datetime

    def signal_types(self) -> list[str]:
        return [s.signal_type for s in self.signals]
