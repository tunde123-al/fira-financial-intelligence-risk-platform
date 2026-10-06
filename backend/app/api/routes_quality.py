"""Data-quality ledger: ingestion batches, quarantined (rejected) rows and the coverage / success totals."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query

from app.api.deps import get_container, require_role
from app.api.routes_monitoring import service_errors
from app.security.principal import Principal

router = APIRouter(prefix="/api/data-quality", tags=["data-quality"])
BATCH_ID = Path(pattern=r"^BAT-[0-9A-F]{4,16}$")
REASON_GROUPS = r"^(malformed|duplicate|invalid|referential)$"


@router.get("/dataset", summary="Audit of the STORED dataset: valid / duplicate / invalid / orphan counts and a quality score, computed on demand")
def dataset(c: Any = Depends(get_container), p: Principal = Depends(require_role("analyst"))) -> dict[str, Any]:
    with service_errors():
        return c.store.dataset_quality()


@router.get("/summary", summary="Coverage, processing success and rejection breakdown (computed from the batch ledger)")
def summary(c: Any = Depends(get_container), p: Principal = Depends(require_role("analyst"))) -> dict[str, Any]:
    with service_errors():
        return c.monitoring.data_quality_summary()


@router.get("/batches", summary="Ingestion batches, newest first")
def batches(limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0, le=100_000),
            c: Any = Depends(get_container), p: Principal = Depends(require_role("analyst"))) -> dict[str, Any]:
    with service_errors():
        return c.monitoring.list_batches(limit, offset)


@router.get("/batches/{batch_id}", summary="One batch with its accounting")
def batch(batch_id: str = BATCH_ID, c: Any = Depends(get_container),
          p: Principal = Depends(require_role("analyst"))) -> dict[str, Any]:
    with service_errors():
        return c.monitoring.get_batch(batch_id)


@router.get("/rejected", summary="Quarantined rows with reason codes (sanitised copies of the submitted rows)")
def rejected(batch_id: str | None = Query(None, pattern=r"^BAT-[0-9A-F]{4,16}$"),
             reason_code: str | None = Query(None, pattern=r"^[A-Z_]{3,40}$"),
             reason_group: str | None = Query(None, pattern=REASON_GROUPS),
             limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0, le=100_000),
             c: Any = Depends(get_container), p: Principal = Depends(require_role("analyst"))) -> dict[str, Any]:
    with service_errors():
        return c.monitoring.list_rejected(batch_id, reason_code, reason_group, limit, offset)
