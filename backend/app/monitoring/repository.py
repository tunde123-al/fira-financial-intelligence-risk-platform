"""Persistence for monitoring alerts, cases, notes, evidence and runs.

Two implementations behind one interface:

* `MemoryMonitoringRepo` - process memory (Frames mode, tests). Lost on restart.
* `SqlMonitoringRepo`    - PostgreSQL (migration 0003). Constraints and the unresolved-alert
  unique index live in the database; both implementations enforce the same rules.

All SQL uses bound parameters. Dynamic SQL is limited to whitelisted column names and
fixed fragments.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.data.store import utcnow
from app.monitoring.lifecycle import UNRESOLVED
from app.monitoring.models import (
    AlertEvent,
    Case,
    CaseEvent,
    CaseEvidence,
    CaseNote,
    ConfigChange,
    IngestionBatch,
    MonitoringAlert,
    MonitoringRun,
    RejectedRow,
)


class DuplicateUnresolvedAlert(Exception):
    """An unresolved alert for (customer, detector) already exists."""


class DuplicateOpenCase(Exception):
    """The customer already has an unclosed case."""


SORTABLE = {"triggered_at", "risk_score", "severity_rank", "status", "customer_id", "detector_id", "updated_at",
            "triage_score"}


@dataclass
class AlertFilter:
    statuses: list[str] | None = None
    severities: list[str] | None = None
    detector: str | None = None
    min_risk: float | None = None
    max_risk: float | None = None
    customer_id: str | None = None
    assigned_to: str | None = None  # a user id, or "unassigned"
    date_from: datetime | None = None
    date_to: datetime | None = None
    q: str | None = None
    case_id: str | None = None
    priorities: list[str] | None = None
    sort: str = "triggered_at"
    order: str = "desc"
    limit: int = 50
    offset: int = 0


@dataclass
class CaseFilter:
    statuses: list[str] | None = None
    priority: str | None = None
    customer_id: str | None = None
    assigned_to: str | None = None
    q: str | None = None
    limit: int = 50
    offset: int = 0


SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
ALERT_UPDATABLE = {
    "status", "assigned_to", "case_id", "resolution", "resolution_reason", "resolved_at", "resolved_by",
    "severity", "risk_score", "risk_contribution", "description", "explanation", "occurrence_count",
    "window_start", "window_end", "last_seen_at", "updated_at", "account_id", "transaction_id",
    "triage_score", "triage_priority", "triage_factors", "triage_computed_at",
}
CASE_UPDATABLE = {
    "status", "priority", "title", "assigned_to", "investigation_id", "updated_at", "closed_at", "decision",
    "decision_reason", "decided_by",
}
JSON_FIELDS = {"explanation", "detail", "content", "details", "triage_factors", "payload", "old_value",
               "new_value"}


class MonitoringRepo(Protocol):
    def transaction(self) -> Any: ...
    def create_alert(self, alert: MonitoringAlert) -> MonitoringAlert: ...
    def get_alert(self, alert_id: str) -> MonitoringAlert | None: ...
    def find_unresolved(self, customer_id: str, detector_id: str) -> MonitoringAlert | None: ...
    def resolved_alert_transactions(self, customer_id: str, detector_id: str) -> set[str]: ...
    def last_resolved_at(self, customer_id: str, detector_id: str) -> datetime | None: ...
    def update_alert(self, alert_id: str, **fields: Any) -> MonitoringAlert | None: ...
    def add_alert_transactions(self, alert_id: str, transaction_ids: list[str]) -> None: ...
    def list_alerts(self, f: AlertFilter) -> tuple[list[MonitoringAlert], int]: ...
    def add_alert_event(self, ev: AlertEvent) -> None: ...
    def alert_events(self, alert_id: str) -> list[AlertEvent]: ...
    def create_case(self, case: Case) -> Case: ...
    def get_case(self, case_id: str) -> Case | None: ...
    def find_open_case(self, customer_id: str) -> Case | None: ...
    def list_cases(self, f: CaseFilter) -> tuple[list[Case], int]: ...
    def update_case(self, case_id: str, **fields: Any) -> Case | None: ...
    def alerts_for_case(self, case_id: str) -> list[MonitoringAlert]: ...
    def add_case_event(self, ev: CaseEvent) -> None: ...
    def case_events(self, case_id: str) -> list[CaseEvent]: ...
    def add_note(self, note: CaseNote) -> None: ...
    def notes(self, case_id: str) -> list[CaseNote]: ...
    def add_case_evidence(self, ev: CaseEvidence) -> None: ...
    def case_evidence(self, case_id: str) -> list[CaseEvidence]: ...
    def save_run(self, run: MonitoringRun) -> None: ...
    def list_runs(self, limit: int = 20) -> list[MonitoringRun]: ...
    def kpis(self) -> dict[str, Any]: ...
    def save_batch(self, batch: IngestionBatch, rejected: list[RejectedRow]) -> None: ...
    def get_batch(self, batch_id: str) -> IngestionBatch | None: ...
    def list_batches(self, limit: int = 50, offset: int = 0) -> tuple[list[IngestionBatch], int]: ...
    def list_rejected(self, batch_id: str | None = None, reason_code: str | None = None,
                      reason_group: str | None = None, limit: int = 50, offset: int = 0) -> tuple[list[RejectedRow], int]: ...
    def quality_totals(self) -> dict[str, Any]: ...
    def add_config_change(self, change: ConfigChange) -> None: ...
    def list_config_changes(self, limit: int = 100) -> list[ConfigChange]: ...
    def last_config_snapshot(self, name: str) -> dict[str, Any] | None: ...
    def resolved_alerts(self, since: datetime | None = None, limit: int = 1000) -> list[MonitoringAlert]: ...
    def all_alerts(self) -> list[MonitoringAlert]: ...
    def ping(self) -> bool: ...


def _matches_q(text: str, q: str) -> bool:
    return q.lower() in text.lower()


# ----------------------------------------------------------------------------- memory
class MemoryMonitoringRepo:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._alerts: dict[str, MonitoringAlert] = {}
        self._events: list[AlertEvent] = []
        self._cases: dict[str, Case] = {}
        self._case_events: list[CaseEvent] = []
        self._notes: list[CaseNote] = []
        self._evidence: list[CaseEvidence] = []
        self._runs: list[MonitoringRun] = []
        self._case_seq = 0
        self._batches: list[IngestionBatch] = []
        self._rejected: list[RejectedRow] = []
        self._config_changes: list[ConfigChange] = []
        self._config_snapshots: dict[str, dict[str, Any]] = {}

    def ping(self) -> bool:
        return True

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialises the steps of one workflow operation. The in-memory repository has no rollback."""
        with self._lock:
            yield

    # ---- alerts
    def create_alert(self, alert: MonitoringAlert) -> MonitoringAlert:
        with self._lock:
            if self.find_unresolved(alert.customer_id, alert.detector_id) is not None:
                raise DuplicateUnresolvedAlert(f"{alert.customer_id}/{alert.detector_id}")
            self._alerts[alert.alert_id] = alert.model_copy(deep=True)
        return alert

    def get_alert(self, alert_id: str) -> MonitoringAlert | None:
        with self._lock:
            a = self._alerts.get(alert_id)
            return a.model_copy(deep=True) if a else None

    def find_unresolved(self, customer_id: str, detector_id: str) -> MonitoringAlert | None:
        with self._lock:
            for a in self._alerts.values():
                if a.customer_id == customer_id and a.detector_id == detector_id and a.status in UNRESOLVED:
                    return a.model_copy(deep=True)
        return None

    def resolved_alert_transactions(self, customer_id: str, detector_id: str) -> set[str]:
        with self._lock:
            out: set[str] = set()
            for a in self._alerts.values():
                if a.customer_id == customer_id and a.detector_id == detector_id and a.status == "RESOLVED":
                    out.update(a.transaction_ids)
            return out

    def last_resolved_at(self, customer_id: str, detector_id: str) -> datetime | None:
        with self._lock:
            ts = [a.resolved_at for a in self._alerts.values()
                  if a.customer_id == customer_id and a.detector_id == detector_id and a.resolved_at]
            return max(ts) if ts else None

    def update_alert(self, alert_id: str, **fields: Any) -> MonitoringAlert | None:
        with self._lock:
            a = self._alerts.get(alert_id)
            if a is None:
                return None
            bad = set(fields) - ALERT_UPDATABLE
            if bad:
                raise ValueError(f"fields not updatable: {sorted(bad)}")
            self._alerts[alert_id] = a.model_copy(update=fields, deep=True)
            return self._alerts[alert_id].model_copy(deep=True)

    def add_alert_transactions(self, alert_id: str, transaction_ids: list[str]) -> None:
        with self._lock:
            a = self._alerts[alert_id]
            merged = list(dict.fromkeys([*a.transaction_ids, *transaction_ids]))
            self._alerts[alert_id] = a.model_copy(update={"transaction_ids": merged})

    def list_alerts(self, f: AlertFilter) -> tuple[list[MonitoringAlert], int]:
        with self._lock:
            items = list(self._alerts.values())
        if f.statuses:
            items = [a for a in items if a.status in f.statuses]
        if f.severities:
            items = [a for a in items if a.severity in f.severities]
        if f.detector:
            items = [a for a in items if a.detector_id == f.detector]
        if f.min_risk is not None:
            items = [a for a in items if a.risk_score >= f.min_risk]
        if f.max_risk is not None:
            items = [a for a in items if a.risk_score <= f.max_risk]
        if f.priorities:
            items = [a for a in items if a.triage_priority in f.priorities]
        if f.customer_id:
            items = [a for a in items if a.customer_id == f.customer_id]
        if f.case_id:
            items = [a for a in items if a.case_id == f.case_id]
        if f.assigned_to == "unassigned":
            items = [a for a in items if a.assigned_to is None]
        elif f.assigned_to:
            items = [a for a in items if a.assigned_to == f.assigned_to]
        if f.date_from:
            items = [a for a in items if a.triggered_at >= f.date_from]
        if f.date_to:
            items = [a for a in items if a.triggered_at <= f.date_to]
        if f.q:
            items = [a for a in items if _matches_q(f"{a.alert_id} {a.customer_id} {a.detector_id} {a.description}", f.q)]
        total = len(items)
        key = f.sort if f.sort in SORTABLE else "triggered_at"
        if key == "severity_rank":
            items.sort(key=lambda a: SEVERITY_RANK[a.severity], reverse=f.order != "asc")
        elif key == "triage_score":
            items.sort(key=lambda a: (a.triage_score is None, -(a.triage_score or 0.0) if f.order != "asc" else (a.triage_score or 0.0)))
        else:
            items.sort(key=lambda a: getattr(a, key), reverse=f.order != "asc")
        page = items[f.offset: f.offset + f.limit]
        return [a.model_copy(deep=True) for a in page], total

    def add_alert_event(self, ev: AlertEvent) -> None:
        with self._lock:
            self._events.append(ev.model_copy(update={"id": len(self._events) + 1}))

    def alert_events(self, alert_id: str) -> list[AlertEvent]:
        with self._lock:
            return [e.model_copy(deep=True) for e in self._events if e.alert_id == alert_id]

    # ---- cases
    def create_case(self, case: Case) -> Case:
        with self._lock:
            if self.find_open_case(case.customer_id) is not None:
                raise DuplicateOpenCase(case.customer_id)
            self._case_seq += 1
            number = f"FC-{case.created_at:%Y}-{self._case_seq:06d}"
            c = case.model_copy(update={"case_number": number}, deep=True)
            self._cases[c.case_id] = c
            return c.model_copy(deep=True)

    def get_case(self, case_id: str) -> Case | None:
        with self._lock:
            c = self._cases.get(case_id)
            return c.model_copy(deep=True) if c else None

    def find_open_case(self, customer_id: str) -> Case | None:
        with self._lock:
            for c in self._cases.values():
                if c.customer_id == customer_id and c.status != "CLOSED":
                    return c.model_copy(deep=True)
        return None

    def list_cases(self, f: CaseFilter) -> tuple[list[Case], int]:
        with self._lock:
            items = list(self._cases.values())
        if f.statuses:
            items = [c for c in items if c.status in f.statuses]
        if f.priority:
            items = [c for c in items if c.priority == f.priority]
        if f.customer_id:
            items = [c for c in items if c.customer_id == f.customer_id]
        if f.assigned_to == "unassigned":
            items = [c for c in items if c.assigned_to is None]
        elif f.assigned_to:
            items = [c for c in items if c.assigned_to == f.assigned_to]
        if f.q:
            items = [c for c in items if _matches_q(f"{c.case_number} {c.customer_id} {c.title}", f.q)]
        items.sort(key=lambda c: c.updated_at, reverse=True)
        return [c.model_copy(deep=True) for c in items[f.offset: f.offset + f.limit]], len(items)

    def update_case(self, case_id: str, **fields: Any) -> Case | None:
        with self._lock:
            c = self._cases.get(case_id)
            if c is None:
                return None
            bad = set(fields) - CASE_UPDATABLE
            if bad:
                raise ValueError(f"fields not updatable: {sorted(bad)}")
            self._cases[case_id] = c.model_copy(update=fields, deep=True)
            return self._cases[case_id].model_copy(deep=True)

    def alerts_for_case(self, case_id: str) -> list[MonitoringAlert]:
        with self._lock:
            items = [a for a in self._alerts.values() if a.case_id == case_id]
        items.sort(key=lambda a: a.triggered_at)
        return [a.model_copy(deep=True) for a in items]

    def add_case_event(self, ev: CaseEvent) -> None:
        with self._lock:
            self._case_events.append(ev.model_copy(update={"id": len(self._case_events) + 1}))

    def case_events(self, case_id: str) -> list[CaseEvent]:
        with self._lock:
            return [e.model_copy(deep=True) for e in self._case_events if e.case_id == case_id]

    def add_note(self, note: CaseNote) -> None:
        with self._lock:
            self._notes.append(note.model_copy(deep=True))

    def notes(self, case_id: str) -> list[CaseNote]:
        with self._lock:
            return [n.model_copy(deep=True) for n in self._notes if n.case_id == case_id]

    def add_case_evidence(self, ev: CaseEvidence) -> None:
        with self._lock:
            self._evidence.append(ev.model_copy(deep=True))

    def case_evidence(self, case_id: str) -> list[CaseEvidence]:
        with self._lock:
            return [e.model_copy(deep=True) for e in self._evidence if e.case_id == case_id]

    # ---- data quality
    def save_batch(self, batch: IngestionBatch, rejected: list[RejectedRow]) -> None:
        with self._lock:
            self._batches.append(batch.model_copy(deep=True))
            for r in rejected:
                self._rejected.append(r.model_copy(update={"id": len(self._rejected) + 1}, deep=True))

    def get_batch(self, batch_id: str) -> IngestionBatch | None:
        with self._lock:
            for b in self._batches:
                if b.batch_id == batch_id:
                    return b.model_copy(deep=True)
        return None

    def list_batches(self, limit: int = 50, offset: int = 0) -> tuple[list[IngestionBatch], int]:
        with self._lock:
            items = sorted(self._batches, key=lambda b: b.started_at, reverse=True)
        return [b.model_copy(deep=True) for b in items[offset: offset + limit]], len(items)

    def list_rejected(self, batch_id: str | None = None, reason_code: str | None = None,
                      reason_group: str | None = None, limit: int = 50, offset: int = 0) -> tuple[list[RejectedRow], int]:
        with self._lock:
            items = list(self._rejected)
        if batch_id:
            items = [r for r in items if r.batch_id == batch_id]
        if reason_code:
            items = [r for r in items if r.reason_code == reason_code]
        if reason_group:
            items = [r for r in items if r.reason_group == reason_group]
        items.sort(key=lambda r: (r.created_at, r.id or 0), reverse=True)
        return [r.model_copy(deep=True) for r in items[offset: offset + limit]], len(items)

    def quality_totals(self) -> dict[str, Any]:
        with self._lock:
            batches = list(self._batches)
            reasons: dict[str, int] = {}
            for r in self._rejected:
                reasons[r.reason_code] = reasons.get(r.reason_code, 0) + 1
        return build_quality_totals([(b.expected_count, b.received, b.processed, b.rejected, b.duplicates, b.malformed,
                                      b.late, b.failed, b.missing, b.finished_at) for b in batches], reasons)

    # ---- configuration governance
    def add_config_change(self, change: ConfigChange) -> None:
        with self._lock:
            self._config_changes.append(change.model_copy(update={"id": len(self._config_changes) + 1}, deep=True))
            if change.path == "(snapshot)":
                self._config_snapshots[change.config_name] = dict(change.new_value or {})

    def list_config_changes(self, limit: int = 100) -> list[ConfigChange]:
        with self._lock:
            items = sorted(self._config_changes, key=lambda c: (c.ts, c.id or 0), reverse=True)
        return [c.model_copy(deep=True) for c in items[:limit]]

    def last_config_snapshot(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            snap = self._config_snapshots.get(name)
            return dict(snap) if snap is not None else None

    def resolved_alerts(self, since: datetime | None = None, limit: int = 1000) -> list[MonitoringAlert]:
        with self._lock:
            items = [a for a in self._alerts.values() if a.status == "RESOLVED"
                     and (since is None or (a.resolved_at and a.resolved_at >= since))]
        items.sort(key=lambda a: a.resolved_at or a.updated_at, reverse=True)
        return [a.model_copy(deep=True) for a in items[:limit]]

    def all_alerts(self) -> list[MonitoringAlert]:
        with self._lock:
            return [a.model_copy(deep=True) for a in self._alerts.values()]

    # ---- runs & KPIs
    def save_run(self, run: MonitoringRun) -> None:
        with self._lock:
            self._runs = [r for r in self._runs if r.run_id != run.run_id] + [run.model_copy(deep=True)]

    def list_runs(self, limit: int = 20) -> list[MonitoringRun]:
        with self._lock:
            runs = sorted(self._runs, key=lambda r: r.started_at, reverse=True)
        return [r.model_copy(deep=True) for r in runs[:limit]]

    def kpis(self) -> dict[str, Any]:
        with self._lock:
            alerts = list(self._alerts.values())
            cases = list(self._cases.values())
            runs = list(self._runs)
        return build_kpis(
            alert_rows=[(a.status, a.severity, a.detector_id, a.resolution) for a in alerts],
            case_rows=[(c.status, c.opened_at, c.closed_at) for c in cases],
            run_rows=[(r.started_at, r.transactions_in_scope, r.customers_evaluated, r.duration_ms, r.alerts_created)
                      for r in runs])


def build_kpis(alert_rows: list[tuple], case_rows: list[tuple], run_rows: list[tuple]) -> dict[str, Any]:
    """KPIs computed from stored rows only (no fabricated values)."""
    by_status: dict[str, int] = {}
    by_sev_open: dict[str, int] = {}
    by_det: dict[str, int] = {}
    resolved = fp = confirmed = 0
    for status, sev, det, resolution in alert_rows:
        by_status[status] = by_status.get(status, 0) + 1
        by_det[det] = by_det.get(det, 0) + 1
        if status in UNRESOLVED:
            by_sev_open[sev] = by_sev_open.get(sev, 0) + 1
        else:
            resolved += 1
            fp += 1 if resolution in ("FALSE_POSITIVE", "CLEARED") else 0
            confirmed += 1 if resolution == "CONFIRMED_SUSPICIOUS" else 0
    case_by_status: dict[str, int] = {}
    durations: list[float] = []
    for status, opened, closed in case_rows:
        case_by_status[status] = case_by_status.get(status, 0) + 1
        if status == "CLOSED" and opened and closed:
            durations.append((closed - opened).total_seconds())
    last = max(run_rows, key=lambda r: r[0]) if run_rows else None
    cust = sum(r[2] for r in run_rows)
    ms = sum(r[3] for r in run_rows)
    return {
        "alerts_generated": len(alert_rows),
        "alerts_by_status": by_status,
        "open_alerts": sum(by_status.get(s, 0) for s in UNRESOLVED),
        "high_risk_alerts": by_sev_open.get("high", 0) + by_sev_open.get("critical", 0),
        "open_alerts_by_severity": by_sev_open,
        "alerts_by_detector": by_det,
        "cases_by_status": case_by_status,
        "cases_under_investigation": case_by_status.get("OPEN", 0) + case_by_status.get("INVESTIGATING", 0)
        + case_by_status.get("PENDING_REVIEW", 0),
        "cases_escalated": case_by_status.get("ESCALATED", 0),
        "cases_resolved": case_by_status.get("CLOSED", 0),
        "resolved_alerts": resolved,
        # shares of DECIDED alerts (not an FPR: true negatives are unknown in operation, see docs/ALERT_QUALITY.md)
        "false_discovery_rate": round(fp / resolved, 4) if resolved else None,
        "confirmation_rate": round(confirmed / resolved, 4) if resolved else None,
        "monitoring_runs": len(run_rows),
        "transactions_monitored_last_run": last[1] if last else None,
        "customers_evaluated_total": cust,
        "avg_detection_ms_per_customer": round(ms / cust, 2) if cust else None,
        "avg_investigation_hours": round(sum(durations) / len(durations) / 3600, 3) if durations else None,
        "closed_cases_measured": len(durations),
    }


def build_quality_totals(rows: list[tuple], reasons: dict[str, int]) -> dict[str, Any]:
    """Data-quality totals from batch rows (expected, received, processed, rejected, duplicates, malformed, late,
    failed, missing, finished_at). Coverage uses only batches that declared what they expected."""
    exp_batches = [r for r in rows if r[0] is not None]
    expected = sum(r[0] for r in exp_batches)
    received_in_expected = sum(r[1] for r in exp_batches)
    received = sum(r[1] for r in rows)
    processed = sum(r[2] for r in rows)
    rejected = sum(r[3] for r in rows)
    duplicates = sum(r[4] for r in rows)
    malformed = sum(r[5] for r in rows)
    late = sum(r[6] for r in rows)
    failed = sum(r[7] for r in rows)
    missing = sum(r[8] for r in rows if r[8] is not None)
    return {
        "batches": len(rows), "batches_with_expected": len(exp_batches),
        "expected": expected if exp_batches else None, "received": received, "processed": processed,
        "rejected": rejected, "duplicates": duplicates, "malformed": malformed, "late": late, "failed": failed,
        "missing": missing if any(r[8] is not None for r in rows) else None,
        "coverage": round(min(received_in_expected / expected, 1.0), 6) if expected else None,
        "effective_coverage": round(sum(r[2] for r in exp_batches) / expected, 6) if expected else None,
        "processing_success": round(processed / received, 6) if received else None,
        "quality_score": round(100.0 * (processed - late) / received, 2) if received else None,
        "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "last_batch_at": max((r[9] for r in rows), default=None),
    }


# --------------------------------------------------------------------------------- SQL
def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v, default=str)


