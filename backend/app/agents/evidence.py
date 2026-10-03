"""Evidence fusion: turn tool outputs into citable EvidenceItems (E1, E2, ...).

Each item records where it came from (source_type/source_id), what it is
(evidence_type), a JSON content payload containing the facts, and a confidence.
Reports may only cite these refs.
"""
from __future__ import annotations

from typing import Any

from app.data.store import new_id, utcnow
from app.schemas.domain import EvidenceItem
from app.security.masking import mask_payload

TXN_FIELDS = ["transaction_id", "timestamp", "amount", "currency", "amount_usd", "transaction_type", "channel",
              "country", "sender_account_id", "receiver_account_id", "merchant_id", "external_counterparty",
              "device_id", "ip_address", "status"]


class EvidenceBuilder:
    def __init__(self, investigation_id: str):
        self.investigation_id = investigation_id
        self.items: list[EvidenceItem] = []
        self.index: dict[str, str] = {}  # semantic key -> ref

    def add(self, key: str, source_type: Any, source_id: str, evidence_type: str, title: str,
            content: dict[str, Any], confidence: float) -> str:
        if key in self.index:
            return self.index[key]
        ref = f"E{len(self.items) + 1}"
        self.items.append(EvidenceItem(
            evidence_id=new_id("EV"), investigation_id=self.investigation_id, ref=ref, source_type=source_type,
            source_id=source_id, evidence_type=evidence_type, title=title,
            content=mask_payload(content), confidence=round(float(confidence), 3), created_at=utcnow()))
        self.index[key] = ref
        return ref


def _txn_dict(t: Any) -> dict[str, Any]:
    d = t.model_dump(mode="json") if hasattr(t, "model_dump") else dict(t)
    return {k: d.get(k) for k in TXN_FIELDS if d.get(k) is not None}


