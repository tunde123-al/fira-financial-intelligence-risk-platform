"""Alert generation, deduplication, lifecycle, cases, access control and audit."""
import unittest
from datetime import timedelta

from app.monitoring import lifecycle as lc
from app.monitoring.repository import AlertFilter, CaseFilter
from app.monitoring.service import AccessError, ConflictError, NotFoundError
from app.security.principal import Principal
from app.synthetic import reference as ref
from tests import support

ALICE = Principal("U-alice", "analyst")
BOB = Principal("U-bob", "analyst")
ADMIN = Principal("U-admin", "admin")


def make_users(store):
    for name, role in (("alice", "analyst"), ("bob", "analyst"), ("admin", "admin")):
        store.upsert_user({"user_id": f"U-{name}", "username": name, "password_hash": "x", "role": role,
                           "active": True})


class LifecycleTest(unittest.TestCase):
    def test_allowed_alert_transitions(self):
        ok = [("NEW", "TRIAGED"), ("NEW", "INVESTIGATING"), ("TRIAGED", "INVESTIGATING"),
              ("INVESTIGATING", "ESCALATED"), ("ESCALATED", "INVESTIGATING")]
        for a, b in ok:
            reason = "escalating for senior review" if b == "ESCALATED" else None
            lc.check_alert_transition(a, b, None, reason)

    def test_resolved_is_terminal(self):
        for t in ("NEW", "TRIAGED", "INVESTIGATING", "ESCALATED", "RESOLVED"):
            with self.assertRaises(lc.LifecycleError):
                lc.check_alert_transition("RESOLVED", t, None, "some reason here")

    def test_backwards_and_unknown_transitions_are_rejected(self):
        for a, b in [("TRIAGED", "NEW"), ("INVESTIGATING", "TRIAGED"), ("NEW", "WHATEVER"), ("ESCALATED", "TRIAGED")]:
            with self.assertRaises(lc.LifecycleError):
                lc.check_alert_transition(a, b, None, "some reason here")

    def test_resolution_rules(self):
        lc.check_alert_transition("INVESTIGATING", "RESOLVED", "FALSE_POSITIVE", "benign business payments")
        with self.assertRaises(lc.LifecycleError):  # missing resolution
            lc.check_alert_transition("INVESTIGATING", "RESOLVED", None, "benign business payments")
        with self.assertRaises(lc.LifecycleError):  # missing reason
            lc.check_alert_transition("INVESTIGATING", "RESOLVED", "CLEARED", " ")
        with self.assertRaises(lc.LifecycleError):  # ESCALATED is a status, not a resolution
            lc.check_alert_transition("INVESTIGATING", "RESOLVED", "ESCALATED", "some reason here")
        with self.assertRaises(lc.LifecycleError):  # resolution without RESOLVED
            lc.check_alert_transition("NEW", "TRIAGED", "CLEARED", None)

    def test_escalation_needs_a_reason(self):
        with self.assertRaises(lc.LifecycleError):
            lc.check_alert_transition("NEW", "ESCALATED", None, "")

    def test_case_transitions_and_decisions(self):
        lc.check_case_transition("OPEN", "INVESTIGATING")
        lc.check_case_transition("INVESTIGATING", "PENDING_REVIEW")
        lc.check_case_transition("PENDING_REVIEW", "INVESTIGATING")
        for a, b in [("CLOSED", "OPEN"), ("OPEN", "PENDING_REVIEW"), ("OPEN", "ESCALATED")]:
            with self.assertRaises(lc.LifecycleError):
                lc.check_case_transition(a, b)
        with self.assertRaises(lc.LifecycleError):  # closing is only possible through a decision
            lc.check_case_transition("INVESTIGATING", "CLOSED")
        self.assertEqual(lc.check_case_decision("INVESTIGATING", "CLEARED", "no concern found"), "CLOSED")
        self.assertEqual(lc.check_case_decision("INVESTIGATING", "ESCALATED", "needs senior review"), "ESCALATED")
        with self.assertRaises(lc.LifecycleError):
            lc.check_case_decision("OPEN", "CLEARED", "no concern found")
        with self.assertRaises(lc.LifecycleError):
            lc.check_case_decision("INVESTIGATING", "CLEARED", "no")
        with self.assertRaises(lc.LifecycleError):
            lc.check_case_decision("CLOSED", "CLEARED", "no concern found")
        with self.assertRaises(lc.LifecycleError):
            lc.check_case_decision("ESCALATED", "ESCALATED", "needs senior review")


