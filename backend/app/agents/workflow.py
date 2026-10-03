"""The investigation agent: an explicit, stateful workflow.

    START -> understand_request -> classify_investigation -> plan_evidence
          -> retrieve_structured -> run_risk_analytics -> query_graph
          -> retrieve_documents -> check_history -> evidence_fusion
          -> risk_assessment -> generate_report -> validate_claims
          -> (generate_report again if the LLM draft failed validation and attempts remain)
          -> human_review -> END
    Early exits (no subject, subject not found, unsupported subject type, missing
    core data) route to `finalize_incomplete`, which records an
    "Insufficient evidence" outcome.

Tool selection is deterministic per investigation type (the plan); the LLM is
used only to draft narrative text from already-collected, cited evidence.
The agent never takes consequential actions: it ends by placing the
investigation in `pending_review` for an analyst.
"""
from __future__ import annotations

import re
import time
from datetime import timedelta
from typing import Any, TypedDict

from app.agents.evidence import fuse
from app.agents.report import (
    DISCLAIMER,
    GENERAL_QUERY,
    SIGNAL_PLAYBOOK,
    SYSTEM_PROMPT,
    deterministic_narrative,
    fact_sections,
    llm_messages,
    parse_llm_json,
    uncertainty_section,
)
from app.agents.runtime import END, WorkflowSpec, compile_workflow
from app.agents.validator import validate_claims
from app.core.observability import METRICS, bind
from app.data.store import new_id, utcnow
from app.llm.provider import LLMUnavailable, NullProvider
from app.memory.episodic import EpisodicMemory
from app.security.principal import Principal
from app.tools.definitions import POLICY_TYPES
from app.tools.registry import ToolContext, ToolRegistry

ID_RE = {
    "customer": re.compile(r"\bCUST-\d{1,10}\b", re.I),
    "account": re.compile(r"\bACC-\d{1,10}\b", re.I),
    "transaction": re.compile(r"\bTXN-\d{1,12}\b", re.I),
    "device": re.compile(r"\bDEV-\d{1,10}\b", re.I),
    "merchant": re.compile(r"\bMER-\d{1,10}\b", re.I),
}
LOOKBACK_RE = re.compile(r"\b(?:last|past|previous)\s+(\d{1,3})\s*(day|days|week|weeks|month|months)\b", re.I)
LOOKBACK2_RE = re.compile(r"\b(\d{1,3})[- ](day|week|month)s?\b", re.I)

EXPECTED_TOOLS: dict[str, list[str]] = {
    "customer_review": ["get_customer", "get_transactions", "get_transaction_statistics", "get_risk_signals",
                        "detect_anomalies", "find_suspicious_cluster", "find_shared_device", "trace_funds",
                        "find_connected_accounts", "search_documents", "get_previous_investigations",
                        "create_investigation", "add_evidence"],
}
EXPECTED_TOOLS["account_review"] = ["get_account"] + EXPECTED_TOOLS["customer_review"]
EXPECTED_TOOLS["transaction_review"] = ["get_transaction", "get_account"] + EXPECTED_TOOLS["customer_review"]
EXPECTED_TOOLS["device_review"] = ["get_related_entities"] + EXPECTED_TOOLS["customer_review"]


class AgentState(TypedDict, total=False):
    request: str
    principal: Principal
    subject_hint: dict[str, str] | None
    lookback_days: int
    subject: dict[str, str]
    customer_id: str
    account_ids: list[str] | None
    primary_account: str | None
    investigation_type: str
    investigation_id: str
    episode_id: str
    request_id: str | None
    status: str
    status_reason: str | None
    plan: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    observations: list[str]
    data: dict[str, Any]
    evidence: list[Any]
    refs: dict[str, Any]
    facts: dict[str, Any]
    uncertainty: dict[str, Any]
    narrative: dict[str, Any]
    narrative_source: str
    validation: dict[str, Any]
    attempt: int
    feedback: list[dict[str, Any]]
    llm_status: str
    llm_usage: dict[str, Any]
    report: dict[str, Any]
    node_trace: list[str]
    started_at: Any
    t0: float
    window_start: Any
    window_end: Any