def fuse(investigation_id: str, data: dict[str, Any]) -> tuple[list[EvidenceItem], dict[str, Any]]:
    """Build evidence from collected tool outputs. Returns (items, ref_map)."""
    b = EvidenceBuilder(investigation_id)
    refs: dict[str, Any] = {"signals": {}, "documents": {}, "doc_by_signal": {}}

    prof = data.get("customer_profile")
    if prof is not None:
        c = prof.customer
        refs["customer"] = b.add(
            "customer", "database", c.customer_id, "customer_profile", f"Customer profile {c.customer_id}",
            {"customer": c.model_dump(mode="json"),
             "accounts": [a.model_dump(mode="json") for a in prof.accounts],
             "identifier_types": sorted({i.identifier_type for i in prof.identifiers}),
             "customers_sharing_identifiers": len(prof.shared_identifier_customers)}, 1.0)

    stats = data.get("statistics")
    if stats is not None:
        keep = ["period_start", "period_end", "n_outbound", "n_inbound", "n_failed_outbound", "outbound_usd",
                "inbound_usd", "daily_outbound_mean", "daily_outbound_max", "max_outbound_60min", "countries",
                "devices", "distinct_inbound_counterparties"]
        refs["statistics"] = b.add(
            "statistics", "metric", stats.customer_id, "behaviour_statistics",
            "Behavioural statistics: investigation window vs baseline",
            {"window": {k: stats.window.get(k) for k in keep}, "baseline": {k: stats.baseline.get(k) for k in keep}},
            0.95)

    assessment = data.get("assessment")
    txns = data.get("transactions")
    txn_lookup = {t.transaction_id: t for t in (txns.transactions if txns is not None else [])}
    txn_lookup.update(data.get("extra_transactions") or {})
    if assessment is not None:
        for s in assessment.signals:
            src = "ml" if s.signal_type == "ML_ANOMALY" else "metric"
            refs["signals"][s.signal_type] = b.add(
                f"signal:{s.signal_type}", src, s.signal_id, f"risk_signal:{s.signal_type}",
                f"Risk signal {s.signal_type} ({s.severity})", s.model_dump(mode="json"), s.confidence)
        key_txn_ids: list[str] = []
        for s in assessment.signals:
            for e in s.evidence:
                if e.kind == "transaction" and e.id not in key_txn_ids:
                    key_txn_ids.append(e.id)
        key_txns = [_txn_dict(txn_lookup[t]) for t in key_txn_ids[:40] if t in txn_lookup]
        if key_txns:
            refs["key_transactions"] = b.add(
                "key_transactions", "database", ",".join(t["transaction_id"] for t in key_txns[:5]), "transactions",
                f"{len(key_txns)} transactions referenced by risk signals", {"transactions": key_txns}, 1.0)
        refs["risk_score"] = b.add(
            "risk_score", "metric", f"{assessment.entity_type}:{assessment.entity_id}", "risk_score",
            f"Risk score {assessment.score} ({assessment.band})",
            {"score": assessment.score, "band": assessment.band, "flagged": assessment.flagged,
             "investigation_threshold": assessment.investigation_threshold,
             "contributors": [c.model_dump() for c in assessment.contributors],
             "signals_fired": [s.signal_type for s in assessment.signals], "n_signals": len(assessment.signals),
             "not_evaluated": [n.model_dump() for n in assessment.not_evaluated],
             "data_quality": assessment.data_quality, "config_version": assessment.config_version,
             "config_fingerprint": assessment.config_fingerprint,
             "window_start": assessment.window_start.isoformat(), "window_end": assessment.window_end.isoformat()},
            1.0)

    anomalies = data.get("anomalies")
    if anomalies is not None and anomalies.anomalies:
        refs["anomalies"] = b.add("anomalies", "metric", anomalies.customer_id, "transaction_anomalies",
                                  f"{len(anomalies.anomalies)} transaction-level outliers",
                                  anomalies.model_dump(mode="json"), 0.8)

    cluster = data.get("cluster")
    if cluster is not None and cluster.members:
        refs["cluster"] = b.add(
            "cluster", "graph", cluster.center, "network_cluster", "Network cluster around the subject",
            {"customers": cluster.customers[:50], "n_customers": len(cluster.customers),
             "n_accounts": len(cluster.accounts), "devices": cluster.devices[:30], "density": cluster.density,
             "flagged_customers": cluster.flagged_customers, "n_flagged": len(cluster.flagged_customers),
             "n_shared_devices": len(cluster.shared_devices), "central_entities": cluster.central_entities,
             "shared_devices": [s.model_dump() for s in cluster.shared_devices]}, 0.9)
    shared = data.get("shared_devices")
    if shared is not None and shared.shared_devices:
        refs["shared_devices"] = b.add("shared_devices", "graph", shared.customer_id, "shared_devices",
                                       "Devices shared with other customers", shared.model_dump(mode="json"), 0.95)
    flows = data.get("fund_flows")
    if flows is not None and flows.flows:
        refs["fund_flows"] = b.add("fund_flows", "graph", flows.account_id, "fund_flows",
                                   f"Outbound fund flows from {flows.account_id}",
                                   {"account_id": flows.account_id, "direction": flows.direction,
                                    "flows": [f.model_dump() for f in flows.flows[:8]]}, 0.85)
    connected = data.get("connected_accounts")
    if connected is not None and connected.connected:
        refs["connected_accounts"] = b.add(
            "connected_accounts", "graph", connected.account_id, "connected_accounts",
            f"{len(connected.connected)} accounts connected to {connected.account_id}",
            {"account_id": connected.account_id, "connected": [c.model_dump() for c in connected.connected[:30]]}, 0.9)

    for item in data.get("passages") or []:
        p = item["passage"]
        r = b.add(f"doc:{p.chunk_id}", "document", p.chunk_id, "policy_passage",
                  f"{p.title} — {p.section or 'passage'}",
                  {"document_id": p.document_id, "title": p.title, "doc_type": p.doc_type, "section": p.section,
                   "page": p.page, "chunk_id": p.chunk_id, "source": p.source, "text": p.text,
                   "retrieval": {"query": item["query"], "score": p.score, "semantic_rank": p.semantic_rank,
                                 "keyword_rank": p.keyword_rank}}, 0.7)
        refs["documents"][p.chunk_id] = r
        for sig in item.get("signals", []):
            refs["doc_by_signal"].setdefault(sig, [])
            if r not in refs["doc_by_signal"][sig]:
                refs["doc_by_signal"][sig].append(r)

    memory = data.get("memory")
    if memory is not None and (memory.get("direct") or memory.get("connected") or memory.get("similar")):
        refs["history"] = b.add("history", "history", "investigations", "previous_investigations",
                                "Previous investigations and analyst conclusions",
                                {**memory, "n_direct": len(memory.get("direct") or []),
                                 "n_connected": len(memory.get("connected") or []),
                                 "n_similar": len(memory.get("similar") or [])}, 0.8)
    return b.items, refs
