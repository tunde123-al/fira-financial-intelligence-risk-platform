"""Core domain schemas shared by the API, the tools and the agent."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EntityType = Literal["customer", "account", "transaction", "merchant", "device"]
ID_PATTERNS: dict[str, str] = {
    "customer": r"^CUST-\d{1,10}$",
    "account": r"^ACC-\d{1,10}$",
    "transaction": r"^TXN-\d{1,12}$",
    "merchant": r"^MER-\d{1,10}$",
    "device": r"^DEV-\d{1,10}$",
    "investigation": r"^INV-[A-Z0-9-]{1,40}$",
    "alert": r"^ALR-\d{1,10}$",
}


class _Model(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Customer(_Model):
    customer_id: str
    customer_type: str
    segment: str
    created_at: datetime
    country: str
    home_city: str | None = None
    home_latitude: float | None = None
    home_longitude: float | None = None
    risk_profile: str
    status: str
    kyc_level: int | None = None


class CustomerIdentifier(_Model):
    identifier_type: str
    masked_value: str
    value_hash: str | None = None  # never returned through the API unless explicitly required


class Account(_Model):
    account_id: str
    customer_id: str
    account_type: str
    currency: str
    opened_at: datetime
    status: str
    balance: float


class Merchant(_Model):
    merchant_id: str
    name: str
    category: str
    country: str
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    risk_profile: str


class Device(_Model):
    device_id: str
    device_type: str
    fingerprint: str
    first_seen: datetime | None = None
    last_seen: datetime | None = None


class Transaction(_Model):
    transaction_id: str
    timestamp: datetime
    sender_account_id: str | None = None
    receiver_account_id: str | None = None
    merchant_id: str | None = None
    amount: float
    currency: str
    amount_usd: float | None = None
    transaction_type: str
    channel: str
    country: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    device_id: str | None = None
    ip_address: str | None = None
    status: str
    external_counterparty: str | None = None


class Alert(_Model):
    alert_id: str
    entity_id: str
    entity_type: str
    alert_type: str
    severity: str
    risk_score: float | None = None
    created_at: datetime
    status: str


InvestigationStatus = Literal["open", "in_progress", "pending_review", "closed", "needs_input", "failed"]
Conclusion = Literal["confirmed_suspicious", "legitimate", "escalated", "insufficient_evidence",
                     "more_evidence_requested"]


class Investigation(_Model):
    investigation_id: str
    subject_id: str
    subject_type: str
    assigned_to: str | None = None
    status: str
    created_at: datetime
    closed_at: datetime | None = None
    conclusion: str | None = None
    request_text: str | None = None
    risk_score: float | None = None
    created_by: str | None = None
    signals: list[str] = Field(default_factory=list)
    summary: str | None = None
    report: dict[str, Any] | None = None


EvidenceSource = Literal["database", "metric", "graph", "document", "ml", "history"]


class EvidenceItem(_Model):
    """A single piece of evidence. `ref` (E1, E2, ...) is what reports cite."""

    evidence_id: str
    investigation_id: str | None = None
    ref: str
    source_type: EvidenceSource
    source_id: str
    evidence_type: str
    title: str
    content: dict[str, Any]
    confidence: float = Field(ge=0.0, le=1.0)
    created_at: datetime | None = None


class HumanDecision(_Model):
    decision_id: str
    investigation_id: str
    decided_by: str
    decision: Literal["confirm", "reject", "escalate", "request_more_evidence"]
    rationale: str
    failure_categories: list[str] = Field(default_factory=list)
    report_quality: int | None = Field(default=None, ge=1, le=5)
    created_at: datetime


class AuditEvent(_Model):
    id: int | None = None
    ts: datetime
    user_id: str | None = None
    role: str | None = None
    action: str
    tool: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    result: str
    request_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class SearchHit(_Model):
    entity_type: str
    entity_id: str
    label: str
    extra: dict[str, Any] = Field(default_factory=dict)
