"""Agent behaviour: tool selection, missing/conflicting evidence, malformed tool
responses, hallucination resistance (with scripted LLM test doubles)."""
import json
import unittest

from app.agents.workflow import EXPECTED_TOOLS, InvestigationAgent
from app.data.store import utcnow
from app.llm.provider import LLMResponse
from app.schemas.domain import Investigation
from app.security.principal import Principal
from tests import support

ANALYST = Principal("U-test", "analyst")


class ScriptedLLM:
    """Test double: returns pre-written JSON drafts in order and records prompts."""

    name = "scripted"
    model = "scripted-test-model"

    def __init__(self, drafts):
        self.drafts = list(drafts)
        self.prompts = []

    def complete(self, system, messages, max_tokens=1500, temperature=0.0):
        self.prompts.append(messages[-1]["content"])
        text = self.drafts.pop(0) if self.drafts else "{}"
        if isinstance(text, Exception):
            raise text
        return LLMResponse(text=text, model=self.model, tokens_in=1000, tokens_out=200, latency_ms=5)


def draft(summary, interp=(), actions=()):
    def cl(items):
        return [{"text": t, "citations": c} for t, c in items]
    return json.dumps({"executive_summary": cl(summary), "interpretation": cl(interp),
                       "recommended_actions": cl(actions)})


class AgentWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = support.container()

    def run_agent(self, request, agent=None, **kw):
        return (agent or self.c.agent).run(request, ANALYST, **kw)

    def test_customer_investigation_tool_selection_and_report(self):
        cid = support.first_of("mule_account")
        res = self.run_agent(f"Investigate customer {cid} and identify unusual activity during the last 30 days.")
        self.assertEqual(res["status"], "pending_review")
        used = {c["tool"] for c in res["tool_calls"]}
        self.assertTrue(set(EXPECTED_TOOLS["customer_review"]) <= used, set(EXPECTED_TOOLS["customer_review"]) - used)
        rep = res["report"]
        for section in ("executive_summary", "risk_indicators", "transaction_analysis", "graph_relationships",
                        "document_evidence", "risk_assessment", "uncertainty", "recommended_actions", "human_decision"):
            self.assertIn(section, rep)
        self.assertEqual(rep["human_decision"]["status"], "pending")
        self.assertEqual(res["validation"]["unsupported_claims"], 0)
        refs = {e.ref for e in self.c.store.list_evidence(res["investigation_id"])}
        for sec in ("executive_summary", "interpretation", "recommended_actions"):
            for claim in rep[sec]:
                self.assertTrue(set(claim["citations"]) <= refs)
        self.assertEqual(self.c.store.get_investigation(res["investigation_id"]).status, "pending_review")

    def test_lookback_parsing(self):
        cid = support.first_of("normal")
        res = self.run_agent(f"Review {cid} over the past 2 weeks")
        self.assertEqual(res["report"]["window"]["lookback_days"], 14)

    def test_account_transaction_device_subjects(self):
        cid = support.first_of("device_sharing_ring")
        acc = self.c.store.accounts_for_customer(cid)[0].account_id
        r = self.run_agent(f"Check account {acc}")
        self.assertEqual(r["status"], "pending_review")
        self.assertIn("get_account", {c["tool"] for c in r["tool_calls"]})
        self.assertEqual(r["report"]["investigation_type"], "account_review")
        tx = self.c.store.transactions_for_accounts([acc], None, None)
        r = self.run_agent(f"Explain transaction {tx.transaction_id.iloc[-1]}")
        self.assertEqual(r["report"]["investigation_type"], "transaction_review")
        dev = next(lb for lb in support.labels() if lb["scenario"] == "device_sharing_ring")["related_entities"][0]
        r = self.run_agent(f"Who is behind device {dev}?")
        self.assertEqual(r["report"]["investigation_type"], "device_review")
        self.assertIn("DEVICE_SHARING", [x["signal"] for x in r["report"]["risk_indicators"]])

    def test_missing_subject_and_unknown_entities(self):
        r = self.run_agent("Is anything suspicious going on?")
        self.assertEqual(r["status"], "needs_input")
        self.assertIn("Insufficient evidence", r["report"]["executive_summary"][0]["text"])
        r = self.run_agent("Investigate customer CUST-99999999")
        self.assertEqual(r["status"], "not_found")
        self.assertEqual(self.c.store.get_investigation(r["investigation_id"]).status, "needs_input")
        r = self.run_agent("Look at merchant MER-5000")
        self.assertEqual(r["status"], "needs_input")

    def test_malformed_tool_response_is_contained(self):
        c = support.container(fresh=True)
        spec = c.registry.tools["find_suspicious_cluster"]
        original = spec.func
        spec.func = lambda ctx, inp: {"not": "a cluster"}
        try:
            r = c.agent.run(f"Investigate {support.first_of('circular_transfer')}", ANALYST)
        finally:
            spec.func = original
        self.assertEqual(r["status"], "pending_review")
        failed = [t for t in r["tool_calls"] if not t["ok"]]
        self.assertEqual([(t["tool"], t["error_type"]) for t in failed], [("find_suspicious_cluster", "malformed_output")])
        missing = " ".join(x["text"] for x in r["report"]["uncertainty"]["missing_data"])
        self.assertIn("find_suspicious_cluster failed", missing)

    def test_conflicting_evidence_from_history(self):
        c = support.container(fresh=True)
        cid = support.first_of("circular_transfer")
        first = c.risk_engine.assess_customer(cid)
        c.store.create_investigation(Investigation(
            investigation_id="INV-PRIOR1", subject_id=cid, subject_type="customer", status="closed",
            created_at=utcnow(), closed_at=utcnow(), conclusion="legitimate", signals=first.signal_types(),
            summary="Reviewed: savings group rotation."))
        r = c.agent.run(f"Investigate customer {cid}", ANALYST)
        conflicts = " ".join(x["text"] for x in r["report"]["uncertainty"]["conflicting_evidence"])
        self.assertIn("INV-PRIOR1", conflicts)
        self.assertIn("legitimate", conflicts)

    def test_hallucinated_llm_claims_are_removed(self):
        cid = support.first_of("mule_account")
        llm = ScriptedLLM([None])  # filled below once evidence refs are known
        agent = InvestigationAgent(self.c, self.c.registry, llm, engine="mini", max_report_attempts=1)
        # first pass with a deterministic run to learn the refs, then script the LLM
        probe = self.run_agent(f"Investigate customer {cid}")
        score = probe["report"]["risk_assessment"]["score"]
        risk_ref = probe["report"]["risk_assessment"]["evidence"][0]
        llm.drafts = [draft(
            summary=[(f"The risk score is {score} for {cid}.", [risk_ref]),
                     ("The customer received USD 9,999,999 from a sanctioned entity.", [risk_ref]),
                     ("The customer is a criminal running a mule ring.", [risk_ref]),
                     ("Funds were moved to ACC-999999.", ["E999"])],
            interp=[("Activity is consistent with money-mule indicators.", [risk_ref])],
            actions=[("Freeze the account immediately.", [risk_ref]),
                     ("Ask an authorised analyst to review the inbound senders.", [risk_ref])])]
        r = agent.run(f"Investigate customer {cid}", ANALYST)
        rep = r["report"]
        texts = [c["text"] for s in ("executive_summary", "interpretation", "recommended_actions") for c in rep[s]]
        self.assertIn(f"The risk score is {score} for {cid}.", texts)
        for bad in ("9,999,999", "criminal", "ACC-999999", "Freeze the account"):
            self.assertFalse(any(bad in t for t in texts), bad)
        v = rep["validation"]
        self.assertEqual(v["unsupported_claims"], 4)
        self.assertTrue(rep["generated_by"]["narrative"].startswith("llm:"))
        self.assertEqual(rep["llm_usage"]["tokens_in"], 1000)
        # the prompt only contains evidence, never ground-truth labels
        for leak in ("is_suspicious", "mule_account", "expected_signals", "scenario_labels"):
            self.assertNotIn(leak, llm.prompts[0])

    def test_retry_then_fallback_when_llm_keeps_failing(self):
        cid = support.first_of("transaction_burst")
        bad = draft(summary=[("Totally unsupported 123,456 claim.", ["E1"]), ("Another 777,777 claim.", ["E1"])])
        llm = ScriptedLLM([bad, bad])
        agent = InvestigationAgent(self.c, self.c.registry, llm, engine="mini", max_report_attempts=2)
        r = agent.run(f"Investigate customer {cid}", ANALYST)
        self.assertEqual(len(llm.prompts), 2)  # retried once with feedback
        self.assertIn("unsupported claims", llm.prompts[1])
        self.assertEqual(r["node_trace"].count("generate_report"), 2)
        self.assertTrue(r["report"]["executive_summary"])  # deterministic fallback filled the section
        self.assertIn("executive_summary", r["report"]["validation"]["fallback_sections"])

    def test_llm_errors_fall_back_to_deterministic(self):
        cid = support.first_of("geographic_anomaly")
        for failure in (RuntimeError("HTTP 500"), "not json at all"):
            agent = InvestigationAgent(self.c, self.c.registry, ScriptedLLM([failure]), engine="mini")
            r = agent.run(f"Investigate customer {cid}", ANALYST)
            self.assertEqual(r["status"], "pending_review")
            self.assertEqual(r["report"]["generated_by"]["narrative"], "deterministic")
            missing = " ".join(x["text"] for x in r["report"]["uncertainty"]["missing_data"])
            self.assertIn("LLM narrative unavailable", missing)

    def test_episode_stores_summary_not_chain_of_thought(self):
        r = self.run_agent(f"Investigate customer {support.first_of('dormant_reactivation')}")
        ep = self.c.store.get_episode(r["episode_id"])
        self.assertTrue(ep["reasoning_summary"].startswith("Subject customer"))
        self.assertTrue(ep["plan"] and ep["tool_calls"] and ep["retrieved_evidence"])
        self.assertEqual(ep["evaluation"]["tool_selection_recall"], 1.0)
        self.assertNotIn("chain_of_thought", json.dumps(ep, default=str).lower())


class LangGraphParityTest(unittest.TestCase):
    def test_langgraph_engine_matches_builtin(self):
        try:
            import langgraph  # noqa: F401
        except ImportError:
            self.skipTest("langgraph not installed")
        c = support.container()
        lg = InvestigationAgent(c, c.registry, engine="langgraph")
        mini = InvestigationAgent(c, c.registry, engine="mini")
        cid = support.first_of("circular_transfer")
        a, b = lg.run(f"Investigate {cid}", ANALYST), mini.run(f"Investigate {cid}", ANALYST)
        self.assertEqual(lg.engine_name, "langgraph")
        self.assertEqual(a["node_trace"], b["node_trace"])
        self.assertEqual(a["report"]["risk_assessment"]["score"], b["report"]["risk_assessment"]["score"])


if __name__ == "__main__":
    unittest.main()
