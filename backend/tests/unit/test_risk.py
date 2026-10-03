import unittest
from datetime import datetime, timezone

from app.risk.config import RiskConfig, load_risk_config, to_yaml
from app.risk.engine import RiskEngine, strength
from app.risk.models import RiskSignal
from tests import support

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


def _sig(t: str, s: float) -> RiskSignal:
    return RiskSignal(signal_id=t, signal_type=t, entity_type="customer", entity_id="CUST-1", observed_value=1,
                      baseline_value=0, threshold=1, unit="x", severity="high", strength=s, confidence=0.9,
                      description="d", timestamp=NOW, window_start=NOW, window_end=NOW)


class ScoringTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_risk_config(support.make_settings().risk_config_path)
        self.eng = RiskEngine(store=None, graph=None, config=self.cfg)  # scoring needs no data

    def test_strength_ramp(self):
        self.assertEqual(strength(10, 10, 1.0), 0.5)
        self.assertEqual(strength(15, 10, 1.0), 0.75)
        self.assertEqual(strength(100, 10, 1.0), 1.0)

    def test_contributions_are_weight_times_strength(self):
        contribs, score = self.eng.score([_sig("CIRCULAR_FLOW", 1.0)])
        self.assertEqual(contribs[0].points, self.cfg.signals["CIRCULAR_FLOW"].weight)
        self.assertEqual(score, self.cfg.signals["CIRCULAR_FLOW"].weight)

    def test_group_cap_prevents_double_counting(self):
        contribs, score = self.eng.score([_sig("TRANSACTION_BURST", 1.0), _sig("VELOCITY_SPIKE", 1.0)])
        cap = self.cfg.group_caps["velocity"]
        self.assertAlmostEqual(sum(c.points for c in contribs), cap)
        self.assertTrue(any(c.capped for c in contribs))
        self.assertEqual(score, cap)

    def test_total_capped_at_100(self):
        sigs = [_sig(t, 1.0) for t in ("CIRCULAR_FLOW", "DORMANT_REACTIVATION", "IMPOSSIBLE_TRAVEL", "DEVICE_SHARING",
                                        "RAPID_PASS_THROUGH", "HIGH_RISK_MERCHANT")]
        _, score = self.eng.score(sigs)
        self.assertEqual(score, 100)

    def test_bands_and_fingerprint(self):
        self.assertEqual(self.cfg.band(0), "low")
        self.assertEqual(self.cfg.band(60), "high")
        self.assertEqual(self.cfg.band(80), "critical")
        again = RiskConfig(**__import__("yaml").safe_load(to_yaml(self.cfg)))
        self.assertEqual(again.fingerprint(), self.cfg.fingerprint())
        again.signals["CIRCULAR_FLOW"].weight = 1
        self.assertNotEqual(again.fingerprint(), self.cfg.fingerprint())


class DetectorTest(unittest.TestCase):
    """Detectors against the synthetic ground truth (small dataset)."""

    @classmethod
    def setUpClass(cls):
        cls.c = support.container()
        cls.labels = support.labels()

    def _assess(self, scenario):
        ids = [lb["entity_id"] for lb in self.labels if lb["scenario"] == scenario][:4]
        self.assertTrue(ids, scenario)
        return [self.c.risk_engine.assess_customer(i) for i in ids]

    def test_each_suspicious_scenario_fires_its_expected_signal(self):
        from app.synthetic.generator import SCENARIO_EXPECTED_SIGNALS, SUSPICIOUS_SCENARIOS

        for scen in sorted(SUSPICIOUS_SCENARIOS):
            res = self._assess(scen)
            expected = set(SCENARIO_EXPECTED_SIGNALS[scen])
            hit = sum(1 for a in res if expected & set(a.signal_types()))
            self.assertGreaterEqual(hit / len(res), 0.5, f"{scen}: {[a.signal_types() for a in res]}")

    def test_normal_customers_mostly_quiet(self):
        res = self._assess("normal")
        self.assertTrue(all(not a.flagged for a in res), [a.score for a in res])

    def test_false_positive_traps_not_flagged(self):
        for scen in ("high_frequency_legit", "legit_high_value"):
            res = self._assess(scen)
            self.assertTrue(all(not a.flagged for a in res), (scen, [a.score for a in res]))

    def test_signal_fields_complete_and_inspectable(self):
        a = self._assess("circular_transfer")[0]
        self.assertTrue(a.signals)
        for s in a.signals:
            for f in ("signal_id", "signal_type", "observed_value", "threshold", "severity", "evidence", "timestamp"):
                self.assertIsNotNone(getattr(s, f), f)
        self.assertAlmostEqual(min(100.0, sum(c.points for c in a.contributors)), a.score, places=1)
        self.assertTrue(a.metrics)  # raw metrics recorded even for non-firing detectors

    def test_unknown_customer(self):
        from app.risk.engine import EntityNotFound

        with self.assertRaises(EntityNotFound):
            self.c.risk_engine.assess_customer("CUST-999999999")

    def test_without_graph_relationship_signals_not_evaluated(self):
        eng = RiskEngine(self.c.store, None, self.c.risk_config)
        a = eng.assess_customer(self.labels[0]["entity_id"])
        reasons = {n.signal_type: n.reason for n in a.not_evaluated}
        self.assertIn("CIRCULAR_FLOW", reasons)
        self.assertTrue(any("Graph backend unavailable" in d for d in a.data_quality))


if __name__ == "__main__":
    unittest.main()
