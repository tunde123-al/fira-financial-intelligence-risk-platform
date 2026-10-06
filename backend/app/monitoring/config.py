"""Monitoring configuration (YAML) with validation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_PATH = Path(__file__).with_name("monitoring_config.yaml")


class AlertingConfig(BaseModel):
    # Explicit list of detectors that alert on their own. Null = the catalogue's "standalone" tier.
    detectors: list[str] | None = None
    # Supporting detectors alert only when the customer's combined score reaches this value.
    # Null = the risk configuration's investigation threshold (default 40).
    supporting_min_customer_score: float | None = Field(default=None, ge=0, le=100)
    min_detector_points: float = Field(default=0.0, ge=0)
    # Detectors switched off for alerting (they still contribute to the customer score). Governed: see governance.py.
    disabled_detectors: list[str] = Field(default_factory=list)
    critical_band: str = "critical"


class DedupConfig(BaseModel):
    resolved_cooldown_days: int = Field(default=7, ge=0, le=365)


class CasesConfig(BaseModel):
    priority_by_severity: dict[str, str] = Field(
        default_factory=lambda: {"low": "low", "medium": "medium", "high": "high", "critical": "critical"})


class DataQualityConfig(BaseModel):
    """Validation rules of the ingestion gate (see app.monitoring.ingest.QualityRules)."""

    transaction_types: list[str] = Field(default_factory=lambda: [
        "bill_payment", "card_payment", "cash_withdrawal", "deposit", "salary", "transfer"])
    channels: list[str] = Field(default_factory=lambda: ["web", "mobile", "pos", "atm", "branch", "api"])
    statuses: list[str] = Field(default_factory=lambda: ["completed", "failed", "reversed", "pending"])
    max_age_days: int = Field(default=3650, ge=1, le=36500)
    max_future_hours: int = Field(default=24, ge=0, le=720)
    late_after_hours: int = Field(default=24, ge=0, le=8760)
    likely_duplicate_window_seconds: int = Field(default=60, ge=0, le=3600)
    duplicate_policy: Literal["reject", "accept"] = "reject"
    reject_closed_accounts: bool = True

    def to_rules(self) -> Any:
        from app.monitoring.ingest import QualityRules

        return QualityRules(**self.model_dump())


class TriageConfig(BaseModel):
    """Thresholds and lookup tables of the heuristic triage score (see app.monitoring.triage)."""

    enabled: bool = True
    # score >= value -> that band; below `medium` is LOW
    thresholds: dict[str, float] = Field(default_factory=lambda: {"critical": 65.0, "high": 50.0, "medium": 30.0})
    severity_points: dict[str, float] = Field(
        default_factory=lambda: {"low": 3.0, "medium": 7.0, "high": 11.0, "critical": 13.0})
    tier_points: dict[str, float] = Field(default_factory=lambda: {"standalone": 5.0, "supporting": 4.0, "context": 0.0})
    # ascending [upper bound (exclusive, USD), points]; larger amounts get the factor maximum
    amount_tiers: list[list[float]] = Field(default_factory=lambda: [[1000.0, 2.0], [5000.0, 4.0], [10000.0, 6.0], [50000.0, 8.0]])
    network_points_full: float = Field(default=10.0, gt=0)
    # ascending [age in hours, points] for unresolved alerts
    age_hours: list[list[float]] = Field(default_factory=lambda: [[24.0, 1.0], [48.0, 3.0], [96.0, 5.0]])
    # Review-time targets used only to flag "overdue" in dashboards. They are assumptions, not regulatory SLAs.
    overdue_hours: dict[str, float] = Field(
        default_factory=lambda: {"CRITICAL": 4.0, "HIGH": 24.0, "MEDIUM": 72.0, "LOW": 168.0})

    @field_validator("thresholds")
    @classmethod
    def _thresholds(cls, v: dict[str, float]) -> dict[str, float]:
        if set(v) != {"critical", "high", "medium"}:
            raise ValueError("thresholds must define exactly critical, high and medium")
        if not (0 < v["medium"] < v["high"] < v["critical"] <= 100):
            raise ValueError("thresholds must satisfy 0 < medium < high < critical <= 100")
        return v

    @model_validator(mode="after")
    def _tables(self) -> TriageConfig:
        for name in ("amount_tiers", "age_hours"):
            rows = getattr(self, name)
            if any(len(r) != 2 for r in rows) or [r[0] for r in rows] != sorted(r[0] for r in rows):
                raise ValueError(f"{name} must be ascending [bound, points] pairs")
        return self


class MuleConfig(BaseModel):
    """Thresholds of the money-mule indicator view (see app.monitoring.mule)."""

    window_days: int = Field(default=30, ge=1, le=365)
    fan_in_min: int = Field(default=5, ge=2, le=1000)
    fan_out_min: int = Field(default=5, ge=2, le=1000)
    rapid_hours: float = Field(default=24.0, gt=0, le=720)
    rapid_share: float = Field(default=0.7, gt=0, lt=1)
    retention_max: float = Field(default=0.15, ge=0, le=1)
    min_usd: float = Field(default=1000.0, ge=0)
    new_account_days: int = Field(default=30, ge=1, le=3650)
    bands: dict[str, float] = Field(default_factory=lambda: {"high": 60.0, "medium": 35.0, "low": 15.0})
    candidate_limit: int = Field(default=200, ge=1, le=2000)

    @field_validator("bands")
    @classmethod
    def _bands(cls, v: dict[str, float]) -> dict[str, float]:
        if set(v) != {"high", "medium", "low"} or not (0 < v["low"] < v["medium"] < v["high"] <= 100):
            raise ValueError("bands must satisfy 0 < low < medium < high <= 100")
        return v


class MonitoringConfig(BaseModel):
    version: str = "monitoring-1"
    lookback_days: int = Field(default=30, ge=1, le=365)
    baseline_days: int = Field(default=90, ge=7, le=730)
    active_days: int = Field(default=1, ge=1, le=90)
    alerting: AlertingConfig = Field(default_factory=AlertingConfig)
    deduplication: DedupConfig = Field(default_factory=DedupConfig)
    cases: CasesConfig = Field(default_factory=CasesConfig)
    data_quality: DataQualityConfig = Field(default_factory=DataQualityConfig)
    triage: TriageConfig = Field(default_factory=TriageConfig)
    mule: MuleConfig = Field(default_factory=MuleConfig)

    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(exclude={"version"}), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    def standalone_detectors(self) -> set[str]:
        from app.risk.catalog import CATALOG

        off = set(self.alerting.disabled_detectors)
        if self.alerting.detectors is not None:
            return {d for d in self.alerting.detectors if d in CATALOG} - off
        return {d for d, info in CATALOG.items() if info.tier == "standalone"} - off

    def supporting_detectors(self) -> set[str]:
        from app.risk.catalog import CATALOG

        return ({d for d, info in CATALOG.items() if info.tier == "supporting"} - self.standalone_detectors()
                - set(self.alerting.disabled_detectors))

    def alerting_detectors(self) -> set[str]:
        """Every detector that can create an alert (standalone, or supporting when the customer is flagged)."""
        return self.standalone_detectors() | self.supporting_detectors()

    def alert_tier(self, detector_id: str) -> str:
        if detector_id in self.standalone_detectors():
            return "standalone"
        if detector_id in self.supporting_detectors():
            return "supporting"
        return "context"


def load_monitoring_config(path: Path | None = None) -> MonitoringConfig:
    p = Path(path) if path else DEFAULT_PATH
    data: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return MonitoringConfig(**data)
