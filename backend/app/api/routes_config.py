"""Configuration governance endpoints: current governed settings, the change log, runtime overrides."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import audit, get_container, get_principal, require_role
from app.monitoring import governance as gov
from app.security.principal import Principal

router = APIRouter(prefix="/api/config", tags=["config"])


class OverrideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=3, max_length=80, pattern=r"^[a-z_]+(\.[a-z_]+)+$")
    value: Any
    reason: str = Field(min_length=5, max_length=500)


class DetectorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    reason: str = Field(min_length=5, max_length=500)


@router.get("/monitoring", summary="Current monitoring configuration and which settings can change at runtime")
def current(c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    cfg = c.monitoring_config
    return {"version": cfg.version, "fingerprint": cfg.fingerprint(), "config": cfg.model_dump(mode="json"),
            "runtime_overridable": list(gov.OVERRIDABLE),
            "risk_config": {"version": c.risk_config.version, "fingerprint": c.risk_config.fingerprint()}}


@router.get("/changes", summary="Configuration change log (name, path, old, new, who, when, why, source)")
def changes(limit: int = Query(100, ge=1, le=500), c: Any = Depends(get_container),
            p: Principal = Depends(get_principal)) -> list[dict[str, Any]]:
    rows = [r for r in c.monitoring_repo.list_config_changes(limit * 3) if r.path != gov.SNAPSHOT_PATH][:limit]
    return [r.model_dump(mode="json") for r in rows]


@router.post("/monitoring", summary="Change one monitoring setting in the running process (admin; validated, logged)")
def override(body: OverrideBody, c: Any = Depends(get_container),
             p: Principal = Depends(require_role("admin"))) -> dict[str, Any]:
    try:
        out = gov.apply_override(c, body.path, body.value, body.reason, p.user_id)
    except ValueError as e:
        audit(c, p, "config_changed", "denied", "config", body.path, reason=str(e)[:200])
        raise HTTPException(422, str(e)[:300]) from None
    audit(c, p, "config_changed", "ok", "config", body.path, reason=body.reason, changes=len(out["changed"]))
    return out


@router.post("/detectors/{detector_id}", summary="Enable or disable alerting for one detector (admin; logged)")
def toggle_detector(body: DetectorBody, detector_id: str = Path(pattern=r"^[A-Z_]{2,40}$"),
                    c: Any = Depends(get_container), p: Principal = Depends(require_role("admin"))) -> dict[str, Any]:
    try:
        out = gov.set_detector_enabled(c, detector_id, body.enabled, body.reason, p.user_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except ValueError as e:
        raise HTTPException(422, str(e)[:300]) from None
    audit(c, p, "detector_enabled" if body.enabled else "detector_disabled", "ok", "detector", detector_id,
          reason=body.reason)
    return out