class _Node:
    """Accumulates the updates of one node so nodes never mutate shared state in place."""

    def __init__(self, agent: InvestigationAgent, state: dict[str, Any], name: str):
        self.agent, self.state, self.name = agent, state, name
        self.calls = list(state.get("tool_calls") or [])
        self.obs = list(state.get("observations") or [])
        self.data = dict(state.get("data") or {})

    @property
    def ctx(self) -> ToolContext:
        return ToolContext(principal=self.state["principal"], services=self.agent.services,
                           request_id=self.state.get("request_id"), investigation_id=self.state.get("investigation_id"),
                           agent_run_id=self.state.get("episode_id"))

    def record(self, res: Any, args: dict[str, Any]) -> None:
        self.calls.append({"node": self.name, "tool": res.tool, "ok": res.ok, "error_type": res.error_type,
                           "error": res.error, "latency_ms": res.latency_ms,
                           "input": {k: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:120])
                                     for k, v in args.items()}})

    def call(self, tool: str, args: dict[str, Any]) -> Any:
        res = self.agent.registry.invoke(tool, args, self.ctx)
        self.record(res, args)
        return res

    def note(self, text: str) -> None:
        self.obs.append(f"{self.name}: {text}")

    def done(self, **extra: Any) -> dict[str, Any]:
        return {"tool_calls": self.calls, "observations": self.obs, "data": self.data, **extra}


