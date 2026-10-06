"""Evaluation metrics (pure functions) and the monitoring benchmark generator."""
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from app.evaluation.monitoring_eval import confusion, pr_auc, rates, roc_auc, to_markdown
from app.synthetic.generator import generate_dataset
from app.synthetic.monitoring_benchmark import NEW_BENIGN, NEW_SUSPICIOUS, generate_monitoring_benchmark


class MetricsTest(unittest.TestCase):
    Y = {"a": True, "b": True, "c": False, "d": False, "e": False}

    def test_confusion_and_rates_on_a_known_case(self):
        cm = confusion(self.Y, {"a", "c"})
        self.assertEqual(cm, {"tp": 1, "fp": 1, "fn": 1, "tn": 2})
        r = rates(cm)
        self.assertAlmostEqual(r["precision"], 0.5)
        self.assertAlmostEqual(r["recall"], 0.5)
        self.assertAlmostEqual(r["f1"], 0.5)
        self.assertAlmostEqual(r["false_positive_rate"], 1 / 3)

    def test_degenerate_cases_do_not_divide_by_zero(self):
        cm = confusion(self.Y, set())
        r = rates(cm)
        self.assertIsNone(r["precision"])
        self.assertEqual(r["recall"], 0.0)
        self.assertEqual(r["false_positive_rate"], 0.0)
        self.assertEqual(rates({"tp": 0, "fp": 0, "fn": 0, "tn": 0}), {"precision": None, "recall": None, "f1": None,
                                                                       "false_positive_rate": None})

    def test_pr_auc_perfect_and_no_skill_and_undefined(self):
        self.assertAlmostEqual(pr_auc(self.Y, {"a": 90, "b": 80, "c": 10}), 1.0)
        worst = pr_auc(self.Y, {"c": 90, "d": 80, "e": 70, "a": 1, "b": 0.5})
        self.assertLess(worst, 0.5)
        self.assertIsNone(pr_auc({"a": False, "b": False}, {}))
        self.assertIsNone(roc_auc({"a": True}, {"a": 1.0}))
        self.assertAlmostEqual(roc_auc(self.Y, {"a": 90, "b": 80, "c": 10}), 1.0)

    def test_markdown_report_states_the_synthetic_caveat_and_has_the_headline_table(self):
        res = {
            "created_at": "2026-01-01T00:00:00+00:00",
            "dataset": {"generator": "g", "seed": 1, "customers": 10, "transactions": 100, "labelled_suspicious": 2},
            "configuration": {"risk_config": "r", "risk_config_fingerprint": "f", "monitoring_config": "m",
                              "monitoring_fingerprint": "f", "lookback_days": 30, "baseline_days": 90,
                              "window_end": "2026-01-01T00:00:00", "thresholds_tuned_on_this_data": False,
                              "supporting_min_customer_score": None},
            "environment": {"python": "3", "platform": "x"},
            "overall": {**confusion(self.Y, {"a"}), **rates(confusion(self.Y, {"a"})), "pr_auc": 0.5, "roc_auc": 0.6,
                        "prevalence": 0.4},
            "operating_points": [], "per_scenario": {}, "expected_detector_recall": {}, "per_detector": {},
            "triage_quality": {"oracle": "o", "alerts": 2, "overall_confirmed_rate": 0.5, "by_priority": {},
                               "monotonic_confirmed_rate": True, "top_k_precision": {"triage_score": {"10": 1.0},
                                                                                     "customer_risk_score": {"10": 1.0},
                                                                                     "random_order": {"10": 0.5}},
                               "alert_level_roc_auc": {"triage_score": 0.7, "customer_risk_score": 0.8}},
            "money_mule": {"universe": {"labelled_customers": 1, "unlabelled_sample": 1, "mule_typology_customers": 1,
                                        "typologies": ["x"]}, "note": "n", "roc_auc_score": 0.9, "per_scenario": {},
                           **{k: {**rates(confusion(self.Y, {"a"})), **confusion(self.Y, {"a"})}
                              for k in ("medium_or_high", "high_only", "baseline_fan_in_or_rapid_alert")}},
            "volumes": {"alerts": 1, "alerting_customers": 1, "severity": {}, "transactions_in_window": 10,
                        "alerts_per_1000_window_transactions": 100.0, "alerts_per_1000_customers": 100.0},
            "performance": {"monitoring_run_s": 1, "customers_per_second": 10, "window_transactions_per_second": 10,
                            "latency_sample_size": 1,
                            "detection_latency_ms": {"mean": 1, "p50": 1, "p95": 1, "p99": 1}},
        }
        md = to_markdown(res)
        self.assertIn("SYNTHETIC data only", md)
        self.assertIn("not evidence of real-world", md)
        self.assertIn("| precision | recall | F1 | FPR | PR-AUC |", md)
        self.assertIn("alerts per 1,000 transactions", md)
        self.assertIn("Triage quality", md)
        self.assertIn("Money-mule indicators", md)


