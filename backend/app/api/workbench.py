"""Assembly of the investigation workbench for one case.

Everything is read from existing components (store, risk engine, graph, retrieval, the agent's stored
investigation). No new analytics are computed here and nothing is written.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pandas as pd

from app.agents.report import SIGNAL_PLAYBOOK
from app.api.deps import present, run_tool
from app.monitoring import evidence as ev
from app.monitoring.activity import activity_windows
from app.monitoring.repository import AlertFilter
from app.monitoring.risk_view import risk_view
from app.security.principal import Principal


def _d(x: Any) -> Any:
    return x.model_dump(mode="json") if hasattr(x, "model_dump") else x


def frame_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    out = []
    for r in df.to_dict("records"):
        rec: dict[str, Any] = {}
        for k, v in r.items():
            if isinstance(v, pd.Timestamp):
                rec[k] = v.isoformat()
            elif v is None or (isinstance(v, float) and v != v) or v is pd.NaT:
                rec[k] = None
            elif hasattr(v, "item"):
                rec[k] = v.item()
            else:
                rec[k] = v
        out.append(rec)
    return out


def _timeline(c: Any, case: Any, alerts: list[Any]) -> list[dict[str, Any]]:
    repo = c.monitoring_repo
    rows: list[dict[str, Any]] = []
    for e in repo.case_events(case.case_id):
        rows.append({"ts": e.ts.isoformat(), "source": "case", "actor": e.actor, "event": e.event_type,
                     "from": e.from_status, "to": e.to_status, "detail": e.detail})
    for a in alerts:
        for e in repo.alert_events(a.alert_id):
            rows.append({"ts": e.ts.isoformat(), "source": "alert", "alert_id": a.alert_id,
                         "detector_id": a.detector_id, "actor": e.actor, "event": e.event_type,
                         "from": e.from_status, "to": e.to_status, "detail": e.detail})
    for n in repo.notes(case.case_id):
        rows.append({"ts": n.created_at.isoformat(), "source": "note", "actor": n.author, "event": "note",
                     "detail": {"note_id": n.note_id, "body": n.body}})
    rows.sort(key=lambda r: r["ts"])
    return rows


def build_workbench(c: Any, p: Principal, case_id: str) -> dict[str, Any]:
    repo = c.monitoring_repo
    case = repo.get_case(case_id)
    if case is None:
        raise LookupError(f"case {case_id} not found")
    cid = case.customer_id
    store = c.store
    cfg = c.monitoring_config
    alerts = repo.alerts_for_case(case_id)
    end = store.as_of()
    ws = end - timedelta(days=cfg.lookback_days)

    customer = run_tool(c, p, "get_customer", {"customer_id": cid})
    assessment = run_tool(c, p, "get_risk_signals", {"entity_type": "customer", "entity_id": cid,
                                                     "lookback_days": cfg.lookback_days})
    risk = risk_view(assessment)

    accounts = [a.account_id for a in store.accounts_for_customer(cid)]
    frame = store.transactions_for_accounts(accounts, ws, end)
    related_ids = {t for a in alerts for t in a.transaction_ids}
    related = frame[frame.transaction_id.isin(related_ids)] if related_ids else frame.iloc[0:0]
    recent = frame.sort_values("timestamp", ascending=False).head(50)

    graph = c.graph
    if graph is not None and hasattr(graph, "customer_counterparties"):
        first = graph.customer_counterparties(cid, 1, ws, 60)
        second = [r for r in graph.customer_counterparties(cid, 2, ws, 60) if r["degree"] == 2]
        shared = graph.shared_beneficiaries(cid, ws, 1, 10)
    else:
        first, second, shared = [], [], []
    subgraph = run_tool(c, p, "get_related_entities", {"kind": "customer", "entity_id": cid, "depth": 2, "limit": 120})
    cluster = run_tool(c, p, "find_suspicious_cluster", {"customer_id": cid})

    docs: dict[str, dict[str, Any]] = {}
    for det in sorted({a.detector_id for a in alerts})[:4]:
        pb = SIGNAL_PLAYBOOK.get(det)
        if not pb:
            continue
        res = run_tool(c, p, "search_documents", {"query": pb["query"], "k": 3})
        for ps in _d(res)["passages"]:
            docs.setdefault(ps["chunk_id"], {**ps, "retrieved_for": det,
                                             "evidence_class": ev.DOCUMENT_EVIDENCE})

    others, _ = repo.list_alerts(AlertFilter(customer_id=cid, limit=100))
    in_case = {a.alert_id for a in alerts}
    related_alerts = [a.model_dump(mode="json") for a in others if a.alert_id not in in_case]
    legacy = [a.model_dump(mode="json") for a in store.list_alerts(entity_id=cid, limit=50)]

    inv = store.get_investigation(case.investigation_id) if case.investigation_id else None
    inv_evidence = [ev.classify_item(e.model_dump(mode="json")) for e in store.list_evidence(inv.investigation_id)] if inv else []
    narrative = ev.classify_claims((inv.report or {}).get("executive_summary", [])) if inv and inv.report else []
    attached = []
    for e in repo.case_evidence(case_id):
        d = e.model_dump(mode="json")
        d["evidence_class_label"] = ev.LABELS[e.evidence_class]
        attached.append(d)
    rule_results = [{"alert_id": a.alert_id, "detector_id": a.detector_id, "evidence_class": ev.RULE_RESULT,
                     "evidence_class_label": ev.LABELS[ev.RULE_RESULT], "explanation": a.explanation,
                     "transaction_ids": a.transaction_ids} for a in alerts]

    payload = {
        "case": case.model_dump(mode="json"),
        "customer": _d(customer),
        "risk": risk,
        "triggered_rules": [a.model_dump(mode="json") for a in alerts],
        "related_transactions": frame_records(related),
        "recent_transactions": frame_records(recent),
        "account_activity": activity_windows(store, cid, end, cfg.baseline_days),
        "counterparties": {"direct": first, "second_degree": second, "shared_beneficiaries": shared,
                           "note": "account-to-account transfers only; external beneficiaries are not graph nodes"},
        "network": {"graph": _d(subgraph), "cluster": _d(cluster)},
        "related_alerts": {"monitoring": related_alerts, "legacy_seeded": legacy},
        "documents": list(docs.values()),
        "evidence": {"rule_results": rule_results, "attached": attached, "investigation": inv_evidence,
                     "classes": {k: ev.LABELS[k] for k in ev.CLASSES}},
        "investigation": ({"investigation_id": inv.investigation_id, "status": inv.status,
                           "conclusion": inv.conclusion, "risk_score": inv.risk_score,
                           "narrative": narrative, "llm_provider": c.settings.llm_provider,
                           "narrative_note": "Claims marked ai_generated were drafted by an LLM from stored evidence; "
                                             "they are not a source of fact."} if inv else None),
        "notes": [n.model_dump(mode="json") for n in repo.notes(case_id)],
        "timeline": _timeline(c, case, alerts),
        "decision": {"decision": case.decision, "reason": case.decision_reason, "decided_by": case.decided_by,
                     "closed_at": case.closed_at.isoformat() if case.closed_at else None},
    }
    return present(c, p, payload)
