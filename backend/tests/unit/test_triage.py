"""Explainable alert triage: factor arithmetic, priority bands, determinism, persistence and recompute."""
import unittest

from pydantic import ValidationError

from app.monitoring import triage as tg
from app.monitoring.config import MonitoringConfig, TriageConfig, load_monitoring_config
from app.monitoring.repository import AlertFilter
from app.security.principal import Principal
from tests import support
from tests.unit.test_monitoring_alerts import make_users

ADMIN = Principal("U-admin", "admin")
CFG = TriageConfig()


def inputs(**kw):
    base = dict(severity="medium", customer_score=40.0, tier="standalone", categories=1, amount_usd=2000.0,
                txn_count=2, occurrence_count=1, network_points=0.0, identity_signals=0, prior_confirmed=0,
                prior_cleared_same_detector=0, age_hours=1.0)
    base.update(kw)
    return tg.TriageInputs(**base)


class TriageFunctionTest(unittest.TestCase):
    def test_factor_maxima_sum_to_exactly_100(self):
        self.assertEqual(sum(f[2] for f in tg.FACTORS), 100.0)
        self.assertEqual(len({f[0] for f in tg.FACTORS}), 12)

    def test_maximal_inputs_reach_100_and_minimal_inputs_stay_low(self):
        top = tg.compute_triage(inputs(severity="critical", customer_score=100, categories=5, amount_usd=1e6,
                                       txn_count=9, occurrence_count=9, network_points=50, identity_signals=3,
                                       prior_confirmed=2, age_hours=500), CFG)
        self.assertEqual((top.score, top.priority), (100.0, "CRITICAL"))
        low = tg.compute_triage(inputs(severity="low", customer_score=0, tier="supporting", categories=0,
                                       amount_usd=None, txn_count=0, prior_cleared_same_detector=2, age_hours=0), CFG)
        self.assertEqual(low.priority, "LOW")
        self.assertLess(low.score, 15)

    def test_score_equals_sum_of_factor_points_and_each_factor_is_capped(self):
        r = tg.compute_triage(inputs(customer_score=250, network_points=999), CFG)  # out-of-range inputs are capped
        self.assertAlmostEqual(r.score, round(sum(f["points"] for f in r.factors), 1), places=1)
        for f in r.factors:
            self.assertLessEqual(f["points"], f["max"])
            self.assertGreaterEqual(f["points"], 0)
            self.assertTrue(f["reason"])
        self.assertLessEqual(r.score, 100)

    def test_deterministic(self):
        a, b = tg.compute_triage(inputs(), CFG), tg.compute_triage(inputs(), CFG)
        self.assertEqual((a.score, a.priority, a.factors), (b.score, b.priority, b.factors))

    def test_monotonic_in_severity_amount_and_score(self):
        s = [tg.compute_triage(inputs(severity=v), CFG).score for v in ("low", "medium", "high", "critical")]
        self.assertEqual(s, sorted(s))
        a = [tg.compute_triage(inputs(amount_usd=v), CFG).score for v in (10, 2000, 7000, 20000, 90000)]
        self.assertEqual(a, sorted(a))
        c = [tg.compute_triage(inputs(customer_score=v), CFG).score for v in (0, 30, 60, 100)]
        self.assertEqual(c, sorted(c))

    def test_history_rules(self):
        base = tg.compute_triage(inputs(), CFG).score
        self.assertGreater(tg.compute_triage(inputs(prior_confirmed=1), CFG).score, base)
        self.assertLess(tg.compute_triage(inputs(prior_cleared_same_detector=1), CFG).score, base)

    def test_age_only_applies_to_unresolved_alerts(self):
        old = tg.compute_triage(inputs(age_hours=200), CFG)
        fresh = tg.compute_triage(inputs(age_hours=1), CFG)
        resolved = tg.compute_triage(inputs(age_hours=None), CFG)
        self.assertGreater(old.score, fresh.score)
        self.assertEqual(next(f for f in resolved.factors if f["id"] == "alert_age")["points"], 0)

    def test_missing_transaction_evidence_is_stated_not_guessed(self):
        r = tg.compute_triage(inputs(amount_usd=None, txn_count=0), CFG)
        f = next(x for x in r.factors if x["id"] == "amount_at_risk")
        self.assertEqual((f["points"], f["value"]), (0, None))
        self.assertIn("no transaction evidence", f["reason"])

    def test_priority_thresholds_are_inclusive_lower_bounds(self):
        th = CFG.thresholds
        self.assertEqual(tg.priority_for(th["critical"], th), "CRITICAL")
        self.assertEqual(tg.priority_for(th["critical"] - 0.1, th), "HIGH")
        self.assertEqual(tg.priority_for(th["high"], th), "HIGH")
        self.assertEqual(tg.priority_for(th["medium"], th), "MEDIUM")
        self.assertEqual(tg.priority_for(th["medium"] - 0.1, th), "LOW")

    def test_result_has_no_probability_or_confidence_wording(self):
        r = tg.compute_triage(inputs(), CFG)
        text = " ".join(f["label"] + f["reason"] for f in r.factors).lower()
        self.assertNotIn("probab", text)
        self.assertNotIn("confidence", text)

    def test_inputs_never_include_the_alerts_own_outcome(self):
        self.assertFalse({"resolution", "outcome", "label", "disposition"} & set(tg.TriageInputs.__dataclass_fields__))


