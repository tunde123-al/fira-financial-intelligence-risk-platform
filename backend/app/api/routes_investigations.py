"""Investigations, agent runs and human decisions."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field

from app.agents.report import to_markdown
from app.api.deps import audit, get_container, get_principal, present, run_tool
from app.core.observability import investigation_id_var, request_id_var
from app.data.store import new_id, utcnow
from app.schemas.domain import ID_PATTERNS, HumanDecision
from app.security.principal import Principal

router = APIRouter(prefix="/api", tags=["investigations"])
INV_ID = Path(pattern=ID_PATTERNS["investigation"])


class CreateInvestigationBody(BaseModel):
    subject_type: Literal["customer", "account", "transaction", "device"]
    subject_id: str = Field(pattern=r"^(CUST|ACC|TXN|DEV)-\d{1,12}$")
    request_text: str | None = Field(default=None, max_length=2000)


class InvestigateBody(BaseModel):
    request: str = Field(min_length=5, max_length=2000,
                         examples=["Investigate customer CUST-10291 and identify unusual activity during the last 30 days."])
    subject_type: Literal["customer", "account", "transaction", "device"] | None = None
    subject_id: str | None = Field(default=None, pattern=r"^(CUST|ACC|TXN|DEV)-\d{1,12}$")
    lookback_days: int | None = Field(default=None, ge=1, le=365)
    investigation_id: str | None = Field(default=None, pattern=ID_PATTERNS["investigation"])


class DecisionBody(BaseModel):
    decision: Literal["confirm", "reject", "escalate", "request_more_evidence"]
    rationale: str = Field(min_length=5, max_length=4000)
    failure_categories: list[Literal["false_positive", "false_negative", "missing_evidence", "wrong_citation",
                                     "unclear_report", "wrong_subject", "other"]] = Field(default_factory=list)
    report_quality: int | None = Field(default=None, ge=1, le=5)


DECISION_EFFECT = {
    "confirm": ("closed", "confirmed_suspicious"),
    "reject": ("closed", "legitimate"),
    "escalate": ("pending_review", "escalated"),
    "request_more_evidence": ("in_progress", "more_evidence_requested"),
}


def _get(c: Any, investigation_id: str) -> Any:
    inv = c.store.get_investigation(investigation_id)
    if inv is None:
        raise HTTPException(404, f"investigation {investigation_id} not found")
    return inv


@router.get("/investigations")
def list_investigations(status: str | None = Query(default=None, pattern="^[a-z_]{2,30}$"),
                        subject_id: str | None = Query(default=None, max_length=40),
                        limit: int = Query(100, ge=1, le=500), c: Any = Depends(get_container),
                        p: Principal = Depends(get_principal)) -> list[dict[str, Any]]:
    invs = c.store.list_investigations(subject_ids=[subject_id] if subject_id else None, status=status, limit=limit)
    return present(c, p, [i.model_copy(update={"report": None}) for i in invs])


@router.post("/investigations", status_code=201)
def create_investigation(body: CreateInvestigationBody, c: Any = Depends(get_container),
                         p: Principal = Depends(get_principal)) -> dict[str, Any]:
    inv = run_tool(c, p, "create_investigation", body.model_dump())
    audit(c, p, "create_investigation", "ok", body.subject_type, body.subject_id, investigation_id=inv.investigation_id)
    return present(c, p, inv)


@router.get("/investigations/{investigation_id}")
def get_investigation(investigation_id: str = INV_ID, c: Any = Depends(get_container),
                      p: Principal = Depends(get_principal)) -> dict[str, Any]:
    inv = _get(c, investigation_id)
    out = present(c, p, inv)
    out["decisions"] = present(c, p, c.store.list_decisions(investigation_id))
    return out


@router.get("/investigations/{investigation_id}/evidence")
def get_evidence(investigation_id: str = INV_ID, c: Any = Depends(get_container),
                 p: Principal = Depends(get_principal)) -> list[dict[str, Any]]:
    _get(c, investigation_id)
    return present(c, p, c.store.list_evidence(investigation_id))


@router.get("/investigations/{investigation_id}/graph")
def get_investigation_graph(investigation_id: str = INV_ID, depth: int = Query(2, ge=1, le=3),
                            c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    inv = _get(c, investigation_id)
    customer = (inv.report or {}).get("analysed_customer")
    kind, eid = ("customer", customer) if customer else (inv.subject_type, inv.subject_id)
    if kind not in ("customer", "account", "device", "merchant"):
        raise HTTPException(422, "graph view requires a customer, account, device or merchant subject")
    return present(c, p, run_tool(c, p, "get_related_entities", {"kind": kind, "entity_id": eid, "depth": depth,
                                                                   "limit": 200}))


@router.get("/investigations/{investigation_id}/episodes")
def get_episodes(investigation_id: str = INV_ID, c: Any = Depends(get_container),
                 p: Principal = Depends(get_principal)) -> list[dict[str, Any]]:
    _get(c, investigation_id)
    return present(c, p, c.store.episodes_for_investigation(investigation_id))


@router.get("/investigations/{investigation_id}/report.md", response_class=Response)
def report_markdown(investigation_id: str = INV_ID, c: Any = Depends(get_container),
                    p: Principal = Depends(get_principal)) -> Response:
    inv = _get(c, investigation_id)
    if not inv.report:
        raise HTTPException(404, "no report yet")
    return Response(to_markdown(present(c, p, inv.report)), media_type="text/markdown")


@router.post("/investigations/{investigation_id}/decision")
def decide(body: DecisionBody, investigation_id: str = INV_ID, c: Any = Depends(get_container),
           p: Principal = Depends(get_principal)) -> dict[str, Any]:
    inv = _get(c, investigation_id)
    if inv.status not in ("pending_review", "in_progress"):
        raise HTTPException(409, f"investigation is {inv.status}; decisions are accepted only while under review")
    d = HumanDecision(decision_id=new_id("DEC"), investigation_id=investigation_id, decided_by=p.user_id,
                      decision=body.decision, rationale=body.rationale, failure_categories=[str(x) for x in body.failure_categories],
                      report_quality=body.report_quality, created_at=utcnow())
    c.store.add_decision(d)
    new_status, conclusion = DECISION_EFFECT[body.decision]
    upd = c.store.update_investigation(investigation_id, status=new_status, conclusion=conclusion,
                                       closed_at=utcnow() if new_status == "closed" else None)
    for ep in c.store.episodes_for_investigation(investigation_id):
        c.store.update_episode(ep["episode_id"], human_feedback={
            "decision": body.decision, "rationale": body.rationale, "failure_categories": body.failure_categories,
            "report_quality": body.report_quality, "by": p.user_id, "at": d.created_at.isoformat()})
    audit(c, p, "human_decision", "ok", "investigation", investigation_id, decision=body.decision)
    return {"decision": present(c, p, d), "investigation": present(c, p, upd.model_copy(update={"report": None}))}


@router.post("/agent/investigate", tags=["agent"], summary="Run the investigation agent (synchronous)")
def investigate(body: InvestigateBody, c: Any = Depends(get_container),
                p: Principal = Depends(get_principal)) -> dict[str, Any]:
    subject = {"type": body.subject_type, "id": body.subject_id} if body.subject_type and body.subject_id else None
    res = c.agent.run(body.request, p, subject=subject, lookback_days=body.lookback_days,
                      request_id=request_id_var.get(), investigation_id=body.investigation_id)
    if res.get("investigation_id"):
        investigation_id_var.set(res["investigation_id"])
    audit(c, p, "agent_investigate", res["status"], "investigation", res.get("investigation_id"),
          episode_id=res["episode_id"], engine=res["engine"])
    return present(c, p, res)