class TriageQualityTest(unittest.TestCase):
    def test_confirmed_rate_by_priority_and_monotonic_flag(self):
        from types import SimpleNamespace as N

        from app.evaluation.monitoring_eval import triage_quality

        def a(cid, pri, score, risk):
            return N(customer_id=cid, triage_priority=pri, triage_score=score, risk_score=risk)

        alerts = [a("s1", "HIGH", 80, 70), a("s2", "HIGH", 70, 60), a("b1", "LOW", 10, 20), a("s3", "LOW", 5, 10)]
        r = triage_quality(alerts, {"s1", "s2", "s3"})
        self.assertEqual(r["by_priority"]["HIGH"]["confirmed_rate"], 1.0)
        self.assertEqual(r["by_priority"]["LOW"]["confirmed_rate"], 0.5)
        self.assertTrue(r["monotonic_confirmed_rate"])
        inverted = triage_quality([a("b1", "HIGH", 80, 70), a("s1", "LOW", 5, 10)], {"s1"})
        self.assertFalse(inverted["monotonic_confirmed_rate"])


class BenchmarkGeneratorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = Path(tempfile.mkdtemp(prefix="fira-bench-a-"))
        cls.b = Path(tempfile.mkdtemp(prefix="fira-bench-b-"))
        generate_monitoring_benchmark(cls.a, n_customers=700, seed=11)
        generate_monitoring_benchmark(cls.b, n_customers=700, seed=11)
        cls.labels = json.loads((cls.a / "scenario_labels.json").read_text(encoding="utf-8"))
        cls.tx = pd.read_csv(cls.a / "transactions.csv.gz")
        cls.tx["timestamp"] = pd.to_datetime(cls.tx["timestamp"], utc=True, format="ISO8601")

    def test_new_scenarios_exist_and_are_labelled_correctly(self):
        by = {}
        for lb in self.labels:
            by.setdefault(lb["scenario"], []).append(lb)
        for name in NEW_SUSPICIOUS | NEW_BENIGN:
            self.assertIn(name, by, name)
        self.assertTrue(all(lb["is_suspicious"] for n in NEW_SUSPICIOUS for lb in by[n]))
        self.assertFalse(any(lb["is_suspicious"] for n in NEW_BENIGN for lb in by[n]))
        # evasive positives expect no detector; detectable ones expect one
        self.assertEqual(by["structuring_evasive"][0]["expected_signals"], [])
        self.assertEqual(by["structuring_detectable"][0]["expected_signals"], ["STRUCTURING"])
        self.assertEqual(by["fan_out_detectable"][0]["expected_signals"], ["FAN_OUT"])

    def test_generation_is_deterministic_for_a_seed(self):
        self.assertEqual((self.a / "scenario_labels.json").read_text(encoding="utf-8"),
                         (self.b / "scenario_labels.json").read_text(encoding="utf-8"))
        x = pd.read_csv(self.a / "transactions.csv.gz")
        y = pd.read_csv(self.b / "transactions.csv.gz")
        self.assertTrue(x.equals(y))

    def test_evasive_structuring_never_has_three_near_threshold_transactions_in_24h(self):
        accounts = pd.read_csv(self.a / "accounts.csv.gz")
        for lb in self.labels:
            if lb["scenario"] != "structuring_evasive":
                continue
            accs = set(accounts[accounts.customer_id == lb["entity_id"]].account_id)
            t = self.tx[self.tx.sender_account_id.isin(accs) & (self.tx.amount_usd >= 8000) & (self.tx.amount_usd < 10000)]
            ts = sorted(t.timestamp)
            for i in range(len(ts) - 2):
                self.assertGreater((ts[i + 2] - ts[i]).total_seconds(), 24 * 3600)

    def test_the_default_generator_is_unchanged_by_the_extension_hook(self):
        d = Path(tempfile.mkdtemp(prefix="fira-default-"))
        manifest = generate_dataset(d, n_customers=400, seed=42)
        self.assertFalse(set(manifest["scenarios"]) & (NEW_SUSPICIOUS | NEW_BENIGN))
        self.assertEqual(manifest["generator"], "fira-synthetic")


if __name__ == "__main__":
    unittest.main()
