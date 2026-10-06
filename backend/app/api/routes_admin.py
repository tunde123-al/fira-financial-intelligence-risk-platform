"""Evaluation, controlled improvement (config versions) and audit endpoints."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from app.api.deps import audit, get_container, get_principal, present, require_role
from app.evaluation.benchmarks import load_labels, sample_labels
from app.evaluation.improvement import ApprovalError, approve, classify_failures, submit_proposal
from app.risk.config import RiskConfig, to_yaml
from app.security.principal import Principal

router = APIRouter(prefix="/api")


class EvalBody(BaseModel):
    kinds: list[Literal["risk", "retrieval", "agent"]] = Field(default=["risk", "retrieval"])
    per_scenario: int = Field(default=10, ge=1, le=200)
    agent_sample: int = Field(default=2, ge=1, le=20)


@router.get("/evaluation/runs", tags=["evaluation"])
def evaluation_runs(limit: int = Query(20, ge=1, le=200), c: Any = Depends(get_container),
                    p: Principal = Depends(get_principal)) -> list[dict[str, Any]]:
    return present(c, p, [{**r, "created_at": str(r.get("created_at"))} for r in c.store.list_evaluation_runs(limit)])


@router.post("/evaluation/run", tags=["evaluation"], summary="Run benchmarks now (admin; synchronous)")
def run_evaluation(body: EvalBody, c: Any = Depends(get_container),
                   p: Principal = Depends(require_role("admin"))) -> dict[str, Any]:
    from app.evaluation.runner import run_all

    res = run_all(c, body.per_scenario, body.agent_sample, tuple(body.kinds))
    audit(c, p, "evaluation_run", "ok", details_kinds=body.kinds, run_id=res["run_id"])
    return {k: v for k, v in res.items() if k not in ("risk_rows", "agent_runs")}


@router.get("/evaluation/failures", tags=["evaluation"], summary="Human-feedback failure classification")
def failures(c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return classify_failures(c.store, c.risk_config.score.investigation_threshold)


class ProposalBody(BaseModel):
    rationale: str | None = Field(default=None, max_length=2000)
    config_yaml: str | None = Field(default=None, max_length=50_000, description="optional explicit candidate")
    per_scenario: int = Field(default=15, ge=2, le=200)


@router.post("/config/proposals", tags=["improvement"], summary="Propose a risk-config change and regress it offline")
def propose(body: ProposalBody, c: Any = Depends(get_container),
            p: Principal = Depends(get_principal)) -> dict[str, Any]:
    import yaml

    candidate = None
    if body.config_yaml:
        try:
            candidate = RiskConfig(**yaml.safe_load(body.config_yaml))
        except Exception as e:
            raise HTTPException(422, f"invalid configuration: {type(e).__name__}") from None
    labels = sample_labels(load_labels(c.settings, c.store), body.per_scenario)
    rec = submit_proposal(c.store, c.graph, c.risk_config, labels, p, candidate, body.rationale, c.ml_model)
    audit(c, p, "config_proposal", rec.get("status", "no_change"), "config", rec.get("version_id"))
    return present(c, p, {**rec, "created_at": str(rec.get("created_at"))})


@router.get("/config/versions", tags=["improvement"])
def config_versions(c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return {"active": {"version": c.risk_config.version, "fingerprint": c.risk_config.fingerprint(),
                       "yaml": to_yaml(c.risk_config)},
            "versions": present(c, p, [{**r, "created_at": str(r.get("created_at")),
                                        "approved_at": str(r.get("approved_at")) if r.get("approved_at") else None}
                                       for r in c.store.list_config_versions()])}


@router.post("/config/versions/{version_id}/approve", tags=["improvement"], summary="Approve and activate (admin)")
def approve_version(version_id: str = Path(pattern=r"^CFG-[A-Z0-9]{1,20}$"), c: Any = Depends(get_container),
                    p: Principal = Depends(require_role("admin"))) -> dict[str, Any]:
    try:
        rec = approve(c.store, version_id, p)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except ApprovalError as e:
        audit(c, p, "config_approve", "denied", "config", version_id, reason=str(e))
        raise HTTPException(409, str(e)) from None
    c.reload_risk_config(RiskConfig(**rec["config"]))
    from app.monitoring import governance

    governance.sync(c, p.user_id, "api:config_approve", str(rec.get("rationale") or "approved risk-config version")[:200])
    audit(c, p, "config_approve", "ok", "config", version_id)
    return {"version_id": version_id, "status": "active", "active_version": c.risk_config.version}


@router.get("/audit", tags=["audit"])
def audit_log(limit: int = Query(200, ge=1, le=2000), user_id: str | None = Query(default=None, max_length=80),
              action: str | None = Query(default=None, pattern="^[a-z_]{2,40}$"), c: Any = Depends(get_container),
              p: Principal = Depends(get_principal)) -> list[dict[str, Any]]:
    if not p.has("admin"):
        user_id = p.user_id  # analysts see their own trail only
    return present(c, p, c.store.list_audit(limit=limit, user_id=user_id, action=action))
