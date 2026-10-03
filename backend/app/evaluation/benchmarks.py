"""Benchmarks over the synthetic ground truth.

Ground-truth labels are read only here; no agent tool can access them.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.agents.workflow import EXPECTED_TOOLS
from app.evaluation.metrics import (
    classification_metrics,
    percentile,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    roc_auc,
)
from app.security.principal import Principal

REPO = Path(__file__).resolve().parents[3]
QRELS_PATH = REPO / "evaluation" / "retrieval_qrels.json"
EVAL_PRINCIPAL = Principal(user_id="evaluation-harness", role="analyst", via="system")


def load_labels(settings: Any, store: Any) -> list[dict[str, Any]]:
    """Ground truth from the dataset directory (frames) or the scenario_labels table."""
    path = Path(settings.dataset_dir) / "scenario_labels.json"
    if settings.data_backend == "frames" or not hasattr(store, "engine"):
        return json.loads(path.read_text(encoding="utf-8"))
    from sqlalchemy import text

    with store.engine.connect() as conn:
        rows = conn.execute(text("SELECT entity_type, entity_id, scenario, is_suspicious, expected_signals, "
                                 "related_entities, notes FROM scenario_labels"))
        return [{**dict(r._mapping), "expected_signals": list(r.expected_signals or [])} for r in rows]


def dedupe_subjects(labels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for lb in labels:
        if lb["entity_type"] != "customer":
            continue
        prev = seen.get(lb["entity_id"])
        if prev is None or (lb["is_suspicious"] and not prev["is_suspicious"]):
            seen[lb["entity_id"]] = lb
    return list(seen.values())


def sample_labels(labels: list[dict[str, Any]], per_scenario: int | None, seed: int = 11) -> list[dict[str, Any]]:
    import random

    rnd = random.Random(seed)
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lb in dedupe_subjects(labels):
        by[lb["scenario"]].append(lb)
    out = []
    for scen, items in sorted(by.items()):
        items = sorted(items, key=lambda x: x["entity_id"])
        rnd.shuffle(items)
        out += items if per_scenario is None else items[: (per_scenario * 3 if scen == "normal" else per_scenario)]
    return out


# ----------------------------------------------------------------- risk
def risk_benchmark(engine: Any, labels: list[dict[str, Any]]) -> dict[str, Any]:
    thr = engine.config.score.investigation_threshold
    rows = []
    t0 = time.perf_counter()
    for lb in labels:
        a = engine.assess_customer(lb["entity_id"])
        rows.append({"entity_id": lb["entity_id"], "scenario": lb["scenario"], "is_suspicious": lb["is_suspicious"],
                     "score": a.score, "flagged": a.flagged, "signals": a.signal_types(),
                     "expected_signals": lb.get("expected_signals") or []})
    elapsed = time.perf_counter() - t0
    y = [r["is_suspicious"] for r in rows]
    overall = classification_metrics(y, [r["flagged"] for r in rows])
    overall["auc"] = roc_auc(y, [r["score"] for r in rows])
    per_scen = {}
    for scen in sorted({r["scenario"] for r in rows}):
        rs = [r for r in rows if r["scenario"] == scen]
        exp = [r for r in rs if r["expected_signals"]]
        cov = [len(set(r["expected_signals"]) & set(r["signals"])) / len(r["expected_signals"]) for r in exp]
        per_scen[scen] = {"n": len(rs), "suspicious": rs[0]["is_suspicious"],
                          "flag_rate": round(sum(r["flagged"] for r in rs) / len(rs), 4),
                          "mean_score": round(sum(r["score"] for r in rs) / len(rs), 2),
                          "expected_signal_recall": round(sum(cov) / len(cov), 4) if cov else None}
    # signal-level: precision of each signal type as an indicator of a suspicious subject
    sig_stats = {}
    for s in sorted({x for r in rows for x in r["signals"]}):
        fired = [r for r in rows if s in r["signals"]]
        sig_stats[s] = {"fired": len(fired), "precision": round(sum(r["is_suspicious"] for r in fired) / len(fired), 4)}
    return {"threshold": thr, "n": len(rows), "overall": overall, "per_scenario": per_scen, "signals": sig_stats,
            "config_version": engine.config.version, "config_fingerprint": engine.config.fingerprint(),
            "ms_per_assessment": round(elapsed * 1000 / max(len(rows), 1), 1),
            "rows": rows}


# ------------------------------------------------------------- retrieval
def _relevant(passage: Any, judgements: list[dict[str, str]]) -> bool:
    sec = (passage.section or "").lower()
    return any(passage.document_id == j["document_id"] and j["section_contains"].lower() in sec for j in judgements)


def retrieval_benchmark(retriever: Any, k_values: tuple[int, ...] = (1, 3, 5), qrels_path: Path = QRELS_PATH) -> dict[str, Any]:
    qrels = json.loads(Path(qrels_path).read_text(encoding="utf-8"))["queries"]
    corpus_ids = {d["document_id"] for d in retriever.repo.documents()}
    out: dict[str, Any] = {}
    kmax = max(k_values)
    for mode in ("keyword", "semantic", "hybrid"):
        if mode == "semantic" and not retriever.semantic_available:
            continue
        per_q = []
        for q in qrels:
            judg = [j for j in q["relevant"] if j["document_id"] in corpus_ids]
            if not judg:
                continue
            res = retriever.search(q["query"], k=kmax, mode=mode)
            rel = [_relevant(p, judg) for p in res]
            n_rel = len(judg)
            per_q.append({"id": q["id"], **{f"p@{k}": precision_at_k(rel, k) for k in k_values},
                          **{f"r@{k}": recall_at_k(rel, k, n_rel) for k in k_values}, "rr": reciprocal_rank(rel)})
        agg = {key: round(sum(p[key] for p in per_q) / len(per_q), 4) for key in per_q[0] if key != "id"} if per_q else {}
        agg["mrr"] = agg.pop("rr", 0.0)
        out[mode] = {"n_queries": len(per_q), **agg, "per_query": per_q}
    return out


# ----------------------------------------------------------------- agent
CONTENT_WORD = re.compile(r"[a-z]{5,}")


def agent_benchmark(container: Any, labels: list[dict[str, Any]], qrels_path: Path = QRELS_PATH) -> dict[str, Any]:
    qrels = {q["signal"]: q["relevant"] for q in json.loads(Path(qrels_path).read_text(encoding="utf-8"))["queries"] if q["signal"]}
    runs = []
    for lb in labels:
        t0 = time.perf_counter()
        res = container.agent.run(f"Investigate customer {lb['entity_id']} and identify unusual activity during the "
                                  "last 30 days.", EVAL_PRINCIPAL)
        latency = (time.perf_counter() - t0) * 1000
        rep = res.get("report") or {}
        calls = res.get("tool_calls") or []
        itype = "customer_review"
        expected = set(EXPECTED_TOOLS[itype])
        used = {c["tool"] for c in calls}
        completed = res["status"] == "pending_review" and all(
            k in rep for k in ("executive_summary", "risk_indicators", "risk_assessment", "uncertainty",
                               "recommended_actions", "human_decision", "document_evidence"))
        fired = {r["signal"] for r in rep.get("risk_indicators", [])}
        exp_sig = set(lb.get("expected_signals") or [])
        coverage = len(exp_sig & fired) / len(exp_sig) if exp_sig else None
        # context relevance: retrieved passages relevant (per qrels) to a signal they were retrieved for
        docs = rep.get("document_evidence", [])
        inv = container.store.list_evidence(res["investigation_id"]) if res.get("investigation_id") else []
        by_ref = {e.ref: e for e in inv}
        rel_flags = []
        for d in docs:
            e = by_ref.get(d["ref"])
            q = e.content["retrieval"]["query"] if e else ""
            sigs = [s for s in qrels if q and _query_signal(q, s)]
            judg = [j for s in sigs for j in qrels.get(s, [])]
            if judg:
                rel_flags.append(any(d["document_id"] == j["document_id"] and
                                     j["section_contains"].lower() in (d["section"] or "").lower() for j in judg))
        # citation correctness: cited evidence shares content words with the claim
        claims = [c for s in ("executive_summary", "interpretation", "recommended_actions") for c in rep.get(s, [])]
        cite_ok = []
        for c in claims:
            words = set(CONTENT_WORD.findall(c["text"].lower()))
            for ref in c.get("citations", []):
                e = by_ref.get(ref)
                if e is None:
                    cite_ok.append(False)
                    continue
                blob = (e.title + " " + json.dumps(e.content, default=str)).lower()
                cite_ok.append(any(w in blob for w in words) or e.evidence_type in ("policy_passage", "risk_score"))
        val = rep.get("validation") or {}
        runs.append({
            "entity_id": lb["entity_id"], "scenario": lb["scenario"], "status": res["status"], "completed": completed,
            "tool_recall": len(expected & used) / len(expected),
            "tool_precision": len(expected & used) / len(used) if used else 0.0,
            "evidence_coverage": coverage, "claims": val.get("total_claims", 0),
            "unsupported": val.get("unsupported_claims", 0), "grounded_claims": len(claims),
            "context_relevance": sum(rel_flags) / len(rel_flags) if rel_flags else None,
            "citation_correctness": sum(cite_ok) / len(cite_ok) if cite_ok else None,
            "latency_ms": latency, "tool_calls": len(calls), "tool_failures": sum(1 for c in calls if not c["ok"]),
            "timeouts": sum(1 for c in calls if c.get("error_type") == "timeout"),
            "tokens_in": (rep.get("llm_usage") or {}).get("tokens_in", 0),
            "tokens_out": (rep.get("llm_usage") or {}).get("tokens_out", 0),
            "cost_usd": (rep.get("llm_usage") or {}).get("cost_usd", 0.0),
            "narrative": (rep.get("generated_by") or {}).get("narrative")})

    def mean(key: str) -> float | None:
        v = [r[key] for r in runs if r[key] is not None]
        return round(sum(v) / len(v), 4) if v else None

    total_claims = sum(r["claims"] for r in runs)
    total_unsup = sum(r["unsupported"] for r in runs)
    n_calls = sum(r["tool_calls"] for r in runs)
    return {
        "n": len(runs), "engine": container.agent.engine_name,
        "agent": {"tool_selection_recall": mean("tool_recall"), "tool_selection_precision": mean("tool_precision"),
                  "task_completion": round(sum(r["completed"] for r in runs) / len(runs), 4) if runs else None,
                  "evidence_coverage": mean("evidence_coverage"),
                  "unsupported_claim_rate": round(total_unsup / total_claims, 4) if total_claims else 0.0},
        "rag": {"context_relevance": mean("context_relevance"),
                "groundedness": round(1 - total_unsup / total_claims, 4) if total_claims else None,
                "citation_correctness": mean("citation_correctness")},
        "system": {"latency_p50_ms": round(percentile([r["latency_ms"] for r in runs], 50), 1),
                   "latency_p95_ms": round(percentile([r["latency_ms"] for r in runs], 95), 1),
                   "tokens_in": sum(r["tokens_in"] for r in runs), "tokens_out": sum(r["tokens_out"] for r in runs),
                   "cost_usd": round(sum(r["cost_usd"] for r in runs), 4),
                   "tool_calls": n_calls, "tool_failure_rate": round(sum(r["tool_failures"] for r in runs) / n_calls, 4)
                   if n_calls else 0.0, "timeout_rate": round(sum(r["timeouts"] for r in runs) / n_calls, 4)
                   if n_calls else 0.0},
        "narrative_sources": dict(Counter(r["narrative"] for r in runs)),
        "runs": runs}


def _query_signal(query: str, signal: str) -> bool:
    from app.agents.report import SIGNAL_PLAYBOOK

    return SIGNAL_PLAYBOOK.get(signal, {}).get("query") == query
