import unittest

import numpy as np
import pandas as pd

from app.analytics.geo import detect_impossible_travel, find_transactions_near_location, haversine_km
from app.analytics.stats import jensen_shannon, max_count_in_window, pass_through, split_customer_frame
from app.evaluation.metrics import (
    classification_metrics,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    roc_auc,
)


def _tx(rows):
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    for c in ("sender_account_id", "receiver_account_id", "external_counterparty", "merchant_id", "device_id",
              "country", "channel", "transaction_type"):
        if c not in df:
            df[c] = None
    df["status"] = df.get("status", "completed")
    return df


class MetricsTest(unittest.TestCase):
    def test_classification(self):
        m = classification_metrics([True, True, False, False], [True, False, True, False])
        self.assertEqual((m["tp"], m["fp"], m["fn"], m["tn"]), (1, 1, 1, 1))
        self.assertAlmostEqual(m["precision"], 0.5)
        self.assertAlmostEqual(m["recall"], 0.5)
        self.assertAlmostEqual(m["false_positive_rate"], 0.5)

    def test_degenerate_classification(self):
        m = classification_metrics([False, False], [False, False])
        self.assertEqual(m["precision"], 0.0)
        self.assertEqual(m["false_positive_rate"], 0.0)

    def test_ranking(self):
        rel = [False, True, True]
        self.assertAlmostEqual(precision_at_k(rel, 2), 0.5)
        self.assertAlmostEqual(recall_at_k(rel, 3, 2), 1.0)
        self.assertAlmostEqual(reciprocal_rank(rel), 0.5)
        self.assertEqual(reciprocal_rank([False]), 0.0)

    def test_auc(self):
        self.assertEqual(roc_auc([True, False], [0.9, 0.1]), 1.0)
        self.assertEqual(roc_auc([True, False], [0.1, 0.9]), 0.0)
        self.assertEqual(roc_auc([True, False], [0.5, 0.5]), 0.5)


class GeoTest(unittest.TestCase):
    def test_haversine_lagos_london(self):
        d = float(haversine_km(6.5244, 3.3792, 51.5072, -0.1276))
        self.assertTrue(4950 < d < 5050, d)

    def test_impossible_travel_physical_only(self):
        tx = _tx([
            {"transaction_id": "T1", "timestamp": "2026-09-01T10:00Z", "latitude": 6.52, "longitude": 3.38, "channel": "pos"},
            {"transaction_id": "T2", "timestamp": "2026-09-01T11:00Z", "latitude": 51.5, "longitude": -0.13, "channel": "pos"},
            {"transaction_id": "T3", "timestamp": "2026-09-01T11:05Z", "latitude": 25.2, "longitude": 55.3, "channel": "web"},
        ])
        v = detect_impossible_travel(tx)
        self.assertEqual(len(v), 1)
        self.assertEqual((v[0].from_txn, v[0].to_txn), ("T1", "T2"))
        self.assertGreater(v[0].speed_kmh, 900)
        self.assertEqual(len(detect_impossible_travel(tx, physical_only=False)), 2)

    def test_plausible_travel_not_flagged(self):
        tx = _tx([
            {"transaction_id": "T1", "timestamp": "2026-09-01T00:00Z", "latitude": 6.52, "longitude": 3.38, "channel": "pos"},
            {"transaction_id": "T2", "timestamp": "2026-09-01T12:00Z", "latitude": 51.5, "longitude": -0.13, "channel": "pos"},
        ])
        self.assertEqual(detect_impossible_travel(tx), [])

    def test_near_location(self):
        tx = _tx([{"transaction_id": "A", "timestamp": "2026-09-01", "latitude": 6.52, "longitude": 3.38},
                  {"transaction_id": "B", "timestamp": "2026-09-01", "latitude": 9.07, "longitude": 7.40}])
        near = find_transactions_near_location(tx, 6.5, 3.4, 50)
        self.assertEqual(list(near.transaction_id), ["A"])


class StatsTest(unittest.TestCase):
    def test_max_count_in_window(self):
        ts = pd.Series(pd.to_datetime(["2026-09-01T10:00Z", "2026-09-01T10:20Z", "2026-09-01T10:50Z",
                                       "2026-09-01T12:00Z"], utc=True))
        n, at = max_count_in_window(ts, 60)
        self.assertEqual(n, 3)
        self.assertEqual(at, pd.Timestamp("2026-09-01T10:00Z"))
        self.assertEqual(max_count_in_window(pd.Series([], dtype="datetime64[ns, UTC]"), 60)[0], 0)

    def test_pass_through(self):
        inbound = _tx([{"transaction_id": "I1", "timestamp": "2026-09-01T10:00Z", "amount_usd": 100.0},
                       {"transaction_id": "I2", "timestamp": "2026-09-02T10:00Z", "amount_usd": 100.0}])
        outbound = _tx([{"transaction_id": "O1", "timestamp": "2026-09-01T12:00Z", "amount_usd": 90.0},
                        {"transaction_id": "O2", "timestamp": "2026-09-05T12:00Z", "amount_usd": 100.0}])
        r = pass_through(inbound, outbound, 24)
        self.assertAlmostEqual(r["ratio"], 0.45)
        self.assertIn(("I1", "O1"), r["pairs"])

    def test_split_frames(self):
        tx = _tx([{"transaction_id": "A", "timestamp": "2026-09-01", "sender_account_id": "X", "receiver_account_id": "Y"},
                  {"transaction_id": "B", "timestamp": "2026-09-01", "sender_account_id": "Z", "receiver_account_id": "X"},
                  {"transaction_id": "C", "timestamp": "2026-09-01", "sender_account_id": "X", "receiver_account_id": "X2"}])
        f = split_customer_frame(tx, {"X", "X2"})
        self.assertEqual(list(f.outbound.transaction_id), ["A"])
        self.assertEqual(list(f.inbound.transaction_id), ["B"])
        self.assertEqual(list(f.internal.transaction_id), ["C"])

    def test_jsd_bounds(self):
        self.assertAlmostEqual(jensen_shannon({"a": 1.0}, {"a": 1.0}), 0.0, places=6)
        self.assertAlmostEqual(jensen_shannon({"a": 1.0}, {"b": 1.0}), 1.0, places=3)
        self.assertTrue(0 < jensen_shannon({"a": 0.5, "b": 0.5}, {"a": 0.9, "b": 0.1}) < 1)
        self.assertTrue(np.isfinite(jensen_shannon({}, {})))


if __name__ == "__main__":
    unittest.main()