class TriageConfigTest(unittest.TestCase):
    def test_default_file_loads_with_documented_thresholds(self):
        c = load_monitoring_config()
        self.assertEqual(c.triage.thresholds, {"critical": 65.0, "high": 50.0, "medium": 30.0})

    def test_invalid_thresholds_rejected(self):
        for bad in ({"critical": 50, "high": 60, "medium": 10}, {"critical": 90, "high": 70}, {"critical": 101, "high": 50, "medium": 10}):
            with self.assertRaises(ValidationError):
                TriageConfig(thresholds=bad)

    def test_tables_must_be_ascending(self):
        with self.assertRaises(ValidationError):
            TriageConfig(amount_tiers=[[5000, 5], [1000, 2]])

    def test_triage_settings_change_the_fingerprint(self):
        a = MonitoringConfig()
        b = MonitoringConfig(triage=TriageConfig(thresholds={"critical": 80, "high": 60, "medium": 40}))
        self.assertNotEqual(a.fingerprint(), b.fingerprint())


class TriageServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = support.container(fresh=True)
        make_users(cls.c.store)
        cls.mon, cls.repo = cls.c.monitoring, cls.c.monitoring_repo
        cls.end = cls.c.store.as_of()
        cls.mule = support.first_of("mule_account")
        cls.mon.run(window_end=cls.end, customer_ids=[cls.mule], actor=ADMIN)
        cls.alerts = cls.repo.list_alerts(AlertFilter(customer_id=cls.mule, limit=100))[0]

    def test_every_new_alert_is_scored_with_factors_that_add_up(self):
        self.assertTrue(self.alerts)
        for a in self.alerts:
            self.assertIsNotNone(a.triage_score)
            self.assertIn(a.triage_priority, tg.PRIORITIES)
            self.assertEqual(len(a.triage_factors), 12)
            self.assertAlmostEqual(a.triage_score, sum(f["points"] for f in a.triage_factors), delta=0.11)
            self.assertIn("triage_context", a.explanation)

    def test_queue_filter_and_sort_by_priority(self):
        items, total = self.repo.list_alerts(AlertFilter(sort="triage_score", order="desc", limit=500))
        scores = [a.triage_score for a in items]
        self.assertEqual(scores, sorted(scores, reverse=True))
        top = items[0].triage_priority
        sel, n = self.repo.list_alerts(AlertFilter(priorities=[top], limit=500))
        self.assertTrue(n >= 1 and all(a.triage_priority == top for a in sel))
        none, n0 = self.repo.list_alerts(AlertFilter(priorities=["NOPE"], limit=5))
        self.assertEqual(n0, 0)

    def open_alert(self):
        items, _ = self.repo.list_alerts(AlertFilter(customer_id=self.mule, statuses=["NEW", "TRIAGED"], limit=100))
        if not items:
            self.skipTest("no unresolved alert left for this customer")
        return items[0]

    def test_explanation_endpoint_payload(self):
        a = self.alerts[0]
        e = self.mon.triage_explanation(a.alert_id)
        self.assertEqual((e["score"], e["max_total"]), (a.triage_score, 100.0))
        self.assertIn("not a probability", e["method"])

    def test_rerun_merges_and_rescoring_records_priority_change(self):
        a = self.open_alert()
        stale = a.triage_computed_at
        self.repo.update_alert(a.alert_id, triage_priority="LOW", triage_score=1.0)  # simulate a stale score
        out = self.mon.recompute_triage(ADMIN, a.alert_id)
        self.assertEqual(out["recomputed"], 1)
        after = self.repo.get_alert(a.alert_id)
        self.assertGreater(after.triage_score, 1.0)
        self.assertGreaterEqual(after.triage_computed_at, stale)
        events = [e for e in self.repo.alert_events(a.alert_id) if e.event_type == "priority_changed"]
        self.assertTrue(events)
        self.assertEqual(events[-1].detail["previous"], "LOW")
        self.assertEqual(events[-1].detail["cause"], "recompute")

    def test_resolved_alert_keeps_its_triage(self):
        b = self.open_alert()
        self.mon.transition_alert(b.alert_id, "RESOLVED", ADMIN, "reviewed, benign pattern", "CLEARED")
        frozen = self.repo.get_alert(b.alert_id)
        with self.assertRaises(Exception):
            self.mon.recompute_triage(ADMIN, b.alert_id)
        again = self.repo.get_alert(b.alert_id)
        self.assertEqual((again.triage_score, again.triage_computed_at), (frozen.triage_score, frozen.triage_computed_at))

    def test_history_uses_earlier_outcomes_not_the_alerts_own(self):
        a = self.open_alert()
        before = self.mon._history(a)
        self.mon.transition_alert(a.alert_id, "RESOLVED", ADMIN, "confirmed after review", "CONFIRMED_SUSPICIOUS")
        self.assertEqual(self.mon._history(self.repo.get_alert(a.alert_id)), before)  # own outcome excluded


if __name__ == "__main__":
    unittest.main()