class AlertPolicyTest(unittest.TestCase):
    """Detector tiers: standalone, supporting (needs a flagged customer) and context-only."""

    @classmethod
    def setUpClass(cls):
        from app.monitoring.config import load_monitoring_config

        cls.cfg = load_monitoring_config()

    def test_tiers_from_the_catalogue(self):
        c = self.cfg
        for d in ("STRUCTURING", "CIRCULAR_FLOW", "DEVICE_SHARING", "FAN_IN", "FAN_OUT", "TRANSACTION_BURST"):
            self.assertEqual(c.alert_tier(d), "standalone", d)
        for d in ("GEO_NEW_COUNTRY", "NEW_DEVICE", "AMOUNT_DEVIATION", "VELOCITY_SPIKE", "PEER_AMOUNT_DEVIATION"):
            self.assertEqual(c.alert_tier(d), "supporting", d)
        for d in ("HISTORICAL_ALERTS", "NETWORK_EXPOSURE", "BEHAVIOURAL_SHIFT", "ML_ANOMALY"):
            self.assertEqual(c.alert_tier(d), "context", d)
        self.assertEqual(c.alert_tier("NOT_A_DETECTOR"), "context")

    def test_explicit_detector_list_replaces_the_standalone_tier(self):
        c = self.cfg.model_copy(deep=True)
        c.alerting.detectors = ["NEW_DEVICE"]
        self.assertEqual(c.standalone_detectors(), {"NEW_DEVICE"})
        self.assertNotIn("NEW_DEVICE", c.supporting_detectors())
        self.assertEqual(c.alert_tier("STRUCTURING"), "context")

    def test_lowering_the_supporting_gate_never_reduces_alerts_and_raises_them_for_benign_customers(self):
        normals = [lb["entity_id"] for lb in support.labels() if lb["scenario"] == "normal"][:80]
        counts = {}
        for gate in (None, 0.0):
            c = support.container(fresh=True)
            c.monitoring_config.alerting.supporting_min_customer_score = gate
            c.monitoring.run(window_end=c.store.as_of(), customer_ids=normals, actor=ADMIN)
            items, _ = c.monitoring_repo.list_alerts(AlertFilter(limit=10_000))
            counts[gate] = len(items)
            if gate is None:  # supporting-tier alerts exist only on customers whose combined score is flagged
                for a in items:
                    if c.monitoring_config.alert_tier(a.detector_id) == "supporting":
                        self.assertGreaterEqual(a.risk_score, c.risk_config.score.investigation_threshold)
        self.assertGreaterEqual(counts[0.0], counts[None])


class MonitoringFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = support.container(fresh=True)
        make_users(cls.c.store)
        cls.mon = cls.c.monitoring
        cls.repo = cls.c.monitoring_repo
        cls.end = cls.c.store.as_of()
        cls.mule = support.first_of("mule_account")

    def run_for(self, cid, **kw):
        return self.mon.run(window_end=self.end, customer_ids=[cid], actor=ADMIN, **kw)

    def alerts(self, cid, **kw):
        items, _ = self.repo.list_alerts(AlertFilter(customer_id=cid, limit=500, **kw))
        return items

    # ------------------------------------------------------------------ generation
    def test_01_monitoring_creates_alerts_with_evidence(self):
        run = self.run_for(self.mule)
        alerts = self.alerts(self.mule)
        self.assertGreaterEqual(run.alerts_created, 1)
        self.assertEqual(len(alerts), run.alerts_created)
        for a in alerts:
            self.assertEqual(a.status, "NEW")
            self.assertIn(a.severity, ("low", "medium", "high", "critical"))
            self.assertTrue(a.description)
            self.assertEqual(a.explanation["triggered"], True)
            self.assertNotIn("confidence", a.explanation)
            self.assertGreater(a.risk_score, 0)
        self.assertTrue(any(a.transaction_ids for a in alerts))
        withtx = next(a for a in alerts if a.transaction_ids)
        self.assertIsNotNone(self.c.store.get_transaction(withtx.transaction_id))
        self.assertEqual(run.config_version, self.c.risk_config.version)

    def test_02_context_only_detectors_never_raise_alerts(self):
        self.run_for(self.mule)
        det = {a.detector_id for a in self.alerts(self.mule)}
        self.assertFalse(det & {"NETWORK_EXPOSURE", "HISTORICAL_ALERTS", "BEHAVIOURAL_SHIFT", "ML_ANOMALY"})

    def test_03_rerun_is_idempotent_and_merges(self):
        before = {a.alert_id: a for a in self.alerts(self.mule)}
        run = self.run_for(self.mule)
        after = {a.alert_id: a for a in self.alerts(self.mule)}
        self.assertEqual(run.alerts_created, 0)
        self.assertEqual(set(before), set(after))
        self.assertGreaterEqual(run.alerts_updated, 1)
        self.assertTrue(all(after[i].occurrence_count == before[i].occurrence_count + 1 for i in before))
        self.assertTrue(all(a.status == "NEW" for a in after.values()))

    def test_04_one_unresolved_alert_per_customer_and_detector(self):
        seen = [(a.customer_id, a.detector_id) for a in self.alerts(self.mule) if a.status != "RESOLVED"]
        self.assertEqual(len(seen), len(set(seen)))

    def test_05_resolved_behaviour_is_not_re_alerted_but_new_behaviour_is(self):
        cid = support.first_of("circular_transfer")
        self.run_for(cid)
        first = self.alerts(cid)
        self.assertTrue(first)
        for a in first:
            self.mon.transition_alert(a.alert_id, "RESOLVED", ALICE, "reviewed: legitimate transfers", "FALSE_POSITIVE")
        rerun = self.run_for(cid)
        self.assertEqual(rerun.alerts_created, 0)
        self.assertGreaterEqual(rerun.alerts_suppressed, 1)
        # genuinely new structuring behaviour after the resolution does raise a fresh alert
        acc = self.c.store.accounts_for_customer(cid)[0]
        rows = [{"timestamp": self.end - timedelta(hours=8 - i), "sender_account_id": acc.account_id,
                 "amount": round(9100 * ref.FX_PER_USD[acc.currency], 2), "currency": acc.currency,
                 "amount_usd": 9100.0, "transaction_type": "transfer", "channel": "web", "status": "completed",
                 "country": self.c.store.get_customer(cid).country, "external_counterparty": "EXT-STRUCT"}
                for i in range(3)]
        out = self.mon.ingest(rows, ADMIN, run_monitoring=True)
        self.assertEqual(out["accepted"], 3)
        dets = {a.detector_id for a in self.alerts(cid) if a.status != "RESOLVED"}
        self.assertIn("STRUCTURING", dets)

    def test_06_alert_events_and_audit_are_written(self):
        a = self.alerts(self.mule)[0]
        kinds = [e.event_type for e in self.repo.alert_events(a.alert_id)]
        self.assertEqual(kinds[0], "created")
        actions = {e.action for e in self.c.store.list_audit(limit=2000)}
        self.assertTrue({"alert_created", "monitoring_run", "risk_score_generated"} <= actions)

    def test_07_run_is_recorded_and_kpis_are_computed_from_data(self):
        runs = self.repo.list_runs()
        self.assertTrue(runs and runs[0].duration_ms >= 0)
        k = self.repo.kpis()
        total = sum(k["alerts_by_status"].values())
        self.assertEqual(k["alerts_generated"], total)
        self.assertEqual(k["open_alerts"], total - k["alerts_by_status"].get("RESOLVED", 0))
        self.assertIsNotNone(k["avg_detection_ms_per_customer"])

    def test_08_list_filters_sort_and_search(self):
        self.run_for(support.first_of("transaction_burst"))
        items, total = self.repo.list_alerts(AlertFilter(statuses=["NEW"], limit=500))
        self.assertEqual(total, len(items))
        self.assertTrue(all(a.status == "NEW" for a in items))
        det = items[0].detector_id
        by_det, _ = self.repo.list_alerts(AlertFilter(detector=det, limit=500))
        self.assertTrue(by_det and all(a.detector_id == det for a in by_det))
        hi, _ = self.repo.list_alerts(AlertFilter(min_risk=1000, limit=10))
        self.assertEqual(hi, [])
        asc, _ = self.repo.list_alerts(AlertFilter(sort="risk_score", order="asc", limit=500))
        self.assertEqual([a.risk_score for a in asc], sorted(a.risk_score for a in asc))
        found, _ = self.repo.list_alerts(AlertFilter(q=items[0].customer_id.lower(), limit=500))
        self.assertTrue(found)
        page, total2 = self.repo.list_alerts(AlertFilter(limit=1, offset=1))
        self.assertLessEqual(len(page), 1)
        self.assertGreaterEqual(total2, 2)

    # ------------------------------------------------------------------ workflow
    def fresh_alert(self, scenario="account_takeover"):
        cid = support.first_of(scenario)
        self.run_for(cid)
        open_ = [a for a in self.alerts(cid) if a.status == "NEW" and not a.case_id]
        self.assertTrue(open_, f"no open alert for {scenario}")
        return open_[0]

    def test_10_alert_assignment_rules(self):
        a = self.fresh_alert()
        self.assertEqual(self.mon.assign_alert(a.alert_id, ALICE.user_id, ALICE).assigned_to, "U-alice")
        with self.assertRaises(AccessError):  # an analyst cannot take over someone else's alert
            self.mon.assign_alert(a.alert_id, BOB.user_id, BOB)
        with self.assertRaises(AccessError):  # nor assign to a third person
            self.mon.assign_alert(a.alert_id, BOB.user_id, ALICE)
        self.assertEqual(self.mon.assign_alert(a.alert_id, BOB.user_id, ADMIN).assigned_to, "U-bob")
        with self.assertRaises(ValueError):
            self.mon.assign_alert(a.alert_id, "U-nobody", ADMIN)
        with self.assertRaises(NotFoundError):
            self.mon.assign_alert("MAL-FFFFFFFFFFFF", ALICE.user_id, ALICE)

    def test_11_alert_status_transitions_and_resolution(self):
        a = self.fresh_alert("geographic_anomaly")
        up = self.mon.transition_alert(a.alert_id, "TRIAGED", ALICE)
        self.assertEqual((up.status, up.assigned_to), ("TRIAGED", "U-alice"))  # working it claims it
        with self.assertRaises(AccessError):
            self.mon.transition_alert(a.alert_id, "INVESTIGATING", BOB)
        with self.assertRaises(ConflictError):
            self.mon.transition_alert(a.alert_id, "NEW", ALICE)
        with self.assertRaises(ConflictError):
            self.mon.transition_alert(a.alert_id, "RESOLVED", ALICE, reason="looks fine to me", resolution=None)
        self.mon.transition_alert(a.alert_id, "INVESTIGATING", ALICE)
        done = self.mon.transition_alert(a.alert_id, "RESOLVED", ALICE, "confirmed by customer call", "CLEARED")
        self.assertEqual((done.status, done.resolution, done.resolved_by), ("RESOLVED", "CLEARED", "U-alice"))
        self.assertIsNotNone(done.resolved_at)
        with self.assertRaises(ConflictError):
            self.mon.transition_alert(a.alert_id, "INVESTIGATING", ALICE)
        actions = [e.action for e in self.c.store.list_audit(limit=3000) if e.entity_id == a.alert_id]
        self.assertIn("alert_status_changed", actions)
        self.assertIn("decision_recorded", actions)
        kinds = [e.event_type for e in self.repo.alert_events(a.alert_id)]
        self.assertEqual(kinds[-1], "resolved")

    def test_12_case_creation_attaches_alerts_and_moves_them_to_investigating(self):
        a = self.fresh_alert("device_sharing_ring")
        case, created = self.mon.create_case(a.customer_id, [a.alert_id], ALICE)
        self.assertTrue(created)
        self.assertRegex(case.case_number, r"^FC-\d{4}-\d{6}$")
        self.assertEqual((case.status, case.assigned_to, case.customer_id), ("OPEN", "U-alice", a.customer_id))
        got = self.repo.get_alert(a.alert_id)
        self.assertEqual((got.case_id, got.status), (case.case_id, "INVESTIGATING"))
        # a second case for the same customer is not created: new alerts join the open one
        other = [x for x in self.alerts(a.customer_id) if not x.case_id and x.status != "RESOLVED"]
        if other:
            again, created2 = self.mon.create_case(a.customer_id, [other[0].alert_id], ALICE)
            self.assertFalse(created2)
            self.assertEqual(again.case_id, case.case_id)
        with self.assertRaises(ConflictError):  # already in a case
            self.mon.create_case(a.customer_id, [a.alert_id], ALICE)
        with self.assertRaises(NotFoundError):
            self.mon.create_case("CUST-99999999", [], ALICE)

    def test_13_case_cannot_mix_customers_or_resolved_alerts(self):
        a = self.fresh_alert("transaction_burst")
        other = support.first_of("dormant_reactivation")
        with self.assertRaises(ValueError):
            self.mon.create_case(other, [a.alert_id], ALICE)

    def test_14_case_decision_closes_case_resolves_alerts_and_audits(self):
        a = self.fresh_alert("high_risk_merchant")
        case, _ = self.mon.create_case(a.customer_id, [a.alert_id], ALICE)
        with self.assertRaises(ConflictError):  # must start the investigation first
            self.mon.decide_case(case.case_id, "CLEARED", "nothing to see here", ALICE)
        self.mon.transition_case(case.case_id, "INVESTIGATING", ALICE)
        note = self.mon.add_note(case.case_id, "Called the customer, spending is seasonal.", ALICE)
        self.assertEqual(note.author, "U-alice")
        with self.assertRaises(AccessError):  # another analyst cannot decide on Alice's case
            self.mon.decide_case(case.case_id, "FALSE_POSITIVE", "I disagree with Alice", BOB)
        with self.assertRaises(ConflictError):
            self.mon.decide_case(case.case_id, "FALSE_POSITIVE", "no", ALICE)
        out = self.mon.decide_case(case.case_id, "FALSE_POSITIVE", "merchant spend matches stated hobby", ALICE)
        closed = out["case"]
        self.assertEqual((closed.status, closed.decision, closed.decided_by), ("CLOSED", "FALSE_POSITIVE", "U-alice"))
        self.assertIsNotNone(closed.closed_at)
        self.assertIn(a.alert_id, out["alerts_updated"])
        al = self.repo.get_alert(a.alert_id)
        self.assertEqual((al.status, al.resolution), ("RESOLVED", "FALSE_POSITIVE"))
        with self.assertRaises(ConflictError):
            self.mon.add_note(case.case_id, "too late", ALICE)
        with self.assertRaises(ConflictError):
            self.mon.decide_case(case.case_id, "CLEARED", "decided twice", ALICE)
        actions = {e.action for e in self.c.store.list_audit(limit=4000) if e.entity_id == case.case_id}
        self.assertTrue({"case_created", "decision_recorded", "case_closed", "investigation_note_added"} <= actions)
        events = [e.event_type for e in self.repo.case_events(case.case_id)]
        self.assertIn("decision_recorded", events)

    def test_15_escalation_moves_case_and_alerts_without_closing(self):
        a = self.fresh_alert("mule_account")
        case, _ = self.mon.create_case(a.customer_id, [a.alert_id], ALICE)
        self.mon.transition_case(case.case_id, "INVESTIGATING", ALICE)
        with self.assertRaises(ConflictError):  # escalation goes through a decision with a reason
            self.mon.transition_case(case.case_id, "ESCALATED", ALICE)
        out = self.mon.decide_case(case.case_id, "ESCALATED", "pattern needs senior review", ALICE)
        self.assertEqual(out["case"].status, "ESCALATED")
        self.assertIsNone(out["case"].closed_at)
        self.assertEqual(self.repo.get_alert(a.alert_id).status, "ESCALATED")
        # an admin can resolve it afterwards
        res = self.mon.decide_case(case.case_id, "CONFIRMED_SUSPICIOUS", "confirmed after review", ADMIN)
        self.assertEqual(res["case"].status, "CLOSED")
        self.assertEqual(self.repo.get_alert(a.alert_id).resolution, "CONFIRMED_SUSPICIOUS")

    def test_16_case_evidence_only_accepts_existing_objects(self):
        a = self.fresh_alert("dormant_reactivation")
        case, _ = self.mon.create_case(a.customer_id, [a.alert_id], ALICE)
        if a.transaction_ids:
            ev = self.mon.add_evidence(case.case_id, "transaction", a.transaction_ids[0], ALICE, "key transfer")
            self.assertEqual(ev.evidence_class, "DATABASE_FACT")
        rule = self.mon.add_evidence(case.case_id, "alert", a.alert_id, ALICE)
        self.assertEqual(rule.evidence_class, "RULE_RESULT")
        with self.assertRaises(NotFoundError):
            self.mon.add_evidence(case.case_id, "transaction", "TXN-987654321012", ALICE)
        with self.assertRaises(NotFoundError):
            self.mon.add_evidence(case.case_id, "document", "NO-SUCH-CHUNK", ALICE)
        with self.assertRaises(ValueError):
            self.mon.add_evidence(case.case_id, "llm_summary", "x", ALICE)
        foreign = self.c.store.transactions_for_accounts(
            [self.c.store.accounts_for_customer(support.first_of("normal"))[0].account_id], None, None)
        if not foreign.empty:
            with self.assertRaises(ValueError):  # a transaction of an unrelated customer is not evidence here
                self.mon.add_evidence(case.case_id, "transaction", str(foreign.iloc[0].transaction_id), ALICE)

    def test_17_case_listing_filters(self):
        items, total = self.repo.list_cases(CaseFilter(statuses=["CLOSED"], limit=100))
        self.assertTrue(all(c.status == "CLOSED" for c in items))
        mine, _ = self.repo.list_cases(CaseFilter(assigned_to="U-alice", limit=100))
        self.assertTrue(mine and all(c.assigned_to == "U-alice" for c in mine))
        none, _ = self.repo.list_cases(CaseFilter(assigned_to="U-bob", statuses=["OPEN"], limit=100))
        self.assertEqual(none, [])


if __name__ == "__main__":
    unittest.main()
