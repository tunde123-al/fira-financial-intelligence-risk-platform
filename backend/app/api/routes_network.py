"""Graph investigation questions and customer activity windows."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.api.deps import audit, get_container, get_principal
from app.api.routes_monitoring import service_errors
from app.monitoring.activity import WINDOWS, activity_windows
from app.risk.engine import _verify_temporal_cycle
from app.security.principal import Principal

router = APIRouter(prefix="/api", tags=["network"])
CUST = Path(pattern=r"^CUST-\d{1,10}$")


def _graph(c: Any, method: str) -> Any:
    g = c.graph
    if g is None:
        raise HTTPException(503, "graph backend unavailable")
    if not hasattr(g, method):
        raise HTTPException(501, f"'{method}' is not implemented for the {getattr(g, 'name', '?')} graph backend")
    return g


def _since(c: Any, days: int) -> datetime:
    return c.store.as_of() - timedelta(days=days)


@router.get("/network/customers/{customer_id}/counterparties",
            summary="Direct (degree 1) and second-degree (degree 2) transfer counterparties")
def counterparties(customer_id: str = CUST, degree: int = Query(1, ge=1, le=2), days: int = Query(90, ge=1, le=365),
                   limit: int = Query(100, ge=1, le=300), c: Any = Depends(get_container),
                   p: Principal = Depends(get_principal)) -> dict[str, Any]:
    g = _graph(c, "customer_counterparties")
    if c.store.get_customer(customer_id) is None:
        raise HTTPException(404, f"customer {customer_id} not found")
    rows = g.customer_counterparties(customer_id, degree, _since(c, days), limit)
    audit(c, p, "graph_query", "ok", "customer", customer_id, query="counterparties", degree=degree)
    return {"customer_id": customer_id, "degree": degree, "days": days, "counterparties": rows,
            "note": "account-to-account transfers only; external beneficiaries are not graph nodes"}


@router.get("/network/customers/{customer_id}/shared-beneficiaries",
            summary="Beneficiary accounts this customer pays that other customers also pay")
def shared_beneficiaries(customer_id: str = CUST, days: int = Query(90, ge=1, le=365),
                         limit: int = Query(50, ge=1, le=200), c: Any = Depends(get_container),
                         p: Principal = Depends(get_principal)) -> dict[str, Any]:
    g = _graph(c, "shared_beneficiaries")
    if c.store.get_customer(customer_id) is None:
        raise HTTPException(404, f"customer {customer_id} not found")
    rows = g.shared_beneficiaries(customer_id, _since(c, days), 1, limit)
    audit(c, p, "graph_query", "ok", "customer", customer_id, query="shared_beneficiaries")
    return {"customer_id": customer_id, "days": days, "shared_beneficiaries": rows}


@router.get("/network/customers/{customer_id}/cycles", summary="Circular transfer paths through the customer's accounts")
def cycles(customer_id: str = CUST, max_length: int = Query(5, ge=2, le=6), days: int = Query(30, ge=1, le=365),
           c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    g = _graph(c, "candidate_cycles")
    if c.store.get_customer(customer_id) is None:
        raise HTTPException(404, f"customer {customer_id} not found")
    accounts = sorted(a.account_id for a in c.store.accounts_for_customer(customer_id))
    end = c.store.as_of()
    start = end - timedelta(days=days)
    cfg = c.risk_config.signals.get("CIRCULAR_FLOW")
    import pandas as pd

    gap = pd.Timedelta(hours=float(cfg.p("max_hop_gap_hours", 72)) if cfg else 72)
    tol = float(cfg.p("amount_tolerance", 0.3)) if cfg else 0.3
    out = []
    for cyc in g.candidate_cycles(accounts, max_length, since=start)[:50]:
        tx = c.store.transactions_for_accounts(cyc, start, end)
        tx = tx[(tx.status == "completed") & tx.sender_account_id.isin(cyc) & tx.receiver_account_id.isin(cyc)]
        chain = _verify_temporal_cycle(cyc, tx, gap, tol)
        out.append({"accounts": cyc, "length": len(cyc), "time_ordered": chain is not None,
                    "transactions": chain.transaction_id.tolist() if chain is not None else [],
                    "start_usd": round(float(chain.amount_usd.iloc[0]), 2) if chain is not None else None})
    audit(c, p, "graph_query", "ok", "customer", customer_id, query="cycles")
    return {"customer_id": customer_id, "cycles": out,
            "note": "time_ordered=true means each hop follows the previous within the configured gap and amount tolerance"}


@router.get("/network/common-recipients", summary="Accounts that receive funds from several of the given accounts")
def common_recipients(accounts: str = Query(pattern=r"^ACC-\d{1,10}(,ACC-\d{1,10}){0,49}$"),
                      min_sources: int = Query(2, ge=2, le=50), days: int = Query(90, ge=1, le=365),
                      c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    g = _graph(c, "common_recipients")
    ids = accounts.split(",")
    rows = g.common_recipients(ids, min_sources, _since(c, days), 50)
    audit(c, p, "graph_query", "ok", "accounts", None, query="common_recipients", n_accounts=len(ids))
    return {"accounts": ids, "min_sources": min_sources, "recipients": rows}


@router.get("/network/flagged-recipients/{customer_id}",
            summary="Accounts receiving funds from several accounts in this customer's flagged cluster")
def flagged_recipients(customer_id: str = CUST, days: int = Query(90, ge=1, le=365),
                       c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    """Uses the existing cluster analysis to pick the flagged customers' accounts, then finds common recipients."""
    g = _graph(c, "common_recipients")
    cluster = g.suspicious_cluster(customer_id)
    flagged = [x for x in cluster.flagged_customers] + [customer_id]
    accounts = sorted({a.account_id for cid in flagged for a in c.store.accounts_for_customer(cid)})
    rows = g.common_recipients(accounts, 2, _since(c, days), 50) if accounts else []
    audit(c, p, "graph_query", "ok", "customer", customer_id, query="flagged_recipients")
    return {"customer_id": customer_id, "flagged_customers": sorted(set(flagged)), "recipients": rows,
            "note": "flagged = customers with an open legacy alert inside this customer's cluster, plus the customer"}


