import unittest

import pandas as pd

from app.graph.networkx_backend import NetworkXGraph, build_projection


def _edges():
    ts = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
    customers = pd.DataFrame([{"customer_id": f"CUST-{i}", "customer_type": "individual", "segment": "mass",
                               "risk_profile": "low", "country": "NG"} for i in range(1, 6)])
    ownership = pd.DataFrame([{"customer_id": f"CUST-{i}", "account_id": f"ACC-{i}"} for i in range(1, 6)])
    transfers = pd.DataFrame([
        {"sender_account_id": "ACC-1", "receiver_account_id": "ACC-2", "n": 1, "total_usd": 1000.0,
         "first_ts": ts("2026-09-01"), "last_ts": ts("2026-09-01"), "sample_txn_ids": ["T1"]},
        {"sender_account_id": "ACC-2", "receiver_account_id": "ACC-3", "n": 1, "total_usd": 980.0,
         "first_ts": ts("2026-09-02"), "last_ts": ts("2026-09-02"), "sample_txn_ids": ["T2"]},
        {"sender_account_id": "ACC-3", "receiver_account_id": "ACC-1", "n": 1, "total_usd": 960.0,
         "first_ts": ts("2026-09-03"), "last_ts": ts("2026-09-03"), "sample_txn_ids": ["T3"]},
        {"sender_account_id": "ACC-4", "receiver_account_id": "ACC-5", "n": 2, "total_usd": 50.0,
         "first_ts": ts("2026-01-01"), "last_ts": ts("2026-01-05"), "sample_txn_ids": ["T4"]},
    ])
    device_usage = pd.DataFrame([{"customer_id": c, "device_id": "DEV-9", "n": 3, "first_ts": ts("2026-09-01"),
                                  "last_ts": ts("2026-09-02")} for c in ("CUST-1", "CUST-2", "CUST-3")])
    return {"customers": customers, "merchants": pd.DataFrame(columns=["merchant_id", "name", "category",
                                                                         "risk_profile", "country"]),
            "ownership": ownership, "transfers": transfers, "device_usage": device_usage,
            "ip_usage": pd.DataFrame(columns=["customer_id", "ip_address", "n"]),
            "merchant_payments": pd.DataFrame(columns=["account_id", "merchant_id", "n", "total_usd"]),
            "identifiers": pd.DataFrame(columns=["customer_id", "identifier_type", "value_hash", "masked_value"]),
            "alerts": pd.DataFrame([{"entity_id": "CUST-3", "entity_type": "customer", "status": "open",
                                     "created_at": ts("2026-09-05")}])}


class GraphTest(unittest.TestCase):
    def setUp(self):
        self.g = NetworkXGraph(build_projection(_edges()))

    def test_cycle_detection_with_time_filter(self):
        self.assertEqual(self.g.candidate_cycles(["ACC-1"], 5), [["ACC-1", "ACC-2", "ACC-3"]])
        self.assertEqual(self.g.candidate_cycles(["ACC-1"], 2), [])
        self.assertEqual(self.g.candidate_cycles(["ACC-1"], 5, since=pd.Timestamp("2026-09-02T12:00Z")), [])

    def test_shared_devices(self):
        sd = self.g.shared_devices("CUST-1")
        self.assertEqual(sd[0].device_id, "DEV-9")
        self.assertEqual(sd[0].customers, ["CUST-1", "CUST-2", "CUST-3"])
        self.assertEqual(self.g.shared_devices("CUST-4"), [])

    def test_shortest_path(self):
        p = self.g.transaction_path("ACC-1", "ACC-3")
        self.assertTrue(p.found)
        self.assertEqual(p.hops, 2)
        self.assertFalse(self.g.transaction_path("ACC-1", "ACC-5").found)
        self.assertFalse(self.g.transaction_path("ACC-1", "ACC-1").found)

    def test_trace_funds_is_temporally_ordered(self):
        flows = self.g.trace_funds("ACC-1", "out", 3)
        paths = [tuple(f.path) for f in flows]
        self.assertIn(("ACC-1", "ACC-2", "ACC-3"), paths)
        # ACC-3 -> ACC-1 would revisit the origin and is excluded
        self.assertTrue(all(len(set(p)) == len(p) for p in paths))

    def test_connected_accounts_and_cluster(self):
        conn = {c.account_id: c for c in self.g.connected_accounts("ACC-1")}
        self.assertIn("ACC-2", conn)
        self.assertTrue(conn["ACC-2"].via.startswith("shared_device") or conn["ACC-2"].via == "transfers")
        cl = self.g.suspicious_cluster("CUST-1")
        self.assertEqual(cl.flagged_customers, ["CUST-3"])
        self.assertIn("DEV-9", cl.devices)
        self.assertGreater(cl.density, 0)

    def test_related_entities_and_flags(self):
        sg = self.g.related_entities("customer", "CUST-1", depth=2)
        ids = {n.id for n in sg.nodes}
        self.assertIn("device:DEV-9", ids)
        self.assertIn("customer:CUST-2", ids)
        self.assertEqual(self.g.customer_flags(["CUST-3", "CUST-1"]), {"CUST-3": True, "CUST-1": False})
        self.assertEqual(self.g.account_owner("ACC-2"), "CUST-2")
        self.assertEqual(self.g.counterparties(["ACC-1"]), ["ACC-2", "ACC-3"])


if __name__ == "__main__":
    unittest.main()