class InvestigationAgent:
    def __init__(self, services: Any, registry: ToolRegistry, llm: Any = None, engine: str = "auto",
                 max_report_attempts: int = 2):
        self.services = services
        self.registry = registry
        self.llm = llm or NullProvider()
        self.memory = EpisodicMemory(registry)
        self.max_report_attempts = max_report_attempts
        self.spec = self._spec()
        self.compiled = compile_workflow(self.spec, AgentState, prefer=engine)

    # --------------------------------------------------------------- wiring
    def _spec(self) -> WorkflowSpec:
        s = WorkflowSpec(entry="understand_request")
        for name in ("understand_request", "classify_investigation", "plan_evidence", "retrieve_structured",
                     "run_risk_analytics", "query_graph", "retrieve_documents", "check_history", "evidence_fusion",
                     "risk_assessment", "generate_report", "validate_claims", "human_review", "finalize_incomplete"):
            s.node(name, getattr(self, name))
        ok_or_stop = lambda st: "stop" if st.get("status") in ("needs_input", "failed", "not_found") else "ok"  # noqa: E731
        s.branch("understand_request", ok_or_stop, {"ok": "classify_investigation", "stop": "finalize_incomplete"})
        s.branch("classify_investigation", ok_or_stop, {"ok": "plan_evidence", "stop": "finalize_incomplete"})
        s.edge("plan_evidence", "retrieve_structured")
        s.branch("retrieve_structured", ok_or_stop, {"ok": "run_risk_analytics", "stop": "finalize_incomplete"})
        s.branch("run_risk_analytics", ok_or_stop, {"ok": "query_graph", "stop": "finalize_incomplete"})
        s.edge("query_graph", "retrieve_documents")
        s.edge("retrieve_documents", "check_history")
        s.edge("check_history", "evidence_fusion")
        s.edge("evidence_fusion", "risk_assessment")
        s.edge("risk_assessment", "generate_report")
        s.edge("generate_report", "validate_claims")
        s.branch("validate_claims", self._after_validation, {"retry": "generate_report", "done": "human_review"})
        s.edge("human_review", END)
        s.edge("finalize_incomplete", END)
        return s

    @property
    def engine_name(self) -> str:
        return self.compiled.engine

    # ------------------------------------------------------------------ run
    def run(self, request: str, principal: Principal, subject: dict[str, str] | None = None,
            lookback_days: int | None = None, request_id: str | None = None,
            investigation_id: str | None = None) -> dict[str, Any]:
        episode_id = new_id("EP")
        state: dict[str, Any] = {
            "request": request, "principal": principal, "subject_hint": subject, "lookback_days": lookback_days or 0,
            "episode_id": episode_id, "request_id": request_id, "investigation_id": investigation_id or "",
            "status": "in_progress", "plan": [], "tool_calls": [], "observations": [], "data": {}, "attempt": 0,
            "feedback": [], "llm_usage": {"calls": 0, "tokens_in": 0, "tokens_out": 0, "model": None,
                                          "cost_usd": 0.0}, "started_at": utcnow(), "t0": time.perf_counter(),
            "node_trace": []}
        with bind(agent_run_id=episode_id, user_id=principal.user_id):
            out = self.compiled.invoke(state)
        METRICS.inc("fira_agent_runs_total", status=out.get("status", "unknown"))
        METRICS.observe("fira_agent_latency_ms", (time.perf_counter() - state["t0"]) * 1000)
        return {"investigation_id": out.get("investigation_id") or None, "episode_id": episode_id, "status": out.get("status"),
                "status_reason": out.get("status_reason"), "report": out.get("report"),
                "node_trace": out.get("node_trace"), "engine": self.engine_name,
                "tool_calls": out.get("tool_calls"), "validation": (out.get("validation") or {}).get("summary")}

    # ---------------------------------------------------------------- nodes
    def understand_request(self, st: dict[str, Any]) -> dict[str, Any]:
        n = _Node(self, st, "understand_request")
        text = st["request"] or ""
        subject = None
        hint = st.get("subject_hint")
        if hint and hint.get("type") in ID_RE and ID_RE[hint["type"]].fullmatch(hint.get("id", "")):
            subject = {"type": hint["type"], "id": hint["id"].upper()}
        else:
            for kind, rx in ID_RE.items():
                m = rx.search(text)
                if m:
                    subject = {"type": kind, "id": m.group(0).upper()}
                    break
        lookback = int(st.get("lookback_days") or 0)
        if not lookback:
            m = LOOKBACK_RE.search(text) or LOOKBACK2_RE.search(text)
            if m:
                k = int(m.group(1))
                unit = m.group(2).lower()
                lookback = k * (7 if unit.startswith("week") else 30 if unit.startswith("month") else 1)
        lookback = max(1, min(lookback or self.services.settings.default_lookback_days, 365))
        if subject is None:
            n.note("no subject identifier found")
            return n.done(status="needs_input", lookback_days=lookback,
                          status_reason="No customer, account, transaction, device or merchant identifier was found "
                                        "in the request. Insufficient evidence to start an investigation.")
        n.note(f"subject {subject['type']} {subject['id']}, lookback {lookback} days")
        return n.done(subject=subject, lookback_days=lookback)

    def classify_investigation(self, st: dict[str, Any]) -> dict[str, Any]:
        n = _Node(self, st, "classify_investigation")
        subj = st["subject"]
        customer_id, account_ids, primary = None, None, None
        kind = subj["type"]
        if kind == "merchant":
            return n.done(status="needs_input", investigation_type="merchant_review",
                          status_reason="Merchant-level investigations are not supported in this version. "
                                        "Investigate the merchant's customers instead.")
        if kind == "customer":
            customer_id, itype = subj["id"], "customer_review"
        elif kind == "account":
            r = n.call("get_account", {"account_id": subj["id"]})
            if not r.ok:
                return n.done(status="not_found", investigation_type="account_review",
                              status_reason=f"Insufficient evidence: {r.error}.")
            customer_id, account_ids, primary, itype = r.data.account.customer_id, [subj["id"]], subj["id"], "account_review"
        elif kind == "transaction":
            r = n.call("get_transaction", {"transaction_id": subj["id"]})
            if not r.ok:
                return n.done(status="not_found", investigation_type="transaction_review",
                              status_reason=f"Insufficient evidence: {r.error}.")
            t = r.data
            n.data["focus_transaction"] = t
            acc = t.sender_account_id or t.receiver_account_id
            r2 = n.call("get_account", {"account_id": acc})
            if not r2.ok:
                return n.done(status="not_found", investigation_type="transaction_review",
                              status_reason=f"Insufficient evidence: {r2.error}.")
            customer_id, primary, itype = r2.data.account.customer_id, acc, "transaction_review"
        else:  # device
            r = n.call("get_related_entities", {"kind": "device", "entity_id": subj["id"], "depth": 1, "limit": 100})
            users = []
            if r.ok:
                for e in r.data.edges:
                    if e.rel == "USES_DEVICE" and e.source.startswith("customer:"):
                        users.append((e.props.get("n", 0), e.source.split(":", 1)[1]))
            if not users:
                return n.done(status="not_found", investigation_type="device_review",
                              status_reason=f"Insufficient evidence: no customers found for device {subj['id']}.")
            users.sort(reverse=True)
            n.data["device_users"] = [u for _, u in users]
            customer_id, itype = users[0][1], "device_review"
        inv_id = st.get("investigation_id")
        if not inv_id:
            r = n.call("create_investigation", {"subject_id": subj["id"], "subject_type": subj["type"],
                                                "request_text": st["request"][:2000]})
            if not r.ok:
                return n.done(status="failed", status_reason=f"could not create investigation: {r.error}")
            inv_id = r.data.investigation_id
        self.services.store.update_investigation(inv_id, status="in_progress")
        n.note(f"{itype}; analysis customer {customer_id}")
        return n.done(investigation_type=itype, customer_id=customer_id, account_ids=account_ids,
                      primary_account=primary, investigation_id=inv_id)

    def plan_evidence(self, st: dict[str, Any]) -> dict[str, Any]:
        n = _Node(self, st, "plan_evidence")
        itype = st["investigation_type"]
        plan = [{"step": i + 1, "tool": t, "purpose": PURPOSES.get(t, "")} for i, t in
                enumerate(t for t in EXPECTED_TOOLS[itype] if t not in ("create_investigation", "add_evidence",
                                                                         "get_account", "get_transaction",
                                                                         "get_related_entities"))]
        n.note(f"{len(plan)} evidence-collection steps planned for {itype}")
        return n.done(plan=plan)

    def retrieve_structured(self, st: dict[str, Any]) -> dict[str, Any]:
        n = _Node(self, st, "retrieve_structured")
        cid, lb = st["customer_id"], st["lookback_days"]
        r = n.call("get_customer", {"customer_id": cid})
        if not r.ok:
            return n.done(status="not_found", status_reason=f"Insufficient evidence: {r.error}.")
        n.data["customer_profile"] = r.data
        primary = st.get("primary_account") or (r.data.accounts[0].account_id if r.data.accounts else None)
        args = {"customer_id": cid, "lookback_days": lb, "limit": 500}
        if st.get("account_ids"):
            args = {"account_ids": st["account_ids"], "lookback_days": lb, "limit": 500}
        rt = n.call("get_transactions", args)
        if rt.ok:
            n.data["transactions"] = rt.data
        rs = n.call("get_transaction_statistics", {"customer_id": cid, "lookback_days": lb,
                                                   "baseline_days": self.services.settings.baseline_days})
        if rs.ok:
            n.data["statistics"] = rs.data
        n.note(f"{len(r.data.accounts)} account(s); {rt.data.total if rt.ok else 'n/a'} transactions in window")
        return n.done(primary_account=primary)

    def run_risk_analytics(self, st: dict[str, Any]) -> dict[str, Any]:
        n = _Node(self, st, "run_risk_analytics")
        lb, bd = st["lookback_days"], self.services.settings.baseline_days
        if st["investigation_type"] == "account_review":
            args = {"entity_type": "account", "entity_id": st["account_ids"][0], "lookback_days": lb, "baseline_days": bd}
        else:
            args = {"entity_type": "customer", "entity_id": st["customer_id"], "lookback_days": lb, "baseline_days": bd}
        r = n.call("get_risk_signals", args)
        if not r.ok:
            return n.done(status="failed", status_reason=f"risk analytics failed: {r.error}")
        a = r.data
        n.data["assessment"] = a
        ra = n.call("detect_anomalies", {"customer_id": st["customer_id"], "lookback_days": lb, "baseline_days": bd})
        if ra.ok:
            n.data["anomalies"] = ra.data
        # fetch signal-referenced transactions that are not already in the window sample
        have = {t.transaction_id for t in (n.data["transactions"].transactions if n.data.get("transactions") else [])}
        extra = {}
        wanted = [e.id for s in a.signals for e in s.evidence if e.kind == "transaction" and e.id not in have]
        for tid in list(dict.fromkeys(wanted))[:15]:
            rt = n.call("get_transaction", {"transaction_id": tid})
            if rt.ok:
                extra[tid] = rt.data
        n.data["extra_transactions"] = extra
        n.note(f"score {a.score} ({a.band}); signals: {', '.join(a.signal_types()) or 'none'}")
        return n.done(window_start=a.window_start, window_end=a.window_end)

    def query_graph(self, st: dict[str, Any]) -> dict[str, Any]:
        n = _Node(self, st, "query_graph")
        cid = st["customer_id"]
        a = n.data["assessment"]
        r = n.call("find_suspicious_cluster", {"customer_id": cid, "max_hops": 2})
        if r.ok:
            n.data["cluster"] = r.data
        r = n.call("find_shared_device", {"customer_id": cid})
        if r.ok:
            n.data["shared_devices"] = r.data
        if st.get("primary_account"):
            r = n.call("trace_funds", {"account_id": st["primary_account"], "direction": "out", "max_hops": 3,
                                       "since": a.window_start.isoformat(), "limit": 10})
            if r.ok:
                n.data["fund_flows"] = r.data
            r = n.call("find_connected_accounts", {"account_id": st["primary_account"], "max_hops": 2, "limit": 50})
            if r.ok:
                n.data["connected_accounts"] = r.data
        cl = n.data.get("cluster")
        n.note(f"cluster of {len(cl.customers) if cl else 0} customers; "
               f"{len(n.data['shared_devices'].shared_devices) if n.data.get('shared_devices') else 0} shared device(s)")
        return n.done()

    def retrieve_documents(self, st: dict[str, Any]) -> dict[str, Any]:
        n = _Node(self, st, "retrieve_documents")
        a = n.data["assessment"]
        order = [c.signal_type for c in a.contributors][:6]
        passages: dict[str, dict[str, Any]] = {}
        queries = [(SIGNAL_PLAYBOOK[s]["query"], [s]) for s in order if s in SIGNAL_PLAYBOOK]
        queries.append((GENERAL_QUERY, []))
        for q, sigs in queries:
            r = n.call("search_documents", {"query": q, "k": 2, "doc_types": POLICY_TYPES})
            if not r.ok:
                continue
            for p in r.data.passages:
                if p.chunk_id in passages:
                    passages[p.chunk_id]["signals"] += sigs
                else:
                    passages[p.chunk_id] = {"passage": p, "query": q, "signals": list(sigs)}
        n.data["passages"] = list(passages.values())[:12]
        n.note(f"{len(n.data['passages'])} policy passage(s) retrieved for {len(queries)} queries")
        return n.done()

    def check_history(self, st: dict[str, Any]) -> dict[str, Any]:
        n = _Node(self, st, "check_history")
        a = n.data["assessment"]
        cl = n.data.get("cluster")
        connected = (cl.customers if cl else []) + (n.data.get("device_users") or [])
        mem, results = self.memory.recall(n.ctx, st["customer_id"], connected, a.signal_types())
        for res in results:
            n.record(res, {"memory": "recall"})
        n.data["memory"] = mem
        n.note(f"{len(mem['direct'])} previous investigation(s) on subject, {len(mem['connected'])} on connected "
               f"customers, {len(mem['similar'])} similar case note(s)")
        return n.done()

    def evidence_fusion(self, st: dict[str, Any]) -> dict[str, Any]:
        n = _Node(self, st, "evidence_fusion")
        items, refs = fuse(st["investigation_id"], n.data)
        r = n.call("add_evidence", {"investigation_id": st["investigation_id"],
                                    "items": [i.model_dump(mode="json") for i in items]})
        n.note(f"{len(items)} evidence items {'stored' if r.ok else 'NOT stored: ' + str(r.error)}")
        return n.done(evidence=items, refs=refs)

    def risk_assessment(self, st: dict[str, Any]) -> dict[str, Any]:
        n = _Node(self, st, "risk_assessment")
        facts = fact_sections(st, st["evidence"], st["refs"])
        n.note(f"score {facts['risk_assessment'].get('score')} from {len(facts['risk_indicators'])} indicator(s)")
        return n.done(facts=facts)

    def generate_report(self, st: dict[str, Any]) -> dict[str, Any]:
        n = _Node(self, st, "generate_report")
        attempt = int(st.get("attempt") or 0) + 1
        usage = dict(st.get("llm_usage") or {})
        if isinstance(self.llm, NullProvider):
            n.note("LLM disabled; narrative rendered by rules from evidence")
            return n.done(attempt=attempt, narrative=deterministic_narrative(st, st["refs"]),
                          narrative_source="deterministic", llm_status="disabled")
        try:
            resp = self.llm.complete(SYSTEM_PROMPT, llm_messages(st, st["evidence"], st.get("feedback")),
                                     max_tokens=self.services.settings.llm_max_tokens)
            usage["calls"] = usage.get("calls", 0) + 1
            usage["tokens_in"] = usage.get("tokens_in", 0) + resp.tokens_in
            usage["tokens_out"] = usage.get("tokens_out", 0) + resp.tokens_out
            usage["model"] = resp.model
            s = self.services.settings
            usage["cost_usd"] = round(usage.get("cost_usd", 0.0) + resp.tokens_in / 1e6 * s.llm_cost_input_per_mtok
                                      + resp.tokens_out / 1e6 * s.llm_cost_output_per_mtok, 6)
            METRICS.inc("fira_llm_tokens_total", resp.tokens_in, direction="in", model=resp.model)
            METRICS.inc("fira_llm_tokens_total", resp.tokens_out, direction="out", model=resp.model)
            narrative = parse_llm_json(resp.text)
            n.note(f"LLM draft attempt {attempt} ({resp.tokens_in}+{resp.tokens_out} tokens)")
            return n.done(attempt=attempt, narrative=narrative, narrative_source=f"llm:{resp.model}",
                          llm_status="ok", llm_usage=usage)
        except LLMUnavailable as e:
            status = f"unavailable: {e}"
        except Exception as e:  # malformed JSON, HTTP errors, timeouts
            status = f"error: {type(e).__name__}"
            METRICS.inc("fira_llm_errors_total", error=type(e).__name__)
        n.note(f"LLM draft failed ({status}); deterministic narrative used")
        return n.done(attempt=attempt, narrative=deterministic_narrative(st, st["refs"]),
                      narrative_source="deterministic", llm_status=status, llm_usage=usage)

    def validate_claims(self, st: dict[str, Any]) -> dict[str, Any]:
        n = _Node(self, st, "validate_claims")
        v = validate_claims(st["narrative"], st["evidence"])
        sections = v["sections"]
        fallback_used = []
        if st.get("narrative_source", "").startswith("llm"):
            det = None
            for name in ("executive_summary", "recommended_actions"):
                if not sections.get(name):
                    det = det or validate_claims(deterministic_narrative(st, st["refs"]), st["evidence"])["sections"]
                    sections[name] = det.get(name, [])
                    fallback_used.append(name)
        summary = {"total_claims": v["total_claims"], "unsupported_claims": v["unsupported_claims"],
                   "unsupported_rate": v["unsupported_rate"], "rejected": v["rejected"],
                   "narrative_source": st.get("narrative_source"), "attempt": st.get("attempt"),
                   "fallback_sections": fallback_used}
        n.note(f"{v['total_claims']} narrative claims, {v['unsupported_claims']} removed as unsupported")
        METRICS.inc("fira_claims_total", v["total_claims"])
        METRICS.inc("fira_claims_unsupported_total", v["unsupported_claims"])
        prior = list((st.get("validation") or {}).get("history") or [])
        return n.done(validation={"sections": sections, "summary": summary, "history": prior + [summary]},
                      feedback=v["rejected"])

    def _after_validation(self, st: dict[str, Any]) -> str:
        s = st["validation"]["summary"]
        if (str(st.get("narrative_source", "")).startswith("llm") and s["unsupported_rate"] > 0.34
                and int(st.get("attempt") or 0) < self.max_report_attempts):
            return "retry"
        return "done"

    def human_review(self, st: dict[str, Any]) -> dict[str, Any]:
        n = _Node(self, st, "human_review")
        a = n.data["assessment"]
        sections = st["validation"]["sections"]
        facts = st["facts"]
        report = {
            "investigation_id": st["investigation_id"], "subject": st["subject"],
            "analysed_customer": st["customer_id"], "investigation_type": st["investigation_type"],
            "request": st["request"], "generated_at": utcnow().isoformat(),
            "window": {"start": a.window_start.isoformat(), "end": a.window_end.isoformat(),
                       "lookback_days": st["lookback_days"]},
            "generated_by": {"facts": "deterministic", "narrative": st.get("narrative_source"),
                             "engine": self.engine_name},
            "disclaimer": DISCLAIMER,
            "executive_summary": sections.get("executive_summary", []),
            **{k: facts[k] for k in ("risk_indicators", "transaction_analysis", "graph_relationships",
                                     "document_evidence", "risk_assessment", "historical_context")},
            "interpretation": sections.get("interpretation", []),
            "uncertainty": uncertainty_section(st, st["refs"]),
            "recommended_actions": sections.get("recommended_actions", []),
            "human_decision": {"status": "pending",
                               "options": ["confirm", "reject", "escalate", "request_more_evidence"]},
            "validation": st["validation"]["summary"],
            "evidence_index": [{"ref": e.ref, "title": e.title, "source_type": e.source_type,
                                "evidence_type": e.evidence_type, "confidence": e.confidence} for e in st["evidence"]],
            "llm_usage": st.get("llm_usage"),
        }
        summary_txt = " ".join(c["text"] for c in report["executive_summary"][:2])[:1000]
        self.services.store.update_investigation(
            st["investigation_id"], status="pending_review", risk_score=a.score, signals=a.signal_types(),
            summary=summary_txt, report=report)
        n.note("report stored; investigation awaiting analyst decision")
        upd = n.done(report=report, status="pending_review")
        self._save_episode({**st, **upd})
        return upd

    def finalize_incomplete(self, st: dict[str, Any]) -> dict[str, Any]:
        n = _Node(self, st, "finalize_incomplete")
        status = st.get("status") or "failed"
        report = {"investigation_id": st.get("investigation_id") or None, "subject": st.get("subject"),
                  "request": st["request"], "generated_at": utcnow().isoformat(), "status": status,
                  "executive_summary": [{"text": st.get("status_reason") or "Insufficient evidence.", "citations": [],
                                         "kind": "fact", "source": "system"}],
                  "disclaimer": DISCLAIMER}
        if st.get("investigation_id"):
            self.services.store.update_investigation(st["investigation_id"], status=(
                "needs_input" if status in ("needs_input", "not_found") else "failed"), report=report,
                summary=st.get("status_reason"))
        n.note(f"stopped: {status}")
        upd = n.done(report=report, status=status)
        self._save_episode({**st, **upd})
        return upd

    # ------------------------------------------------------------- episode
    def _save_episode(self, st: dict[str, Any]) -> None:
        a = (st.get("data") or {}).get("assessment")
        val = (st.get("validation") or {}).get("summary") or {}
        calls = st.get("tool_calls") or []
        evidence = st.get("evidence") or []
        expected = set(EXPECTED_TOOLS.get(st.get("investigation_type") or "", []))
        used = {c["tool"] for c in calls}
        reasoning = (
            f"Subject {st.get('subject', {}).get('type')} {st.get('subject', {}).get('id')} resolved to customer "
            f"{st.get('customer_id')} ({st.get('investigation_type')}). Executed {len(calls)} tool call(s), "
            f"{sum(1 for c in calls if not c['ok'])} failed. "
            + (f"Risk score {a.score} ({a.band}) from {len(a.signals)} indicator(s). " if a else "")
            + f"{len(evidence)} evidence item(s); narrative by {st.get('narrative_source', 'n/a')}; "
            f"{val.get('unsupported_claims', 0)} unsupported claim(s) removed. Final status {st.get('status')}.")
        latency = int((time.perf_counter() - st["t0"]) * 1000) if st.get("t0") else None
        usage = st.get("llm_usage") or {}
        self.services.store.save_episode({
            "episode_id": st["episode_id"], "investigation_id": st.get("investigation_id") or None,
            "request": st["request"], "requested_by": st["principal"].user_id, "status": st.get("status"),
            "plan": st.get("plan") or [], "tool_calls": calls, "observations": st.get("observations") or [],
            "retrieved_evidence": [{"ref": e.ref, "title": e.title, "source_type": e.source_type,
                                    "source_id": e.source_id} for e in evidence],
            "reasoning_summary": reasoning,
            "final_output": {"status": st.get("status"), "risk_score": a.score if a else None,
                             "signals": a.signal_types() if a else [], "status_reason": st.get("status_reason"),
                             "node_trace": (st.get("node_trace") or [])},
            "evaluation": {"validation": val, "tools_expected": sorted(expected), "tools_used": sorted(used),
                           "tool_selection_recall": round(len(expected & used) / len(expected), 3) if expected else None,
                           "tool_failures": sum(1 for c in calls if not c["ok"]),
                           "timeouts": sum(1 for c in calls if c.get("error_type") == "timeout")},
            "human_feedback": None, "model": usage.get("model"), "tokens_in": usage.get("tokens_in", 0),
            "tokens_out": usage.get("tokens_out", 0), "latency_ms": latency, "started_at": st.get("started_at"),
            "finished_at": utcnow()})


PURPOSES = {
    "get_customer": "customer profile, accounts and identifiers",
    "get_transactions": "transactions in the investigation window",
    "get_transaction_statistics": "behavioural statistics vs baseline",
    "get_risk_signals": "deterministic risk signals and score",
    "detect_anomalies": "transaction-level outliers",
    "find_suspicious_cluster": "network cluster, density and central entities",
    "find_shared_device": "devices shared with other customers",
    "trace_funds": "where funds went after leaving the primary account",
    "find_connected_accounts": "accounts connected by ownership, devices or transfers",
    "search_documents": "policy and procedure passages for the fired indicators",
    "get_previous_investigations": "previous investigations and human conclusions",
}


def window_of(st: dict[str, Any]) -> tuple[Any, Any]:
    end = st.get("window_end")
    return (end - timedelta(days=st["lookback_days"]), end) if end else (None, None)