ALERT_COLS = ("alert_id, run_id, customer_id, account_id, transaction_id, detector_id, alert_type, category, severity, "
              "risk_score, risk_contribution, status, description, explanation, occurrence_count, window_start, "
              "window_end, triggered_at, created_at, updated_at, last_seen_at, assigned_to, case_id, resolution, "
              "resolution_reason, resolved_at, resolved_by, triage_score, triage_priority, triage_factors, "
              "triage_computed_at")
CASE_COLS = ("case_id, case_number, customer_id, status, priority, title, assigned_to, investigation_id, opened_at, "
             "created_at, updated_at, closed_at, decision, decision_reason, decided_by, created_by")
SEV_RANK_SQL = "CASE severity WHEN 'low' THEN 1 WHEN 'medium' THEN 2 WHEN 'high' THEN 3 ELSE 4 END"
ALERT_SORT_SQL = {
    "triggered_at": "triggered_at", "risk_score": "risk_score", "severity_rank": SEV_RANK_SQL, "status": "status",
    "customer_id": "customer_id", "detector_id": "detector_id", "updated_at": "updated_at",
    "triage_score": "triage_score",
}


class SqlMonitoringRepo:
    def __init__(self, engine: Any):
        self.engine = engine
        self._local = threading.local()

    # ---- unit of work
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """All repository calls made inside commit together, or roll back together if anything raises.

        Re-entrant: a nested call joins the outer transaction. Each write runs in a SAVEPOINT so that a
        handled constraint violation (for example the unresolved-alert unique index) does not abort the
        whole transaction.
        """
        if getattr(self._local, "conn", None) is not None:
            yield
            return
        with self.engine.begin() as conn:
            self._local.conn = conn
            try:
                yield
            finally:
                self._local.conn = None

    @contextmanager
    def _write_conn(self) -> Iterator[Any]:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            with conn.begin_nested():
                yield conn
        else:
            with self.engine.begin() as c:
                yield c

    @contextmanager
    def _read_conn(self) -> Iterator[Any]:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            yield conn
        else:
            with self.engine.connect() as c:
                yield c

    # ---- helpers
    def _rows(self, sql: str, **p: Any) -> list[dict[str, Any]]:
        from sqlalchemy import text

        with self._read_conn() as conn:
            return [dict(r._mapping) for r in conn.execute(text(sql), p)]

    def _exec(self, sql: str, **p: Any) -> None:
        from sqlalchemy import text

        with self._write_conn() as conn:
            conn.execute(text(sql), p)

    def ping(self) -> bool:
        from sqlalchemy import text

        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM monitoring_alerts LIMIT 1"))
        return True

    def _alert(self, r: dict[str, Any], txns: list[str] | None = None) -> MonitoringAlert:
        r = dict(r)
        r["transaction_ids"] = txns or []
        return MonitoringAlert(**r)

    def _alert_txns(self, alert_ids: list[str]) -> dict[str, list[str]]:
        if not alert_ids:
            return {}
        out: dict[str, list[str]] = {a: [] for a in alert_ids}
        for r in self._rows("SELECT alert_id, transaction_id FROM monitoring_alert_transactions "
                            "WHERE alert_id = ANY(:ids) ORDER BY transaction_id", ids=alert_ids):
            out[r["alert_id"]].append(r["transaction_id"])
        return out

    # ---- alerts
    def create_alert(self, alert: MonitoringAlert) -> MonitoringAlert:
        from sqlalchemy.exc import IntegrityError

        d = alert.model_dump(exclude={"transaction_ids"})
        d["explanation"] = _j(d["explanation"])
        d["triage_factors"] = _j(d["triage_factors"])
        names = [c.strip() for c in ALERT_COLS.split(",")]
        sql = (f"INSERT INTO monitoring_alerts ({ALERT_COLS}) VALUES (" +
               ", ".join(f"CAST(:{n} AS jsonb)" if n in ("explanation", "triage_factors") else f":{n}"
                         for n in names) + ")")
        try:
            self._exec(sql, **d)
        except IntegrityError as e:
            if "ux_alert_unresolved_per_detector" in str(e.orig):
                raise DuplicateUnresolvedAlert(f"{alert.customer_id}/{alert.detector_id}") from None
            raise
        self.add_alert_transactions(alert.alert_id, alert.transaction_ids)
        return alert

    def get_alert(self, alert_id: str) -> MonitoringAlert | None:
        rows = self._rows(f"SELECT {ALERT_COLS} FROM monitoring_alerts WHERE alert_id = :id", id=alert_id)
        return self._alert(rows[0], self._alert_txns([alert_id])[alert_id]) if rows else None

    def find_unresolved(self, customer_id: str, detector_id: str) -> MonitoringAlert | None:
        rows = self._rows(f"SELECT {ALERT_COLS} FROM monitoring_alerts WHERE customer_id = :c AND detector_id = :d "
                          "AND status <> 'RESOLVED'", c=customer_id, d=detector_id)
        if not rows:
            return None
        return self._alert(rows[0], self._alert_txns([rows[0]["alert_id"]])[rows[0]["alert_id"]])

    def resolved_alert_transactions(self, customer_id: str, detector_id: str) -> set[str]:
        rows = self._rows(
            "SELECT t.transaction_id FROM monitoring_alert_transactions t JOIN monitoring_alerts a "
            "ON a.alert_id = t.alert_id WHERE a.customer_id = :c AND a.detector_id = :d AND a.status = 'RESOLVED'",
            c=customer_id, d=detector_id)
        return {r["transaction_id"] for r in rows}

    def last_resolved_at(self, customer_id: str, detector_id: str) -> datetime | None:
        rows = self._rows("SELECT max(resolved_at) AS ts FROM monitoring_alerts WHERE customer_id = :c "
                          "AND detector_id = :d AND status = 'RESOLVED'", c=customer_id, d=detector_id)
        return rows[0]["ts"] if rows else None

    def update_alert(self, alert_id: str, **fields: Any) -> MonitoringAlert | None:
        cols = [k for k in fields if k in ALERT_UPDATABLE]
        if set(fields) - ALERT_UPDATABLE:
            raise ValueError(f"fields not updatable: {sorted(set(fields) - ALERT_UPDATABLE)}")
        if cols:
            sets = ", ".join(f"{c} = CAST(:{c} AS jsonb)" if c in JSON_FIELDS else f"{c} = :{c}" for c in cols)
            params = {c: (_j(fields[c]) if c in JSON_FIELDS else fields[c]) for c in cols}
            self._exec(f"UPDATE monitoring_alerts SET {sets} WHERE alert_id = :_id", _id=alert_id, **params)
        return self.get_alert(alert_id)

    def add_alert_transactions(self, alert_id: str, transaction_ids: list[str]) -> None:
        if not transaction_ids:
            return
        self._exec("INSERT INTO monitoring_alert_transactions (alert_id, transaction_id) "
                   "SELECT :a, unnest(CAST(:ids AS text[])) ON CONFLICT DO NOTHING",
                   a=alert_id, ids=list(dict.fromkeys(transaction_ids)))

    def list_alerts(self, f: AlertFilter) -> tuple[list[MonitoringAlert], int]:
        where: list[str] = ["TRUE"]
        p: dict[str, Any] = {}
        if f.statuses:
            where.append("status = ANY(:statuses)")
            p["statuses"] = list(f.statuses)
        if f.severities:
            where.append("severity = ANY(:severities)")
            p["severities"] = list(f.severities)
        if f.detector:
            where.append("detector_id = :detector")
            p["detector"] = f.detector
        if f.priorities:
            where.append("triage_priority = ANY(:priorities)")
            p["priorities"] = list(f.priorities)
        if f.min_risk is not None:
            where.append("risk_score >= :min_risk")
            p["min_risk"] = f.min_risk
        if f.max_risk is not None:
            where.append("risk_score <= :max_risk")
            p["max_risk"] = f.max_risk
        if f.customer_id:
            where.append("customer_id = :customer_id")
            p["customer_id"] = f.customer_id
        if f.case_id:
            where.append("case_id = :case_id")
            p["case_id"] = f.case_id
        if f.assigned_to == "unassigned":
            where.append("assigned_to IS NULL")
        elif f.assigned_to:
            where.append("assigned_to = :assigned_to")
            p["assigned_to"] = f.assigned_to
        if f.date_from:
            where.append("triggered_at >= :date_from")
            p["date_from"] = f.date_from
        if f.date_to:
            where.append("triggered_at <= :date_to")
            p["date_to"] = f.date_to
        if f.q:
            esc = f.q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(alert_id ILIKE :q OR customer_id ILIKE :q OR detector_id ILIKE :q OR description ILIKE :q)")
            p["q"] = f"%{esc}%"
        order = "ASC" if f.order == "asc" else "DESC"
        sort = ALERT_SORT_SQL.get(f.sort, "triggered_at")
        nulls = " NULLS LAST" if sort == "triage_score" else ""
        clause = " AND ".join(where)
        total = self._rows(f"SELECT count(*) AS n FROM monitoring_alerts WHERE {clause}", **p)[0]["n"]  # noqa: S608
        rows = self._rows(f"SELECT {ALERT_COLS} FROM monitoring_alerts WHERE {clause} "  # noqa: S608
                          f"ORDER BY {sort} {order}{nulls}, alert_id LIMIT :limit OFFSET :offset",
                          limit=f.limit, offset=f.offset, **p)
        txns = self._alert_txns([r["alert_id"] for r in rows])
        return [self._alert(r, txns.get(r["alert_id"])) for r in rows], int(total)

    def add_alert_event(self, ev: AlertEvent) -> None:
        d = ev.model_dump(exclude={"id"})
        d["detail"] = _j(d["detail"])
        self._exec("INSERT INTO monitoring_alert_events (alert_id, ts, actor, event_type, from_status, to_status, "
                   "detail) VALUES (:alert_id, :ts, :actor, :event_type, :from_status, :to_status, "
                   "CAST(:detail AS jsonb))", **d)

    def alert_events(self, alert_id: str) -> list[AlertEvent]:
        return [AlertEvent(**r) for r in self._rows(
            "SELECT id, alert_id, ts, actor, event_type, from_status, to_status, detail FROM monitoring_alert_events "
            "WHERE alert_id = :id ORDER BY id", id=alert_id)]

    # ---- cases
    def create_case(self, case: Case) -> Case:
        from sqlalchemy.exc import IntegrityError

        d = case.model_dump(exclude={"case_number"})
        names = [c.strip() for c in CASE_COLS.split(",")]
        sql = (f"INSERT INTO cases ({CASE_COLS}) VALUES (" +
               ", ".join("'FC-' || to_char(CAST(:created_at AS timestamptz), 'YYYY') || '-' || "
                         "lpad(nextval('case_number_seq')::text, 6, '0')" if n == "case_number" else f":{n}"
                         for n in names) + ")")
        try:
            self._exec(sql, **d)
        except IntegrityError as e:
            if "ux_one_open_case_per_customer" in str(e.orig):
                raise DuplicateOpenCase(case.customer_id) from None
            raise
        got = self.get_case(case.case_id)
        assert got is not None
        return got

    def get_case(self, case_id: str) -> Case | None:
        rows = self._rows(f"SELECT {CASE_COLS} FROM cases WHERE case_id = :id", id=case_id)
        return Case(**rows[0]) if rows else None

    def find_open_case(self, customer_id: str) -> Case | None:
        rows = self._rows(f"SELECT {CASE_COLS} FROM cases WHERE customer_id = :c AND status <> 'CLOSED'", c=customer_id)
        return Case(**rows[0]) if rows else None

    def list_cases(self, f: CaseFilter) -> tuple[list[Case], int]:
        where: list[str] = ["TRUE"]
        p: dict[str, Any] = {}
        if f.statuses:
            where.append("status = ANY(:statuses)")
            p["statuses"] = list(f.statuses)
        if f.priority:
            where.append("priority = :priority")
            p["priority"] = f.priority
        if f.customer_id:
            where.append("customer_id = :customer_id")
            p["customer_id"] = f.customer_id
        if f.assigned_to == "unassigned":
            where.append("assigned_to IS NULL")
        elif f.assigned_to:
            where.append("assigned_to = :assigned_to")
            p["assigned_to"] = f.assigned_to
        if f.q:
            esc = f.q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(case_number ILIKE :q OR customer_id ILIKE :q OR title ILIKE :q)")
            p["q"] = f"%{esc}%"
        clause = " AND ".join(where)
        total = self._rows(f"SELECT count(*) AS n FROM cases WHERE {clause}", **p)[0]["n"]  # noqa: S608
        rows = self._rows(f"SELECT {CASE_COLS} FROM cases WHERE {clause} ORDER BY updated_at DESC, case_id "  # noqa: S608
                          "LIMIT :limit OFFSET :offset", limit=f.limit, offset=f.offset, **p)
        return [Case(**r) for r in rows], int(total)

    def update_case(self, case_id: str, **fields: Any) -> Case | None:
        if set(fields) - CASE_UPDATABLE:
            raise ValueError(f"fields not updatable: {sorted(set(fields) - CASE_UPDATABLE)}")
        cols = list(fields)
        if cols:
            sets = ", ".join(f"{c} = :{c}" for c in cols)
            self._exec(f"UPDATE cases SET {sets} WHERE case_id = :_id", _id=case_id, **fields)  # noqa: S608
        return self.get_case(case_id)

    def alerts_for_case(self, case_id: str) -> list[MonitoringAlert]:
        rows = self._rows(f"SELECT {ALERT_COLS} FROM monitoring_alerts WHERE case_id = :id ORDER BY triggered_at",
                          id=case_id)
        txns = self._alert_txns([r["alert_id"] for r in rows])
        return [self._alert(r, txns.get(r["alert_id"])) for r in rows]

    def add_case_event(self, ev: CaseEvent) -> None:
        d = ev.model_dump(exclude={"id"})
        d["detail"] = _j(d["detail"])
        self._exec("INSERT INTO case_events (case_id, ts, actor, event_type, from_status, to_status, detail) VALUES "
                   "(:case_id, :ts, :actor, :event_type, :from_status, :to_status, CAST(:detail AS jsonb))", **d)

    def case_events(self, case_id: str) -> list[CaseEvent]:
        return [CaseEvent(**r) for r in self._rows(
            "SELECT id, case_id, ts, actor, event_type, from_status, to_status, detail FROM case_events "
            "WHERE case_id = :id ORDER BY id", id=case_id)]

    def add_note(self, note: CaseNote) -> None:
        self._exec("INSERT INTO case_notes (note_id, case_id, author, body, created_at) VALUES "
                   "(:note_id, :case_id, :author, :body, :created_at)", **note.model_dump())

    def notes(self, case_id: str) -> list[CaseNote]:
        return [CaseNote(**r) for r in self._rows(
            "SELECT note_id, case_id, author, body, created_at FROM case_notes WHERE case_id = :id "
            "ORDER BY created_at, note_id", id=case_id)]

    def add_case_evidence(self, ev: CaseEvidence) -> None:
        d = ev.model_dump()
        d["content"] = _j(d["content"])
        self._exec("INSERT INTO case_evidence (evidence_id, case_id, alert_id, transaction_id, document_id, chunk_id, "
                   "evidence_class, title, content, source, added_by, created_at) VALUES (:evidence_id, :case_id, "
                   ":alert_id, :transaction_id, :document_id, :chunk_id, :evidence_class, :title, "
                   "CAST(:content AS jsonb), :source, :added_by, :created_at)", **d)

    def case_evidence(self, case_id: str) -> list[CaseEvidence]:
        return [CaseEvidence(**r) for r in self._rows(
            "SELECT evidence_id, case_id, alert_id, transaction_id, document_id, chunk_id, evidence_class, title, "
            "content, source, added_by, created_at FROM case_evidence WHERE case_id = :id "
            "ORDER BY created_at, evidence_id", id=case_id)]

    # ---- data quality
    def save_batch(self, batch: IngestionBatch, rejected: list[RejectedRow]) -> None:
        d = batch.model_dump()
        d["details"] = _j(d["details"])
        cols = list(d)
        with self.transaction():
            self._exec(f"INSERT INTO ingestion_batches ({', '.join(cols)}) VALUES ("  # noqa: S608
                       + ", ".join("CAST(:details AS jsonb)" if c == "details" else f":{c}" for c in cols) + ")", **d)
            for r in rejected:
                rd = r.model_dump(exclude={"id"})
                rd["payload"] = _j(rd["payload"])
                self._exec("INSERT INTO rejected_transactions (batch_id, row_number, transaction_id, reason_code, "
                           "reason_group, reason, payload, created_at) VALUES (:batch_id, :row_number, :transaction_id, "
                           ":reason_code, :reason_group, :reason, CAST(:payload AS jsonb), :created_at)", **rd)

    def get_batch(self, batch_id: str) -> IngestionBatch | None:
        rows = self._rows("SELECT * FROM ingestion_batches WHERE batch_id = :id", id=batch_id)
        return IngestionBatch(**rows[0]) if rows else None

    def list_batches(self, limit: int = 50, offset: int = 0) -> tuple[list[IngestionBatch], int]:
        total = self._rows("SELECT count(*) AS n FROM ingestion_batches")[0]["n"]
        rows = self._rows("SELECT * FROM ingestion_batches ORDER BY started_at DESC, batch_id LIMIT :l OFFSET :o",
                          l=limit, o=offset)
        return [IngestionBatch(**r) for r in rows], int(total)

    def list_rejected(self, batch_id: str | None = None, reason_code: str | None = None,
                      reason_group: str | None = None, limit: int = 50, offset: int = 0) -> tuple[list[RejectedRow], int]:
        where, p = ["TRUE"], {}
        for col, val in (("batch_id", batch_id), ("reason_code", reason_code), ("reason_group", reason_group)):
            if val:
                where.append(f"{col} = :{col}")
                p[col] = val
        clause = " AND ".join(where)
        total = self._rows(f"SELECT count(*) AS n FROM rejected_transactions WHERE {clause}", **p)[0]["n"]  # noqa: S608
        rows = self._rows(f"SELECT * FROM rejected_transactions WHERE {clause} ORDER BY created_at DESC, id DESC "  # noqa: S608
                          "LIMIT :limit OFFSET :offset", limit=limit, offset=offset, **p)
        return [RejectedRow(**r) for r in rows], int(total)

    def quality_totals(self) -> dict[str, Any]:
        rows = [(r["expected_count"], r["received"], r["processed"], r["rejected"], r["duplicates"], r["malformed"],
                 r["late"], r["failed"], r["missing"], r["finished_at"]) for r in self._rows(
            "SELECT expected_count, received, processed, rejected, duplicates, malformed, late, failed, missing, "
            "finished_at FROM ingestion_batches")]
        reasons = {r["reason_code"]: int(r["n"]) for r in self._rows(
            "SELECT reason_code, count(*) AS n FROM rejected_transactions GROUP BY reason_code")}
        return build_quality_totals(rows, reasons)

    # ---- configuration governance
    def add_config_change(self, change: ConfigChange) -> None:
        d = change.model_dump(exclude={"id"})
        d["old_value"], d["new_value"] = _j(d["old_value"]), _j(d["new_value"])
        self._exec("INSERT INTO config_change_log (ts, config_name, path, old_value, new_value, changed_by, reason, "
                   "source) VALUES (:ts, :config_name, :path, CAST(:old_value AS jsonb), CAST(:new_value AS jsonb), "
                   ":changed_by, :reason, :source)", **d)

    def list_config_changes(self, limit: int = 100) -> list[ConfigChange]:
        return [ConfigChange(**r) for r in self._rows(
            "SELECT * FROM config_change_log ORDER BY ts DESC, id DESC LIMIT :l", l=limit)]

    def last_config_snapshot(self, name: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT new_value FROM config_change_log WHERE config_name = :n AND path = '(snapshot)' "
                          "ORDER BY id DESC LIMIT 1", n=name)
        return dict(rows[0]["new_value"]) if rows and rows[0]["new_value"] is not None else None

    def resolved_alerts(self, since: datetime | None = None, limit: int = 1000) -> list[MonitoringAlert]:
        rows = self._rows(f"SELECT {ALERT_COLS} FROM monitoring_alerts WHERE status = 'RESOLVED' "  # noqa: S608
                          "AND (CAST(:since AS timestamptz) IS NULL OR resolved_at >= :since) "
                          "ORDER BY resolved_at DESC LIMIT :limit", since=since, limit=limit)
        txns = self._alert_txns([r["alert_id"] for r in rows])
        return [self._alert(r, txns.get(r["alert_id"])) for r in rows]

    def all_alerts(self) -> list[MonitoringAlert]:
        rows = self._rows(f"SELECT {ALERT_COLS} FROM monitoring_alerts")  # noqa: S608
        return [self._alert(r) for r in rows]

    # ---- runs & KPIs
    def save_run(self, run: MonitoringRun) -> None:
        d = run.model_dump()
        d["details"] = _j(d["details"])
        cols = [k for k in d]
        sql = (f"INSERT INTO monitoring_runs ({', '.join(cols)}) VALUES ("  # noqa: S608
               + ", ".join("CAST(:details AS jsonb)" if c == "details" else f":{c}" for c in cols) + ") "
               "ON CONFLICT (run_id) DO UPDATE SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "run_id"))
        self._exec(sql, **d)

    def list_runs(self, limit: int = 20) -> list[MonitoringRun]:
        return [MonitoringRun(**r) for r in self._rows(
            "SELECT * FROM monitoring_runs ORDER BY started_at DESC LIMIT :limit", limit=limit)]

    def kpis(self) -> dict[str, Any]:
        alert_rows = [(r["status"], r["severity"], r["detector_id"], r["resolution"]) for r in self._rows(
            "SELECT status, severity, detector_id, resolution FROM monitoring_alerts")]
        case_rows = [(r["status"], r["opened_at"], r["closed_at"]) for r in self._rows(
            "SELECT status, opened_at, closed_at FROM cases")]
        run_rows = [(r["started_at"], r["transactions_in_scope"], r["customers_evaluated"], r["duration_ms"],
                     r["alerts_created"]) for r in self._rows(
            "SELECT started_at, transactions_in_scope, customers_evaluated, duration_ms, alerts_created "
            "FROM monitoring_runs")]
        return build_kpis(alert_rows, case_rows, run_rows)


def new_repo(settings: Any, store: Any) -> MonitoringRepo:
    """Repository matching the configured data backend."""
    if settings.data_backend == "postgres":
        return SqlMonitoringRepo(store.engine)
    return MemoryMonitoringRepo()


__all__ = ["AlertFilter", "CaseFilter", "MonitoringRepo", "MemoryMonitoringRepo", "SqlMonitoringRepo",
           "DuplicateUnresolvedAlert", "DuplicateOpenCase", "build_kpis", "new_repo", "utcnow"]
