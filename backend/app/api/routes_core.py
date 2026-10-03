"""Health, auth, search, entity, alert, risk and dashboard endpoints."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from pydantic import BaseModel, Field

from app.api.deps import audit, get_container, get_principal, present, require_role, run_tool
from app.core.observability import METRICS
from app.schemas.domain import ID_PATTERNS
from app.security.auth import AuthenticationError, authenticate, create_token
from app.security.principal import Principal

router = APIRouter()


# ------------------------------------------------------------------ health
@router.get("/health", tags=["system"], summary="Liveness probe")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", tags=["system"], summary="Readiness: dependency checks")
def ready(response: Response, c: Any = Depends(get_container)) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    for name, fn in (("database", c.store.ping), ("graph", (c.graph.ping if c.graph else lambda: False)),
                     ("vector_store", c.vectors.ping)):
        try:
            checks[name] = "ok" if fn() else "down"
        except Exception as e:  # report, never raise
            checks[name] = f"down ({type(e).__name__})"
    # The vector store holds chunks, not documents: report both counts under accurate names.
    vector_ok = checks.get("vector_store") == "ok"
    checks["indexed_chunks"] = c.vectors.count() if vector_ok else 0
    try:
        checks["source_documents"] = len(c.doc_repo.documents())
    except Exception:  # report, never raise
        checks["source_documents"] = 0
    checks["llm_provider"] = c.settings.llm_provider
    checks["agent_engine"] = c.agent.engine_name
    checks["graph_backend"] = getattr(c.graph, "name", None)
    healthy = all(checks[k] == "ok" for k in ("database", "graph", "vector_store"))
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if healthy else "degraded", "checks": checks}


@router.get("/metrics", tags=["system"], summary="Prometheus metrics (admin)")
def metrics(_: Principal = Depends(require_role("admin"))) -> Response:
    return Response(METRICS.render(), media_type="text/plain; version=0.0.4")


# -------------------------------------------------------------------- auth
class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@router.post("/api/auth/login", tags=["auth"])
def login(body: LoginIn, request: Request, c: Any = Depends(get_container)) -> dict[str, Any]:
    ip = request.client.host if request.client else "unknown"
    try:
        user = authenticate(c.store, body.username, body.password)
    except AuthenticationError:
        audit(c, None, "login", "denied", "user", body.username[:64], ip=ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials") from None
    tok = create_token(c.settings, user["user_id"], user["username"], user["role"])
    audit(c, Principal(user["user_id"], user["role"]), "login", "ok", "user", user["username"], ip=ip)
    return {**tok, "username": user["username"]}


@router.get("/api/auth/me", tags=["auth"])
def me(p: Principal = Depends(get_principal)) -> dict[str, str]:
    return {"user_id": p.user_id, "role": p.role}


# ------------------------------------------------------------------ search
@router.get("/api/search", tags=["search"], summary="Search customers, accounts, transactions, merchants, devices")
def search(q: str = Query(min_length=2, max_length=64), entity_type: str | None = Query(
        default=None, pattern="^(customer|account|transaction|merchant|device)$"),
        limit: int = Query(20, ge=1, le=100), c: Any = Depends(get_container),
        p: Principal = Depends(get_principal)) -> list[dict[str, Any]]:
    return present(c, p, c.store.search(q, entity_type, limit))


# ---------------------------------------------------------------- entities
@router.get("/api/customers/{customer_id}", tags=["entities"])
def get_customer(customer_id: str = Path(pattern=ID_PATTERNS["customer"]), unmask: bool = False,
                 c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    data = run_tool(c, p, "get_customer", {"customer_id": customer_id})
    if unmask and p.has("admin"):
        audit(c, p, "unmask", "ok", "customer", customer_id)
    out = present(c, p, data, unmask)
    out["alerts"] = present(c, p, c.store.list_alerts(entity_id=customer_id, limit=50))
    out["investigations"] = present(c, p, [i.model_copy(update={"report": None}) for i in
                                           c.store.list_investigations(subject_ids=[customer_id], limit=50)])
    return out


@router.get("/api/customers/{customer_id}/transactions", tags=["entities"])
def customer_transactions(customer_id: str = Path(pattern=ID_PATTERNS["customer"]),
                          lookback_days: int = Query(90, ge=1, le=730), limit: int = Query(200, ge=1, le=2000),
                          c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return present(c, p, run_tool(c, p, "get_transactions", {"customer_id": customer_id,
                                                              "lookback_days": lookback_days, "limit": limit}))


@router.get("/api/customers/{customer_id}/statistics", tags=["entities"])
def customer_statistics(customer_id: str = Path(pattern=ID_PATTERNS["customer"]),
                        lookback_days: int = Query(30, ge=1, le=365), c: Any = Depends(get_container),
                        p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return present(c, p, run_tool(c, p, "get_transaction_statistics",
                                  {"customer_id": customer_id, "lookback_days": lookback_days}))


@router.get("/api/accounts/{account_id}", tags=["entities"])
def get_account(account_id: str = Path(pattern=ID_PATTERNS["account"]), c: Any = Depends(get_container),
                p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return present(c, p, run_tool(c, p, "get_account", {"account_id": account_id}))


@router.get("/api/transactions/{transaction_id}", tags=["entities"])
def get_transaction(transaction_id: str = Path(pattern=ID_PATTERNS["transaction"]), c: Any = Depends(get_container),
                    p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return present(c, p, run_tool(c, p, "get_transaction", {"transaction_id": transaction_id}))


@router.get("/api/merchants/{merchant_id}", tags=["entities"])
def get_merchant(merchant_id: str = Path(pattern=ID_PATTERNS["merchant"]), c: Any = Depends(get_container),
                 p: Principal = Depends(get_principal)) -> dict[str, Any]:
    m = c.store.get_merchant(merchant_id)
    if m is None:
        raise HTTPException(404, f"merchant {merchant_id} not found")
    return present(c, p, m)


@router.get("/api/devices/{device_id}", tags=["entities"])
def get_device(device_id: str = Path(pattern=ID_PATTERNS["device"]), c: Any = Depends(get_container),
               p: Principal = Depends(get_principal)) -> dict[str, Any]:
    d = c.store.get_device(device_id)
    if d is None:
        raise HTTPException(404, f"device {device_id} not found")
    users = c.store.device_users([device_id], None, None)
    out = present(c, p, d)
    out["users"] = [{"customer_id": r.customer_id, "n_transactions": int(r.n_transactions),
                     "first_ts": str(r.first_ts), "last_ts": str(r.last_ts)} for r in users.itertuples()]
    return out


# ------------------------------------------------------------ alerts, risk
@router.get("/api/alerts", tags=["alerts"])
def list_alerts(status_: str | None = Query(default=None, alias="status", pattern="^[a-z_]{2,40}$"),
                entity_id: str | None = Query(default=None, max_length=40), limit: int = Query(100, ge=1, le=1000),
                c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> list[dict[str, Any]]:
    return present(c, p, c.store.list_alerts(entity_id=entity_id, status=status_, limit=limit))


@router.get("/api/risk/{entity_type}/{entity_id}", tags=["risk"], summary="Explainable deterministic risk assessment")
def risk(entity_type: Literal["customer", "account"], entity_id: str = Path(pattern=r"^(CUST|ACC)-\d{1,10}$"),
         lookback_days: int = Query(30, ge=1, le=365), c: Any = Depends(get_container),
         p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return present(c, p, run_tool(c, p, "get_risk_signals", {"entity_type": entity_type, "entity_id": entity_id,
                                                               "lookback_days": lookback_days}))


@router.get("/api/dashboard", tags=["dashboard"])
def dashboard(c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    stats = c.store.dashboard_stats()
    invs = c.store.list_investigations(limit=15)
    stats["recent_investigations"] = present(c, p, [i.model_copy(update={"report": None}) for i in invs])
    stats["open_alerts"] = present(c, p, c.store.list_alerts(status="open", limit=15))
    stats["system"] = {"graph_backend": getattr(c.graph, "name", None), "vector_backend": c.vectors.name,
                       "indexed_chunks": c.vectors.count(), "source_documents": len(c.doc_repo.documents()),
                       "llm_provider": c.settings.llm_provider,
                       "agent_engine": c.agent.engine_name, "risk_config": c.risk_config.version,
                       "metrics": METRICS.snapshot()["counters"]}
    return stats


@router.get("/api/tools", tags=["agent"], summary="Tools available to the caller")
def tools(c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> list[dict[str, Any]]:
    return c.registry.list(p)
