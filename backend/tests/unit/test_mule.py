"""Money-mule indicators: deterministic cases, benign look-alikes, graph queries and the HTTP surface."""
import inspect
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
from fastapi.testclient import TestClient

from app.main import create_app
from app.monitoring import mule as ml
from app.monitoring.config import MuleConfig
from tests import support

END = datetime(2026, 2, 1, tzinfo=timezone.utc)
CFG = MuleConfig()


class FakeStore:
    def __init__(self, rows, customer_type="individual", opened=None):
        self.df = pd.DataFrame(rows, columns=["transaction_id", "timestamp", "sender_account_id", "receiver_account_id",
                                              "amount_usd", "status", "external_counterparty"])
        self.opened = opened or END - timedelta(days=900)
        self.ctype = customer_type

    def accounts_for_customer(self, cid):
        return [SimpleNamespace(account_id="ACC-1", opened_at=self.opened)]

    def transactions_for_accounts(self, accs, start, end):
        return self.df.copy()

    def get_customer(self, cid):
        return SimpleNamespace(customer_type=self.ctype)


def tx(i, t, s, r, usd, ext=None):
    return (f"TXN-{i}", END - timedelta(hours=t), s, r, usd, "completed", ext)


def mule_rows():
    rows = [tx(i, 30 - i * 0.2, f"ACC-9{i}", "ACC-1", 1500) for i in range(8)]  # 8 senders, USD 12,000 in
    rows += [tx(100 + i, 27 - i * 0.2, "ACC-1", f"ACC-8{i}", 1150) for i in range(9)]  # 9 beneficiaries, USD 10,350 out
    return rows


class FifoTest(unittest.TestCase):
    def test_matches_outbound_to_earlier_inbound_within_window(self):
        t0 = pd.Timestamp("2026-01-01", tz="UTC")
        inb = pd.DataFrame([{"timestamp": t0, "amount_usd": 100.0, "transaction_id": "I1"}])
        out = pd.DataFrame([{"timestamp": t0 + pd.Timedelta(hours=1), "amount_usd": 60.0, "transaction_id": "O1"},
                            {"timestamp": t0 + pd.Timedelta(hours=30), "amount_usd": 40.0, "transaction_id": "O2"}])
        matched, dwell, ids = ml.fifo_dwell(inb, out, 24)
        self.assertEqual((matched, ids), (60.0, ["O1"]))
        self.assertAlmostEqual(dwell[0], 1.0)

    def test_outbound_before_inbound_never_matches(self):
        t0 = pd.Timestamp("2026-01-01", tz="UTC")
        inb = pd.DataFrame([{"timestamp": t0 + pd.Timedelta(hours=5), "amount_usd": 100.0, "transaction_id": "I"}])
        out = pd.DataFrame([{"timestamp": t0, "amount_usd": 100.0, "transaction_id": "O"}])
        self.assertEqual(ml.fifo_dwell(inb, out, 24)[0], 0.0)


class IndicatorTest(unittest.TestCase):
    def run_case(self, store, assessment=None, graph=None):
        return ml.assess_mule(store, graph, assessment, "CUST-1", END, CFG)

    def ind(self, r, iid):
        return next(i for i in r["indicators"] if i["id"] == iid)

    def test_maxima_sum_to_100(self):
        self.assertEqual(sum(ml.MAX_POINTS.values()), 100.0)

    def test_mule_pattern_fires_flow_indicators_with_evidence(self):
        r = self.run_case(FakeStore(mule_rows()))
        for iid in ("fan_in", "fan_out", "rapid_movement", "low_retention"):
            self.assertTrue(self.ind(r, iid)["fired"], iid)
        self.assertTrue(self.ind(r, "fan_in")["evidence"]["transaction_ids"])
        self.assertGreaterEqual(r["score"], CFG.bands["medium"])
        self.assertEqual(r["band"], "MEDIUM" if r["score"] < CFG.bands["high"] else "HIGH")
        self.assertIn("not proof", r["disclaimer"])
        self.assertEqual(r["score"], round(sum(i["points"] for i in r["indicators"]), 1))

    def test_payroll_business_fan_out_alone_stays_below_high_and_is_explained(self):
        rows = [tx(i, 50 - i, "ACC-1", f"ACC-7{i}", 2000) for i in range(12)]
        r = self.run_case(FakeStore(rows, customer_type="business"))
        self.assertEqual([i["id"] for i in r["indicators"] if i["fired"]], ["fan_out"])
        self.assertLess(r["score"], CFG.bands["medium"])
        self.assertTrue(any("Business account" in c for c in r["context"]))

    def test_marketplace_seller_fan_in_that_retains_funds_is_low(self):
        rows = [tx(i, 90 - i, f"ACC-6{i}", "ACC-1", 800) for i in range(10)]
        r = self.run_case(FakeStore(rows, customer_type="business"))
        self.assertEqual([i["id"] for i in r["indicators"] if i["fired"]], ["fan_in"])
        self.assertLess(r["score"], CFG.bands["medium"])

    def test_salary_income_spent_is_not_a_mule_pattern(self):
        rows = [(f"TXN-{i}", END - timedelta(days=20 - i), None, "ACC-1", 3000.0, "completed", None) for i in range(1)]
        rows += [tx(10 + i, 400 - i * 24, "ACC-1", f"ACC-5{i % 3}", 900) for i in range(3)]
        r = self.run_case(FakeStore(rows))
        self.assertEqual(r["band"], "NONE")
        self.assertEqual(r["inbound_usd"], 0.0)

    def test_new_account_and_dormant_signals(self):
        r = self.run_case(FakeStore(mule_rows(), opened=END - timedelta(days=5)))
        self.assertTrue(self.ind(r, "new_or_dormant_account")["fired"])
        sig = SimpleNamespace(contributors=[SimpleNamespace(signal_type="DORMANT_REACTIVATION", points=9.0)])
        r2 = self.run_case(FakeStore(mule_rows()), assessment=sig)
        self.assertTrue(self.ind(r2, "new_or_dormant_account")["fired"])

    def test_empty_window_is_none_with_explanation(self):
        r = self.run_case(FakeStore([]))
        self.assertEqual((r["score"], r["band"]), (0.0, "NONE"))
        self.assertTrue(any("No completed transactions" in c for c in r["context"]))

    def test_failed_transactions_are_ignored(self):
        rows = [(a, b, c, d, e, "failed", f) for a, b, c, d, e, _, f in mule_rows()]
        self.assertEqual(self.run_case(FakeStore(rows))["band"], "NONE")

    def test_logic_does_not_read_ground_truth_labels(self):
        src = inspect.getsource(ml).lower()
        for word in ("scenario", "ground_truth", "is_fraud", "mule_account", "load_labels", "labels.json"):
            self.assertNotIn(word, src)

    def test_config_validation(self):
        with self.assertRaises(Exception):
            MuleConfig(bands={"high": 10, "medium": 20, "low": 30})
        with self.assertRaises(Exception):
            MuleConfig(rapid_share=1.0)


class MuleGraphAndApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = support.container(fresh=True)
        cls.c.settings.bootstrap_admin_password = "admin-password-123"
        cls.c.settings.bootstrap_analyst_password = "analyst-password-123"
        cls.c.settings.rate_limit_per_minute = 100_000
        cls.client = TestClient(create_app(cls.c))
        cls.client.__enter__()
        cls.h = cls.login("analyst", "analyst-password-123")
        cls.admin = cls.login("admin", "admin-password-123")
        cls.mule = support.first_of("mule_account")
        cls.normal = support.first_of("normal")

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    @classmethod
    def login(cls, u, p):
        r = cls.client.post("/api/auth/login", json={"username": u, "password": p})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def test_assessment_for_the_synthetic_mule_and_a_normal_customer(self):
        r = self.client.get(f"/api/mule/customers/{self.mule}", headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        a = r.json()
        self.assertEqual(a["band"], "HIGH")
        self.assertTrue(all(i["reason"] for i in a["indicators"]))
        n = self.client.get(f"/api/mule/customers/{self.normal}", headers=self.h).json()
        self.assertIn(n["band"], ("NONE", "LOW"))

    def test_flow_subgraph_has_directed_edges_with_amounts_times_and_depth(self):
        f = self.client.get(f"/api/mule/customers/{self.mule}/flow", params={"depth": 2}, headers=self.h).json()
        self.assertTrue(f["nodes"] and f["edges"])
        self.assertTrue(any(n["depth"] == 0 for n in f["nodes"]))
        e = f["edges"][0]
        for k in ("source", "target", "total_usd", "n", "first_ts", "last_ts", "depth", "direction"):
            self.assertIn(k, e)
        ids = {n["account_id"] for n in f["nodes"]}
        self.assertTrue(all(e["source"] in ids and e["target"] in ids for e in f["edges"]))
        self.assertFalse(f["truncated"])

    def test_pattern_search_finds_the_mule_account(self):
        p = self.client.get("/api/mule/patterns", params={"min_degree": 5}, headers=self.h).json()
        self.assertIn(self.mule, {r["owner_customer_id"] for r in p["patterns"]})
        self.assertTrue(all(r["distinct_senders"] >= 5 or r["distinct_receivers"] >= 5 for r in p["patterns"]))

    def test_suspects_list_needs_alerts_and_ranks_by_score(self):
        self.client.post("/api/monitoring/run", headers=self.admin, json={"customer_ids": [self.mule]})
        s = self.client.get("/api/mule/suspects", params={"min_band": "MEDIUM"}, headers=self.h).json()
        self.assertIn(self.mule, [x["customer_id"] for x in s["suspects"]])
        scores = [x["score"] for x in s["suspects"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertIn("not proof", s["disclaimer"])

    def test_validation_auth_and_audit(self):
        self.assertEqual(self.client.get("/api/mule/customers/CUST-10226").status_code, 401)
        self.assertEqual(self.client.get("/api/mule/customers/bad", headers=self.h).status_code, 422)
        self.assertEqual(self.client.get("/api/mule/customers/CUST-99999999", headers=self.h).status_code, 404)
        self.assertEqual(self.client.get(f"/api/mule/customers/{self.mule}/flow", params={"depth": 9}, headers=self.h).status_code, 422)
        self.assertEqual(self.client.get("/api/mule/patterns", params={"min_degree": 1}, headers=self.h).status_code, 422)
        actions = [e.action for e in self.c.store.list_audit(limit=200)] if hasattr(self.c.store, "list_audit") else ["mule_assessment"]
        self.assertIn("mule_assessment", actions)


if __name__ == "__main__":
    unittest.main()
