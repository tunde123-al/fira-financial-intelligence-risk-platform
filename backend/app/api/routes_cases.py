"""Case management and the investigation workbench."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_container, get_principal, present
from app.api.routes_monitoring import AssignBody, service_errors
from app.api.workbench import build_workbench
from app.monitoring.models import ID_PATTERNS
from app.monitoring.repository import CaseFilter
from app.security.principal import Principal

router = APIRouter(prefix="/api/cases", tags=["cases"])
CASE_ID = Path(pattern=ID_PATTERNS["case"])
CASE_STATUSES = {"OPEN", "INVESTIGATING", "ESCALATED", "PENDING_REVIEW", "CLOSED"}


class CreateCaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str = Field(pattern=r"^CUST-\d{1,10}$")
    alert_ids: list[str] = Field(default_factory=list, max_length=50)
    priority: Literal["low", "medium", "high", "critical"] | None = None
    title: str | None = Field(default=None, max_length=200)


class TransitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["INVESTIGATING", "PENDING_REVIEW"]


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["CLEARED", "FALSE_POSITIVE", "CONFIRMED_SUSPICIOUS", "ESCALATED"]
    reason: str = Field(min_length=5, max_length=4000)


class PriorityBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priority: Literal["low", "medium", "high", "critical"]
    reason: str = Field(min_length=5, max_length=1000)


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=4000)


class EvidenceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["transaction", "document", "alert"]
    ref: str = Field(min_length=3, max_length=120)
    note: str | None = Field(default=None, max_length=1000)


class InvestigateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lookback_days: int | None = Field(default=None, ge=1, le=365)


class AttachBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alert_ids: list[str] = Field(min_length=1, max_length=50)


@router.get("", summary="List cases")
def list_cases(status: str | None = Query(default=None), priority: Literal["low", "medium", "high", "critical"] | None = None,
               customer_id: str | None = Query(default=None, pattern=r"^CUST-\d{1,10}$"),
               assigned_to: str | None = Query(default=None, pattern=r"^(me|unassigned|U-[A-Za-z0-9_.-]{1,60})$"),
               q: str | None = Query(default=None, max_length=80), limit: int = Query(50, ge=1, le=200),
               offset: int = Query(0, ge=0), c: Any = Depends(get_container),
               p: Principal = Depends(get_principal)) -> dict[str, Any]:
    statuses = None
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        bad = [s for s in statuses if s not in CASE_STATUSES]
        if bad:
            raise HTTPException(422, f"invalid status: {bad}")
    items, total = c.monitoring_repo.list_cases(CaseFilter(
        statuses=statuses, priority=priority, customer_id=customer_id,
        assigned_to=p.user_id if assigned_to == "me" else assigned_to, q=q, limit=limit, offset=offset))
    return {"items": [i.model_dump(mode="json") for i in items], "total": total, "limit": limit, "offset": offset}


@router.post("", status_code=201, summary="Create a case (or attach alerts to the customer's open case)")
def create_case(body: CreateCaseBody, c: Any = Depends(get_container),
                p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        case, created = c.monitoring.create_case(body.customer_id, body.alert_ids, p, body.priority, body.title)
    return {"case": case.model_dump(mode="json"), "created": created}


@router.get("/{case_id}")
def get_case(case_id: str = CASE_ID, c: Any = Depends(get_container),
             p: Principal = Depends(get_principal)) -> dict[str, Any]:
    case = c.monitoring_repo.get_case(case_id)
    if case is None:
        raise HTTPException(404, f"case {case_id} not found")
    return {"case": case.model_dump(mode="json"),
            "alerts": [a.model_dump(mode="json") for a in c.monitoring_repo.alerts_for_case(case_id)],
            "notes": [n.model_dump(mode="json") for n in c.monitoring_repo.notes(case_id)]}


@router.get("/{case_id}/workbench", summary="Everything an investigator needs for the case, in one response")
def workbench(case_id: str = CASE_ID, c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    try:
        return build_workbench(c, p, case_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None


@router.post("/{case_id}/alerts", summary="Attach more of the customer's alerts to the case")
def attach_alerts(body: AttachBody, case_id: str = CASE_ID, c: Any = Depends(get_container),
                  p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        case = c.monitoring_repo.get_case(case_id)
        if case is None:
            raise HTTPException(404, f"case {case_id} not found")
        out, _ = c.monitoring.create_case(case.customer_id, body.alert_ids, p)
    return {"case": out.model_dump(mode="json")}


@router.post("/{case_id}/assign")
def assign(body: AssignBody, case_id: str = CASE_ID, c: Any = Depends(get_container),
           p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        return c.monitoring.assign_case(case_id, body.assignee, p).model_dump(mode="json")


@router.post("/{case_id}/priority", summary="Change the case priority (reason required; audited)")
def set_priority(body: PriorityBody, case_id: str = CASE_ID, c: Any = Depends(get_container),
                 p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        return c.monitoring.set_case_priority(case_id, body.priority, body.reason, p).model_dump(mode="json")


@router.post("/{case_id}/transition")
def transition(body: TransitionBody, case_id: str = CASE_ID, c: Any = Depends(get_container),
               p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        return c.monitoring.transition_case(case_id, body.status, p).model_dump(mode="json")


@router.post("/{case_id}/decision", summary="Record the investigator's decision (audited; closes the case unless ESCALATED)")
def decide(body: DecisionBody, case_id: str = CASE_ID, c: Any = Depends(get_container),
           p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        out = c.monitoring.decide_case(case_id, body.decision, body.reason, p)
    return {"case": out["case"].model_dump(mode="json"), "alerts_updated": out["alerts_updated"],
            "investigation": out["investigation"]}


@router.post("/{case_id}/notes", status_code=201)
def add_note(body: NoteBody, case_id: str = CASE_ID, c: Any = Depends(get_container),
             p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        return c.monitoring.add_note(case_id, body.body, p).model_dump(mode="json")


@router.get("/{case_id}/evidence")
def list_evidence(case_id: str = CASE_ID, c: Any = Depends(get_container),
                  p: Principal = Depends(get_principal)) -> list[dict[str, Any]]:
    if c.monitoring_repo.get_case(case_id) is None:
        raise HTTPException(404, f"case {case_id} not found")
    return present(c, p, [e.model_dump(mode="json") for e in c.monitoring_repo.case_evidence(case_id)])


@router.post("/{case_id}/evidence", status_code=201, summary="Attach an existing transaction, document passage or alert")
def add_evidence(body: EvidenceBody, case_id: str = CASE_ID, c: Any = Depends(get_container),
                 p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        e = c.monitoring.add_evidence(case_id, body.kind, body.ref, p, body.note)
    return present(c, p, e.model_dump(mode="json"))


@router.post("/{case_id}/investigate", summary="Run the evidence-grounded investigation agent for the case's customer")
def investigate(body: InvestigateBody, case_id: str = CASE_ID, c: Any = Depends(get_container),
                p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        res = c.monitoring.run_investigation(case_id, p, body.lookback_days)
    return present(c, p, {k: res.get(k) for k in ("investigation_id", "episode_id", "status", "status_reason",
                                                   "engine", "validation")})