@router.get("/customers/{customer_id}/activity-windows",
            summary="Counts, value and counterparties in 1 h / 24 h / 7 d / 30 d windows versus the baseline")
def customer_activity_windows(customer_id: str = CUST, end: datetime | None = None,
            windows: str = Query(default=",".join(WINDOWS), pattern=r"^(1h|24h|7d|30d)(,(1h|24h|7d|30d))*$"),
            baseline_days: int = Query(90, ge=7, le=365), c: Any = Depends(get_container),
            p: Principal = Depends(get_principal)) -> dict[str, Any]:
    if c.store.get_customer(customer_id) is None:
        raise HTTPException(404, f"customer {customer_id} not found")
    return activity_windows(c.store, customer_id, end, baseline_days, windows.split(","))


# ---------------------------------------------------------------------------------- money-mule indicators
mule = APIRouter(prefix="/api/mule", tags=["investigations"])


@mule.get("/customers/{customer_id}", summary="Money-mule risk indicators with supporting evidence (not a verdict)")
def mule_assessment(customer_id: str = CUST, end: datetime | None = None, c: Any = Depends(get_container),
                    p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        out = c.monitoring.mule_assessment(customer_id, end)
    audit(c, p, "mule_assessment", "ok", "customer", customer_id, band=out["band"], score=out["score"])
    return out


@mule.get("/customers/{customer_id}/flow", summary="Money-flow subgraph: accounts, amounts, times, direction and depth")
def mule_flow(customer_id: str = CUST, depth: int = Query(2, ge=1, le=3), days: int = Query(30, ge=1, le=365),
              c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        out = c.monitoring.mule_flow(customer_id, depth, days)
    audit(c, p, "graph_query", "ok", "customer", customer_id, query="mule_flow", depth=depth)
    return out


@mule.get("/patterns", summary="Search the transfer graph for fan-in / fan-out accounts")
def mule_patterns(days: int = Query(30, ge=1, le=365), min_degree: int = Query(5, ge=2, le=200),
                  limit: int = Query(50, ge=1, le=200), c: Any = Depends(get_container),
                  p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        out = c.monitoring.mule_patterns(days, min_degree, limit)
    audit(c, p, "graph_query", "ok", "graph", None, query="fan_patterns", min_degree=min_degree)
    return out


@mule.get("/suspects", summary="Customers with fund-flow alerts ranked by mule-indicator score")
def mule_suspects(limit: int = Query(25, ge=1, le=100), min_band: str = Query("LOW", pattern="^(NONE|LOW|MEDIUM|HIGH)$"),
                  c: Any = Depends(get_container), p: Principal = Depends(get_principal)) -> dict[str, Any]:
    with service_errors():
        return c.monitoring.mule_suspects(limit, min_band)
