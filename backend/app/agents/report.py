"""Investigation report construction.

Fact sections (risk indicators, transactions, graph, documents, risk assessment,
uncertainty) are always rendered deterministically from evidence. Narrative
sections (executive summary, interpretation, recommended actions) are written by
the configured LLM when available — constrained to cite evidence refs and then
validated — or otherwise rendered by transparent rules from the same evidence.
Every claim is tagged with its kind and how it was produced.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.schemas.domain import EvidenceItem

DISCLAIMER = ("Decision support only. This report was produced by an automated system for review by an authorised "
              "investigator. It does not establish that any offence occurred and no action has been taken on any "
              "account. Consequential actions require human approval.")

SIGNAL_PLAYBOOK: dict[str, dict[str, str]] = {
    "TRANSACTION_BURST": {
        "query": "burst of transactions within one hour card testing failed authorisations velocity",
        "action": "Review the burst transactions and the card or credential status with Fraud Operations; any block "
                  "requires authorised approval."},
    "VELOCITY_SPIKE": {
        "query": "sustained increase in daily transaction counts velocity seasonal business explanation",
        "action": "Compare the peak-day activity with known business or seasonal explanations before escalation."},
    "AMOUNT_DEVIATION": {
        "query": "single large payment legitimate property school fees travel amount deviation merchant category",
        "action": "Check the merchant category, counterparty history, device and location of the large payment(s)."},
    "PEER_AMOUNT_DEVIATION": {
        "query": "deviation compared with peer group customer segment unusually large transactions",
        "action": "Compare the payment(s) with the customer's declared income or turnover."},
    "RAPID_PASS_THROUGH": {
        "query": "rapid pass-through of funds credits followed by debits money mule within 24 hours",
        "action": "Identify the ultimate beneficiaries of the outflows and whether the senders are connected."},
    "FAN_IN": {
        "query": "account receives transfers from many unrelated senders fan-in mule",
        "action": "Review the senders of inbound funds and their relationship to the subject."},
    "GEO_NEW_COUNTRY": {
        "query": "transactions in a new country travel notification unusual geography",
        "action": "Check for travel notifications and whether the foreign transactions used the customer's usual device."},
    "IMPOSSIBLE_TRAVEL": {
        "query": "card-present transactions distance elapsed time impossible travel card cloning",
        "action": "Confirm the merchants' terminal locations; any card block requires authorised Fraud Operations approval."},
    "NEW_DEVICE": {
        "query": "new device new location new beneficiaries account takeover indicators",
        "action": "Establish when the new device was first used and verify recent activity with the customer through a "
                  "verified channel."},
    "DEVICE_SHARING": {
        "query": "device used by three or more unrelated customers mule network shared device",
        "action": "Review every customer using the shared device and establish whether a legitimate relationship exists."},
    "SHARED_IDENTIFIER": {
        "query": "same phone number email address shared identifiers unrelated customers enhanced due diligence",
        "action": "Verify the relationship between customers sharing identifiers under enhanced due diligence."},
    "CIRCULAR_FLOW": {
        "query": "circular transfers funds return to originating account layering artificial turnover",
        "action": "Review the relationship between all accounts in the cycle and the economic purpose of the transfers."},
    "DORMANT_REACTIVATION": {
        "query": "dormant account inactive 90 days reactivation significant value account sold takeover",
        "action": "Check for account takeover and for use of the account by a third party."},
    "HIGH_RISK_MERCHANT": {
        "query": "escalating payments gambling crypto-asset exchange money-transfer merchants high-risk",
        "action": "Compare high-risk merchant spending with the customer's income and historical spending."},
    "BEHAVIOURAL_SHIFT": {
        "query": "behaviour inconsistent with expected profile risk rating review trigger",
        "action": "Review whether the customer's risk rating and expected activity profile remain accurate."},
    "NETWORK_EXPOSURE": {
        "query": "connections to customers with open alerts previously confirmed cases network analysis",
        "action": "Review the flagged counterparties' cases; a connection alone does not establish suspicion."},
    "HISTORICAL_ALERTS": {
        "query": "alerts triage closed with documented rationale",
        "action": "Read the rationale of earlier alerts before concluding."},
    "ML_ANOMALY": {
        "query": "automated risk scores decision-support machine-learning models",
        "action": "Treat the model output as a prioritisation aid and confirm it with the deterministic evidence."},
}
GENERAL_QUERY = "investigation report evidence citations uncertainty neutral language automated drafts"

TYPOLOGIES: list[tuple[str, set[str], int, str]] = [
    ("money_mule", {"RAPID_PASS_THROUGH", "FAN_IN"}, 1,
     "Observed indicators ({sig}) are consistent with the money-mule indicators described in policy; the account holder's "
     "knowledge or intent cannot be inferred from transaction data."),
    ("account_takeover", {"NEW_DEVICE", "GEO_NEW_COUNTRY", "TRANSACTION_BURST"}, 2,
     "Observed indicators ({sig}) together are consistent with account-takeover indicators; if confirmed, the account holder may be a victim."),
    ("card_fraud", {"IMPOSSIBLE_TRAVEL"}, 1,
     "The {sig} indicator is consistent with possible card cloning described in the fraud playbook."),
    ("card_testing", {"TRANSACTION_BURST"}, 1,
     "The {sig} indicator matches the burst pattern the procedures associate with card testing or velocity abuse."),
    ("network", {"DEVICE_SHARING", "SHARED_IDENTIFIER", "NETWORK_EXPOSURE"}, 1,
     "Network indicators ({sig}) show connections that the procedures treat as review triggers, not as proof."),
    ("layering", {"CIRCULAR_FLOW"}, 1,
     "The {sig} indicator is consistent with the circular-transfer red flag (possible layering or artificial turnover); "
     "legitimate explanations such as savings groups should be considered."),
    ("dormant_misuse", {"DORMANT_REACTIVATION"}, 1,
     "The {sig} indicator is consistent with the dormant-reactivation red flag, which may reflect takeover or third-party use."),
    ("high_risk_spend", {"HIGH_RISK_MERCHANT"}, 1,
     "The {sig} indicator shows spending at high-risk merchant categories above the customer's baseline."),
]


def claim(text: str, citations: list[str | None], kind: str, source: str = "deterministic") -> dict[str, Any]:
    return {"text": text, "citations": [c for c in citations if c], "kind": kind, "source": source}


def _names(sigs: list[str]) -> str:
    return ", ".join(s.replace("_", " ").lower() for s in sigs)


# ------------------------------------------------------------- fact sections
def fact_sections(state: dict[str, Any], items: list[EvidenceItem], refs: dict[str, Any]) -> dict[str, Any]:
    a = state["data"].get("assessment")
    by_ref = {e.ref: e for e in items}
    indicators = []
    if a is not None:
        pts = {c.signal_type: c for c in a.contributors}
        for s in a.signals:
            r = refs["signals"].get(s.signal_type)
            c = pts.get(s.signal_type)
            indicators.append({
                "signal": s.signal_type, "observation": s.description, "observed_value": s.observed_value,
                "baseline": s.baseline_value, "threshold": s.threshold, "unit": s.unit, "severity": s.severity,
                "confidence": s.confidence, "points": c.points if c else 0.0, "capped": c.capped if c else False,
                "evidence": [x for x in [r, refs.get("key_transactions")] if x]})
    txn_ref = refs.get("key_transactions")
    key_txns = by_ref[txn_ref].content["transactions"] if txn_ref else []
    graph = {}
    if refs.get("cluster"):
        graph["cluster"] = {**by_ref[refs["cluster"]].content, "evidence": [refs["cluster"]]}
    if refs.get("shared_devices"):
        graph["shared_devices"] = {**by_ref[refs["shared_devices"]].content, "evidence": [refs["shared_devices"]]}
    if refs.get("fund_flows"):
        graph["fund_flows"] = {**by_ref[refs["fund_flows"]].content, "evidence": [refs["fund_flows"]]}
    if refs.get("connected_accounts"):
        c = by_ref[refs["connected_accounts"]].content
        graph["connected_accounts"] = {"account_id": c["account_id"], "count": len(c["connected"]),
                                       "sample": c["connected"][:10], "evidence": [refs["connected_accounts"]]}
    docs = []
    for chunk_id, r in refs["documents"].items():
        cnt = by_ref[r].content
        docs.append({"ref": r, "document_id": cnt["document_id"], "title": cnt["title"], "section": cnt["section"],
                     "page": cnt["page"], "chunk_id": chunk_id, "source": cnt["source"],
                     "excerpt": cnt["text"][:600], "retrieved_for": cnt["retrieval"]["query"]})
    risk = {}
    if a is not None:
        risk = {"score": a.score, "band": a.band, "flagged": a.flagged, "threshold": a.investigation_threshold,
                "contributors": [c.model_dump() for c in a.contributors], "config_version": a.config_version,
                "config_fingerprint": a.config_fingerprint, "evidence": [refs.get("risk_score")],
                "method": "Sum of weight x strength for each fired signal, with group caps for correlated signals, "
                          "capped at 100. Weights are engineering defaults (see RISK_ENGINE.md)."}
    history = by_ref[refs["history"]].content if refs.get("history") else {}
    return {"risk_indicators": indicators,
            "transaction_analysis": {"key_transactions": key_txns[:25], "evidence": [x for x in [txn_ref, refs.get("statistics")] if x],
                                     "statistics_ref": refs.get("statistics"), "anomalies_ref": refs.get("anomalies")},
            "graph_relationships": graph, "document_evidence": docs, "risk_assessment": risk,
            "historical_context": history}


def uncertainty_section(state: dict[str, Any], refs: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    a = state["data"].get("assessment")
    missing, conflicting, weak, assumptions = [], [], [], []
    risk_ref = refs.get("risk_score")
    if a is not None:
        for n in a.not_evaluated:
            missing.append(claim(f"{n.signal_type} was not evaluated: {n.reason}.", [risk_ref], "fact"))
        for dq in a.data_quality:
            missing.append(claim(dq, [risk_ref], "fact"))
        for s in a.signals:
            if s.severity == "low" or s.confidence < 0.7:
                weak.append(claim(f"{s.signal_type} is weak (severity {s.severity}, confidence {s.confidence}).",
                                  [refs["signals"].get(s.signal_type)], "metric"))
        if len(a.signals) == 1:
            conflicting.append(claim(f"Only one indicator ({a.signals[0].signal_type}) fired; single indicators "
                                     "frequently have legitimate explanations.",
                                     [refs["signals"].get(a.signals[0].signal_type)], "interpretation"))
        assumptions.append(claim(f"Thresholds and weights are engineering defaults from configuration "
                                 f"{a.config_version}, not regulatory values.", [risk_ref], "assumption"))
        assumptions.append(claim("The analysis covers all accounts of the customer and the window "
                                 f"{a.window_start.date()} to {a.window_end.date()}.", [risk_ref], "assumption"))
    for tc in state.get("tool_calls") or []:
        if not tc["ok"]:
            missing.append(claim(f"Tool {tc['tool']} failed ({tc.get('error_type')}); its evidence is missing.", [],
                                 "fact", "system"))
    if not refs["documents"]:
        missing.append(claim("Insufficient evidence: no policy passages were retrieved.", [], "fact", "system"))
    mem = state["data"].get("memory") or {}
    fired = set(a.signal_types()) if a is not None else set()
    for item in mem.get("direct", []):
        if item.get("conclusion") == "legitimate" and fired & set(item.get("signals") or []):
            conflicting.append(claim(
                f"Previous investigation {item['investigation_id']} on this subject concluded legitimate with "
                f"overlapping indicators ({', '.join(sorted(fired & set(item['signals'])))}).",
                [refs.get("history")], "fact"))
    if state.get("llm_status") and state["llm_status"] != "ok":
        missing.append(claim(f"LLM narrative unavailable ({state['llm_status']}); narrative sections were rendered "
                             "deterministically from evidence.", [], "fact", "system"))
    return {"missing_data": missing, "conflicting_evidence": conflicting, "weak_signals": weak,
            "assumptions": assumptions}


# --------------------------------------------------------- narrative (rules)
def deterministic_narrative(state: dict[str, Any], refs: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    a = state["data"].get("assessment")
    subject = state["subject"]
    summary: list[dict[str, Any]] = []
    interp: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    if a is None:
        summary.append(claim("Insufficient evidence: the risk assessment could not be completed.", [], "fact"))
        return {"executive_summary": summary, "interpretation": interp, "recommended_actions": actions}
    rr = refs.get("risk_score")
    summary.append(claim(
        f"{subject['type'].capitalize()} {subject['id']} has a risk score of {a.score} ({a.band}) for "
        f"{a.window_start.date()} to {a.window_end.date()}; {len(a.signals)} indicator(s) crossed their thresholds.",
        [rr], "metric"))
    for c in a.contributors[:3]:
        s = next(x for x in a.signals if x.signal_type == c.signal_type)
        summary.append(claim(s.description, [refs["signals"][s.signal_type]],
                             "ml" if s.signal_type == "ML_ANOMALY" else "metric"))
    if not a.signals:
        summary.append(claim("No configured risk indicator crossed its threshold in the investigation window.",
                             [rr, refs.get("statistics")], "metric"))
    cl = state["data"].get("cluster")
    if cl is not None and refs.get("cluster") and (cl.flagged_customers or cl.shared_devices):
        summary.append(claim(
            f"The subject's network cluster contains {len(cl.customers)} customers, of which "
            f"{len(cl.flagged_customers)} have open alerts, and {len(cl.shared_devices)} shared device(s).",
            [refs["cluster"]], "fact"))
    mem = state["data"].get("memory") or {}
    if mem.get("direct"):
        last = mem["direct"][0]
        outcome = (f"concluded {last['conclusion']}" if last.get("conclusion")
                   else f"has status {last.get('status')}")
        summary.append(claim(f"{len(mem['direct'])} previous investigation(s) on this subject; the most recent "
                             f"({last['investigation_id']}) {outcome}.", [refs.get("history")], "fact"))
    fired = set(a.signal_types())
    for _name, sigs, need, template in TYPOLOGIES:
        hit = sorted(fired & sigs)
        if len(hit) >= need:
            cites = [refs["signals"][s] for s in hit] + [d for s in hit for d in refs["doc_by_signal"].get(s, [])[:1]]
            interp.append(claim(template.format(sig=_names(hit)), cites, "interpretation"))
    if not interp and fired:
        interp.append(claim(f"The indicators ({_names(sorted(fired))}) do not match a single documented typology; "
                            "they should be reviewed individually.", [refs["signals"][s] for s in sorted(fired)],
                            "interpretation"))
    if not fired:
        interp.append(claim("Insufficient evidence of unusual activity in the investigation window.", [], "interpretation"))
    for s in sorted(fired, key=lambda x: -next(c.points for c in a.contributors if c.signal_type == x))[:5]:
        pb = SIGNAL_PLAYBOOK.get(s)
        if pb:
            actions.append(claim(pb["action"], [refs["signals"][s]] + refs["doc_by_signal"].get(s, [])[:1], "recommendation"))
    if a.flagged:
        actions.append(claim("Assign to an analyst for review and decision (confirm, reject, escalate or request more "
                             "evidence).", [rr], "recommendation"))
    return {"executive_summary": summary, "interpretation": interp, "recommended_actions": actions}


# ------------------------------------------------------------ narrative (LLM)
SYSTEM_PROMPT = """You are drafting sections of a financial-crime investigation report for review by a human
investigator at a bank. You are a decision-support tool.

