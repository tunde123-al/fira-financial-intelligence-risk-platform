"""End-to-end: synthetic suspicious customer -> investigation -> evidence -> graph
-> report -> citation verification -> analyst feedback -> evaluation -> audit.

Runs against the in-process stack (FrameStore + NetworkX + memory vectors), so it
needs no infrastructure. The same flow over HTTP and PostgreSQL/Neo4j/Qdrant is
covered by tests/integration/test_api_stack.py when those services are configured.
"""
import unittest

from app.data.store import new_id, utcnow
from app.evaluation.benchmarks import agent_benchmark, risk_benchmark, sample_labels
from app.evaluation.improvement import ApprovalError, approve, classify_failures, submit_proposal
from app.risk.config import RiskConfig
from app.schemas.domain import HumanDecision
from app.security.principal import Principal
from tests import support

ANALYST = Principal("U-analyst", "analyst")
ADMIN = Principal("U-admin", "admin")


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = support.container(fresh=True)

    def test_full_investigation_lifecycle(self):
        c = self.c
        cid = support.first_of("circular_transfer")
        # 3-5: search and open the profile, accounts and transactions
        self.assertIn(cid, [h.entity_id for h in c.store.search(cid)])
        self.assertTrue(c.store.accounts_for_customer(cid))
        # 6-12: run the investigation
        res = c.agent.run(f"Investigate customer {cid} and identify unusual activity during the last 30 days.", ANALYST)
        self.assertEqual(res["status"], "pending_review")
        rep = res["report"]
        tools = {t["tool"] for t in res["tool_calls"]}
        self.assertTrue({"get_risk_signals", "find_suspicious_cluster", "search_documents"} <= tools)
        self.assertIn("CIRCULAR_FLOW", [r["signal"] for r in rep["risk_indicators"]])
        self.assertTrue(rep["graph_relationships"])
        self.assertTrue(rep["document_evidence"])
        # 13: every important claim resolves to stored evidence
        evidence = {e.ref: e for e in c.store.list_evidence(res["investigation_id"])}
        claims = [x for s in ("executive_summary", "interpretation", "recommended_actions") for x in rep[s]]
        self.assertTrue(claims)
        for cl in claims:
            self.assertTrue(cl["citations"], cl)
            for ref in cl["citations"]:
                self.assertIn(ref, evidence)
        for ind in rep["risk_indicators"]:
            self.assertTrue(set(ind["evidence"]) <= set(evidence))
        for d in rep["document_evidence"]:
            e = evidence[d["ref"]]
            self.assertEqual(e.source_type, "document")
            self.assertEqual(e.content["chunk_id"], d["chunk_id"])
        # 14: uncertainty is exposed
        self.assertTrue(rep["uncertainty"]["assumptions"])
        # 15-17: analyst reviews and gives feedback, which is stored
        d = HumanDecision(decision_id=new_id("DEC"), investigation_id=res["investigation_id"], decided_by=ANALYST.user_id,
                          decision="confirm", rationale="Cycle confirmed after reviewing all four accounts.",
                          created_at=utcnow())
        c.store.add_decision(d)
        c.store.update_investigation(res["investigation_id"], status="closed", conclusion="confirmed_suspicious")
        self.assertEqual(c.store.list_decisions(res["investigation_id"])[0].decision, "confirm")
        # 18: evaluation metrics can be calculated
        labels = sample_labels(support.labels(), 2)
        rb = risk_benchmark(c.risk_engine, labels)
        self.assertGreater(rb["overall"]["recall"], 0.5)
        ab = agent_benchmark(c, sample_labels(support.labels(), 1)[:4])
        self.assertEqual(ab["agent"]["task_completion"], 1.0)
        self.assertEqual(ab["agent"]["unsupported_claim_rate"], 0.0)
        # 19: audit shows what happened
        audit = c.store.list_audit(limit=2000)
        self.assertTrue(any(a.tool == "get_risk_signals" and a.user_id == ANALYST.user_id for a in audit))
        self.assertTrue(all(a.request_id is None or isinstance(a.request_id, str) for a in audit))

    def test_controlled_improvement_requires_validation_and_four_eyes(self):
        c = self.c
        labels = sample_labels(support.labels(), 3)
        # analyst feedback rejecting a flagged case -> false positive classification
        res = c.agent.run(f"Investigate customer {support.first_of('travel_legit')}", ANALYST)
        c.store.add_decision(HumanDecision(decision_id=new_id("DEC"), investigation_id=res["investigation_id"],
                                           decided_by=ANALYST.user_id, decision="reject",
                                           rationale="Customer notified travel in advance.", created_at=utcnow(),
                                           failure_categories=["false_positive"]))
        fails = classify_failures(c.store, c.risk_config.score.investigation_threshold)
        self.assertGreaterEqual(fails["category_counts"].get("false_positive", 0), 1)
        # a harmless candidate (lower weight of a weak, low-precision signal) passes regression
        cand = RiskConfig(**c.risk_config.model_dump())
        cand.signals["HISTORICAL_ALERTS"].weight = 2
        cand.version = "candidate-test"
        rec = submit_proposal(c.store, c.graph, c.risk_config, labels, ANALYST, cand, "test")
        self.assertEqual(rec["status"], "validated", rec["evaluation"]["regression"])
        with self.assertRaises(ApprovalError):
            approve(c.store, rec["version_id"], Principal(ANALYST.user_id, "admin"))  # proposer cannot approve
        out = approve(c.store, rec["version_id"], ADMIN)
        self.assertEqual(out["status"], "active")
        c.reload_risk_config()
        self.assertEqual(c.risk_config.signals["HISTORICAL_ALERTS"].weight, 2)
        # a harmful candidate (disable the strongest signal) is rejected by regression
        bad = RiskConfig(**c.risk_config.model_dump())
        bad.signals["CIRCULAR_FLOW"].enabled = False
        bad.signals["DEVICE_SHARING"].enabled = False
        rec2 = submit_proposal(c.store, c.graph, c.risk_config, labels, ANALYST, bad, "bad")
        self.assertEqual(rec2["status"], "rejected")
        with self.assertRaises(ApprovalError):
            approve(c.store, rec2["version_id"], ADMIN)


if __name__ == "__main__":
    unittest.main()
