"""Regression-baseline comparison used by the performance benchmark."""
import unittest

from app.monitoring.benchmark import compare, extract_metrics


def result(p50=100.0, ms_per=100.0, rows=1000.0):
    return {"results": [{"transactions": 10000, "backends": [{
        "backend": "frames", "risk_scoring_per_customer": {"p50_ms": p50}, "daily_batch": {"ms_per_customer": ms_per},
        "api_batch_ingestion": {"rows_per_second": rows}, "queries": {}, "v3": {}}]}]}


class BaselineTest(unittest.TestCase):
    def test_extract_only_tracked_present_metrics(self):
        m = extract_metrics(result())
        self.assertEqual(len(m), 3)
        self.assertIn("10000|frames|risk_scoring_per_customer.p50_ms", m)

    def test_within_tolerance_is_not_a_regression(self):
        rows = compare(result(), result(p50=140, rows=800), tolerance=0.5)
        self.assertFalse(any(r["regression"] for r in rows))

    def test_slowdown_and_throughput_drop_are_regressions(self):
        rows = {r["label"]: r for r in compare(result(), result(p50=200, rows=500), tolerance=0.5)}
        self.assertTrue(rows["risk scoring p50 (ms)"]["regression"])
        self.assertTrue(rows["batch ingestion rows/s"]["regression"])
        self.assertFalse(rows["daily batch ms per customer"]["regression"])

    def test_improvement_and_missing_metrics_are_ignored(self):
        rows = compare(result(), result(p50=10, ms_per=10, rows=9000))
        self.assertFalse(any(r["regression"] for r in rows))
        self.assertEqual(compare(result(), {"results": []}), [])


if __name__ == "__main__":
    unittest.main()
