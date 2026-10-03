"""Agent memory.

Two deliberately separate concepts:

* **Episodic memory** (this module): what happened before — previous
  investigations on the subject or connected entities, their signals and the
  *human* conclusions. It is recalled as evidence and shown to the analyst. It
  never changes scores or thresholds by itself.
* **Semantic knowledge**: policies, procedures and case notes in the retrieval
  index (Qdrant / hybrid retriever).

Neither is *learning*. Changes to how FIRA scores risk happen only through the
controlled improvement loop in `app.evaluation.improvement` (proposal -> offline
regression -> human approval).
"""
from __future__ import annotations

from typing import Any

from app.tools.registry import ToolContext, ToolRegistry


def _summ(item: Any) -> dict[str, Any]:
    inv = item.investigation
    return {"investigation_id": inv.investigation_id, "subject_id": inv.subject_id, "subject_type": inv.subject_type,
            "status": inv.status, "conclusion": inv.conclusion, "signals": list(inv.signals),
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
            "closed_at": inv.closed_at.isoformat() if inv.closed_at else None, "summary": inv.summary,
            "human_decisions": [{"decision": d.decision, "rationale": d.rationale, "by": d.decided_by,
                                 "at": d.created_at.isoformat()} for d in item.decisions]}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


class EpisodicMemory:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def recall(self, ctx: ToolContext, subject_id: str, connected_ids: list[str], signal_types: list[str],
               k: int = 5) -> tuple[dict[str, Any], list[Any]]:
        """Returns (memory, tool_call_records)."""
        calls = []
        mem: dict[str, Any] = {"direct": [], "connected": [], "similar": [], "notes": []}
        r = self.registry.invoke("get_previous_investigations", {"subject_ids": [subject_id], "limit": 20}, ctx)
        calls.append(r)
        if r.ok:
            mem["direct"] = [_summ(i) for i in r.data.items if i.investigation.status in ("closed", "pending_review")]
        others = [c for c in connected_ids if c != subject_id][:100]
        if others:
            r = self.registry.invoke("get_previous_investigations", {"subject_ids": others, "limit": 50}, ctx)
            calls.append(r)
            if r.ok:
                mem["connected"] = [_summ(i) for i in r.data.items if i.investigation.conclusion][:k]
        if signal_types:
            # semantic recall over indexed historical case notes, then re-rank by signal overlap
            r = self.registry.invoke("search_documents", {
                "query": " ".join(s.replace("_", " ").lower() for s in signal_types), "k": 10,
                "doc_types": ["investigation_report"]}, ctx)
            calls.append(r)
            if r.ok:
                sig = set(signal_types)
                scored = []
                for p in r.data.passages:
                    txt = p.text.upper()
                    hist = {s for s in [*sig, "VELOCITY_SPIKE", "NEW_DEVICE", "AMOUNT_DEVIATION", "GEO_NEW_COUNTRY",
                                        "DEVICE_SHARING", "HIGH_RISK_MERCHANT"] if s in txt}
                    j = jaccard(sig, hist)
                    if j > 0:
                        scored.append({"chunk_id": p.chunk_id, "case": (p.section or "").split(" ")[0],
                                       "signal_overlap": round(j, 3), "excerpt": p.text[:300]})
                mem["similar"] = sorted(scored, key=lambda x: -x["signal_overlap"])[:k]
        return mem, calls
