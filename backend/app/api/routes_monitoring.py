"""Monitoring runs, transaction ingestion, alert management and detector catalogue."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_container, get_principal, present, require_role
from app.monitoring.detectors import catalogue
from app.monitoring.models import ID_PATTERNS
from app.monitoring.repository import AlertFilter
from app.monitoring.service import AccessError, ConflictError, NotFoundError
from app.security.principal import Principal

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])
ALERT_ID = Path(pattern=ID_PATTERNS["alert"])
STATUSES = {"NEW", "TRIAGED", "INVESTIGATING", "ESCALATED", "RESOLVED"}
SEVERITIES = {"low", "medium", "high", "critical"}
PRIORITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}


@contextmanager
def service_errors() -> Iterator[None]:
    """Map workflow errors to HTTP status codes (details are validation messages, never internals)."""
    try:
        yield
    except NotFoundError as e:
        raise HTTPException(404, str(e)) from None
    except AccessError as e:
        raise HTTPException(403, str(e)) from None
    except ConflictError as e:
        raise HTTPException(409, str(e)) from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


def _csv(value: str | None, allowed: set[str], name: str) -> list[str] | None:
    if not value:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    bad = [v for v in items if v not in allowed]
    if bad:
        raise HTTPException(422, f"invalid {name}: {bad}")
    return items


# ------------------------------------------------------------------------ models
class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_end: datetime | None = None
    lookback_days: int | None = Field(default=None, ge=1, le=365)
    active_days: int | None = Field(default=None, ge=1, le=90)
    customer_ids: list[str] | None = Field(default=None, max_length=5000)


class ExpectedIn(BaseModel):
    """What the sender says the batch should contain; used to measure coverage and missing records."""
    model_config = ConfigDict(extra="forbid")
    count: int | None = Field(default=None, ge=0, le=10_000_000)
    ids: list[str] | None = Field(default=None, max_length=50_000)
    sequence: dict[str, Any] | None = None


class IngestBody(BaseModel):
    """Rows are accepted as raw objects: the data-quality gate classifies bad rows (reason codes + quarantine)
    instead of failing the whole request."""
    model_config = ConfigDict(extra="forbid")
    transactions: list[Any] = Field(min_length=1, max_length=5000)
    run_monitoring: bool = True
    source: str = Field(default="api", min_length=1, max_length=60, pattern=r"^[A-Za-z0-9_. :/-]+$")
    expected: ExpectedIn | None = None
    require_transaction_id: bool = False


class AssignBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignee: str = Field(min_length=3, max_length=80, pattern=r"^U-[A-Za-z0-9_.-]+$")


class TransitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["TRIAGED", "INVESTIGATING", "ESCALATED", "RESOLVED"]
    reason: str | None = Field(default=None, max_length=2000)
    resolution: Literal["CLEARED", "FALSE_POSITIVE", "CONFIRMED_SUSPICIOUS"] | None = None


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution: Literal["CLEARED", "FALSE_POSITIVE", "CONFIRMED_SUSPICIOUS"]
    reason: str = Field(min_length=5, max_length=2000)


class AlertCaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priority: Literal["low", "medium", "high", "critical"] | None = None
    title: str | None = Field(default=None, max_length=200)


# ----------------------------------------------------------------------- endpoints
@router.get("/detectors", summary="Detector catalogue with the active configuration")
def detectors(c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return {"risk_config": c.risk_config.version, "monitoring_config": c.monitoring_config.version,
            "detectors": catalogue(c.risk_config, c.monitoring_config.alert_tier)}


@router.get("/kpis", summary="Alert and case KPIs computed from stored data")
def kpis(c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return c.monitoring_repo.kpis()


@router.get("/runs")
def runs(limit: int = Query(20, ge=1, le=200), c: Any = Depends(get_container),
         p: Principal = Depends(get_principal)) -> list[dict[str, Any]]:
    return [r.model_dump(mode="json") for r in c.monitoring_repo.list_runs(limit)]


@router.post("/run", summary="Run transaction monitoring now (admin; synchronous)")
def run_monitoring(body: RunBody, c: Any = Depends(get_container),
                   p: Principal = Depends(require_role("admin"))) -> dict[str, Any]:
    with service_errors():
        run = c.monitoring.run(window_end=body.window_end, lookback_days=body.lookback_days,
                               customer_ids=body.customer_ids, active_days=body.active_days, actor=p, mode="api")
    return run.model_dump(mode="json")


@router.post("/transactions", summary="Ingest a transaction batch and optionally monitor it (admin)")
def ingest(body: IngestBody, c: Any = Depends(get_container),
           p: Principal = Depends(require_role("admin"))) -> dict[str, Any]:
    with service_errors():
        exp = body.expected.model_dump(exclude_none=True) if body.expected else None
        return c.monitoring.ingest(body.transactions, p, body.run_monitoring, body.source, exp,
                                   body.require_transaction_id)


@router.get("/alerts", summary="Alert queue: filter, search, sort, paginate")
def list_alerts(
        status: str | None = Query(default=None, description="comma-separated statuses"),
        severity: str | None = Query(default=None, description="comma-separated severities"),
        priority: str | None = Query(default=None, description="comma-separated triage priorities"),
        detector: str | None = Query(default=None, pattern=r"^[A-Z_]{2,40}$"),
        min_risk: float | None = Query(default=None, ge=0, le=100), max_risk: float | None = Query(default=None, ge=0, le=100),
        customer_id: str | None = Query(default=None, pattern=r"^CUST-\d{1,10}$"),
        assigned_to: str | None = Query(default=None, pattern=r"^(me|unassigned|U-[A-Za-z0-9_.-]{1,60})$"),
        date_from: datetime | None = None, date_to: datetime | None = None,
        q: str | None = Query(default=None, max_length=80), case_id: str | None = Query(default=None, pattern=ID_PATTERNS["case"]),
        sort: Literal["triggered_at", "risk_score", "severity_rank", "status", "customer_id", "detector_id",
                      "updated_at", "triage_score"] = "triggered_at",
        order: Literal["asc", "desc"] = "desc", limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
        c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    f = AlertFilter(statuses=_csv(status, STATUSES, "status"), severities=_csv(severity, SEVERITIES, "severity"),
                    detector=detector, min_risk=min_risk, max_risk=max_risk, customer_id=customer_id,
                    assigned_to=p.user_id if assigned_to == "me" else assigned_to, date_from=date_from,
                    date_to=date_to, q=q, case_id=case_id, priorities=_csv(priority, PRIORITIES, "priority"),
                    sort=sort, order=order, limit=limit, offset=offset)
    items, total = c.monitoring.list_alerts(f)
    return {"items": [a.model_dump(mode="json") for a in items], "total": total, "limit": limit, "offset": offset}


@router.get("/alerts/{alert_id}")
def get_alert(alert_id: str = ALERT_ID, c: Any = Depends(get_container),
              p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        d = c.monitoring.alert_detail(alert_id)
    case = c.monitoring_repo.get_case(d["alert"].case_id) if d["alert"].case_id else None
    return {"alert": d["alert"].model_dump(mode="json"), "transactions": present(c, p, d["transactions"]),
            "events": [e.model_dump(mode="json") for e in d["events"]],
            "case": case.model_dump(mode="json") if case else None}


@router.post("/alerts/{alert_id}/assign")
def assign_alert(body: AssignBody, alert_id: str = ALERT_ID, c: Any = Depends(get_container),
                 p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        return c.monitoring.assign_alert(alert_id, body.assignee, p).model_dump(mode="json")


@router.post("/alerts/{alert_id}/transition")
def transition_alert(body: TransitionBody, alert_id: str = ALERT_ID, c: Any = Depends(get_container),
                     p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        return c.monitoring.transition_alert(alert_id, body.status, p, body.reason, body.resolution).model_dump(mode="json")


@router.post("/alerts/{alert_id}/resolve")
def resolve_alert(body: ResolveBody, alert_id: str = ALERT_ID, c: Any = Depends(get_container),
                  p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        return c.monitoring.transition_alert(alert_id, "RESOLVED", p, body.reason, body.resolution).model_dump(mode="json")


@router.post("/alerts/{alert_id}/case", status_code=201, summary="Create a case for this alert, or join the customer's open case")
def alert_to_case(body: AlertCaseBody, alert_id: str = ALERT_ID, c: Any = Depends(get_container),
                  p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        a = c.monitoring_repo.get_alert(alert_id)
        if a is None:
            raise NotFoundError(f"alert {alert_id} not found")
        case, created = c.monitoring.create_case(a.customer_id, [alert_id], p, body.priority, body.title)
    return {"case": case.model_dump(mode="json"), "created": created}


@router.get("/triage/{alert_id}", tags=["triage"],
            summary="Why this alert has its triage score: every factor with its points, value and reason")
def triage_explanation(alert_id: str = ALERT_ID, c: Any = Depends(get_container),
                       p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        return c.monitoring.triage_explanation(alert_id)


class RecomputeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alert_id: str | None = Field(default=None, pattern=ID_PATTERNS["alert"])


@router.post("/triage/recompute", tags=["triage"],
             summary="Re-score unresolved alerts (age and customer history change over time); admin only")
def triage_recompute(body: RecomputeBody, c: Any = Depends(get_container),
                     p: Principal = Depends(require_role("admin"))) -> dict[str, Any]:
    with service_errors():
        return c.monitoring.recompute_triage(p, body.alert_id)


@router.get("/quality", tags=["alerts"],
            summary="Operational alert quality: confirmation / false-discovery / closure rates (no FPR or recall here)")
def alert_quality(date_from: datetime | None = None, c: Any = Depends(get_container),
                  p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        return c.monitoring.alert_quality(date_from)


@router.get("/feedback", tags=["alerts"],
            summary="Recorded investigator outcomes (alert, decision, reason, investigator, timestamp)")
def alert_feedback(limit: int = Query(100, ge=1, le=500), c: Any = Depends(get_container),
                   p: Principal = Depends(get_principal)) -> list[dict[str, Any]]:
    with service_errors():
        return c.monitoring.feedback(limit)


@router.get("/my-work", tags=["alerts"], summary="The caller's open alerts by priority, overdue items and recent outcomes")
def my_work(recent_days: int = Query(7, ge=1, le=90), c: Any = Depends(get_container),
            p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        return c.monitoring.my_work(p, recent_days)