Rules (all mandatory):
- Use ONLY the evidence provided. Every claim must cite one or more evidence refs such as "E3" in its
  "citations" list. Do not cite refs that are not listed.
- Copy numbers, dates and identifiers exactly as they appear in the cited evidence. Do not compute new figures.
- Do not invent transactions, customers, laws, regulations, risk indicators or investigation history.
- If the evidence does not support a statement, write "Insufficient evidence: ..." instead.
- Use neutral language ("consistent with", "indicator of", "requires review"). Never state or imply that anyone
  committed a crime, and remember that account holders may be victims.
- Recommended actions are suggestions for the investigator. Never instruct the system to freeze, close or block
  accounts or to file reports; such actions require authorised human approval and must say so.
- Output a single JSON object and nothing else, with this shape:
  {"executive_summary": [{"text": str, "citations": [str]}],
   "interpretation": [{"text": str, "citations": [str]}],
   "recommended_actions": [{"text": str, "citations": [str]}]}
- Executive summary: 2-5 claims. Interpretation: 1-4 claims weighing the evidence, including legitimate
  explanations and weaknesses. Recommended actions: 1-5 claims."""


def evidence_pack(items: list[EvidenceItem], max_chars: int = 14000) -> str:
    rows: list[dict[str, Any]] = []
    for e in items:
        c = e.content
        if e.evidence_type == "policy_passage":
            body = {"title": c["title"], "section": c["section"], "text": c["text"][:700]}
        elif e.evidence_type.startswith("risk_signal:"):
            body = {k: c.get(k) for k in ("signal_type", "description", "observed_value", "baseline_value",
                                          "threshold", "unit", "severity", "confidence")}
        elif e.evidence_type == "transactions":
            body = {"transactions": c["transactions"][:12]}
        elif e.evidence_type == "risk_score":
            body = {k: c.get(k) for k in ("score", "band", "flagged", "investigation_threshold", "contributors",
                                          "not_evaluated", "data_quality", "window_start", "window_end")}
        else:
            body = c
        rows.append({"ref": e.ref, "source_type": e.source_type, "type": e.evidence_type, "title": e.title,
                     "confidence": e.confidence, "content": body})
    text = json.dumps(rows, default=str)
    if len(text) > max_chars:  # keep the highest-value items when over budget
        pri = {"risk_score": 0, "customer_profile": 1, "behaviour_statistics": 2}
        rows.sort(key=lambda r: pri.get(r["type"], 3 if r["type"].startswith("risk_signal") else 5))
        while len(json.dumps(rows, default=str)) > max_chars and len(rows) > 3:
            rows.pop()
        text = json.dumps(rows, default=str)
    return text


def llm_messages(state: dict[str, Any], items: list[EvidenceItem], feedback: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    subject = state["subject"]
    user = (f"Investigation request: {state['request']}\nSubject: {subject['type']} {subject['id']}\n"
            f"Evidence (JSON list):\n{evidence_pack(items)}")
    if feedback:
        user += ("\n\nYour previous draft contained unsupported claims that were removed:\n"
                 + json.dumps(feedback[:10]) + "\nRewrite the sections, fixing these problems.")
    return [{"role": "user", "content": user}]


def parse_llm_json(text: str) -> dict[str, list[dict[str, Any]]]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in model output")
    obj = json.loads(m.group(0))
    out: dict[str, list[dict[str, Any]]] = {}
    for key in ("executive_summary", "interpretation", "recommended_actions"):
        claims = obj.get(key) or []
        if not isinstance(claims, list):
            raise ValueError(f"{key} must be a list")
        out[key] = [{"text": str(c.get("text", "")), "citations": [str(x) for x in (c.get("citations") or [])],
                     "kind": "recommendation" if key == "recommended_actions" else "interpretation", "source": "llm"}
                    for c in claims if isinstance(c, dict)]
    return out


def to_markdown(report: dict[str, Any]) -> str:
    """Human-readable rendering of a report (used for export and the MCP tool)."""
    def cl(c: dict[str, Any]) -> str:
        cites = ", ".join(c.get("citations") or [])
        return f"- {c['text']}" + (f" [{cites}]" if cites else "") + f" _({c.get('kind')}, {c.get('source')})_"

    s = report.get("subject", {})
    lines = [f"# Investigation report {report.get('investigation_id')}", "",
             f"Subject: {s.get('type')} {s.get('id')} · Window: {report.get('window', {}).get('start')} → "
             f"{report.get('window', {}).get('end')}", "", f"> {report.get('disclaimer')}", "",
             "## Executive Summary", *[cl(c) for c in report.get("executive_summary", [])], "",
             "## Risk Indicators", "", "| Signal | Observation | Baseline | Threshold | Severity | Points | Evidence |",
             "|---|---|---|---|---|---|---|"]
    for r in report.get("risk_indicators", []):
        lines.append(f"| {r['signal']} | {r['observation']} | {r['baseline']} | {r['threshold']} | {r['severity']} | "
                     f"{r['points']} | {', '.join(r['evidence'])} |")
    ra = report.get("risk_assessment", {})
    lines += ["", "## Risk Assessment", f"Score {ra.get('score')} ({ra.get('band')}), threshold {ra.get('threshold')}.",
              *[f"- {c['signal_type']}: +{c['points']}" + (" (group-capped)" if c.get("capped") else "")
                for c in ra.get("contributors", [])],
              "", "## Interpretation", *[cl(c) for c in report.get("interpretation", [])],
              "", "## Document Evidence"]
    for d in report.get("document_evidence", []):
        lines.append(f"- [{d['ref']}] {d['title']} — {d['section']} (doc {d['document_id']}, chunk {d['chunk_id']})")
    lines += ["", "## Uncertainty"]
    for k, v in report.get("uncertainty", {}).items():
        lines.append(f"**{k.replace('_', ' ').title()}**")
        lines += [cl(c) for c in v] or ["- none recorded"]
    lines += ["", "## Recommended Investigation Actions (suggestions only)",
              *[cl(c) for c in report.get("recommended_actions", [])], "", "## Human Decision",
              "[ ] Confirm  [ ] Reject  [ ] Escalate  [ ] Request More Evidence"]
    return "\n".join(lines)
