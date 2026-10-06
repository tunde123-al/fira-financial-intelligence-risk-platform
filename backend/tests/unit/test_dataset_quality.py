"""Dataset data-quality audit: each check is proven with injected faults; nothing is hard-coded."""
import unittest
from datetime import datetime, timedelta, timezone

import pandas as pd
from fastapi.testclient import TestClient

from app.data.quality import TXN_CHECKS, audit
from app.main import create_app
from tests import support

AS_OF = datetime(2026, 1, 15, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 20, tzinfo=timezone.utc)
CUST = pd.DataFrame({"customer_id": ["C1", "C2"], "country": ["NG", "GB"], "segment": ["mass", "sme"]})
ACC = pd.DataFrame({"account_id": ["ACC-1", "ACC-2"], "customer_id": ["C1", "C2"], "status": ["active", "active"]})
MER = pd.DataFrame({"merchant_id": ["MER-1"]})
DEV = pd.DataFrame({"device_id": ["DEV-1"]})


def row(i, **kw):
    base = {"transaction_id": f"TXN-{i}", "timestamp": AS_OF - timedelta(hours=i), "sender_account_id": "ACC-1",
            "receiver_account_id": "ACC-2", "merchant_id": None, "device_id": None, "amount": 10.0 + i, "currency": "USD",
            "transaction_type": "transfer", "channel": "web", "status": "completed"}
    base.update(kw)
    return base


def run(rows, **kw):
    return audit(kw.get("cust", CUST), kw.get("acc", ACC), pd.DataFrame(rows), MER, DEV, AS_OF, NOW)


def count(r, name):
    return r["checks"][name]["count"]


class AuditChecksTest(unittest.TestCase):
    def test_clean_data_scores_100_and_every_check_is_defined(self):
        r = run([row(i) for i in range(1, 6)])
        self.assertEqual((r["records_processed"], r["valid"], r["with_issues"], r["quality_score"]), (5, 5, 0, 100.0))
        self.assertEqual(set(r["checks"]), set(TXN_CHECKS))
        self.assertTrue(all(v["count"] == 0 and v["description"] for v in r["checks"].values()))

    def test_each_fault_is_counted_by_its_own_check(self):
        rows = [row(1), row(2),
                row(3, transaction_id=None),                        # missing id
                row(4, transaction_id="TXN-2"),                     # duplicate of row 2's id
                row(5, amount=-4), row(6, amount=0), row(7, amount=float("nan")),   # invalid amounts (3)
                row(8, currency="ZZZ"),                             # invalid currency
                row(9, timestamp=None),                             # invalid timestamp
                row(10, timestamp=AS_OF + timedelta(days=5)),       # future
                row(11, sender_account_id=None, receiver_account_id=None),   # no account
                row(12, sender_account_id="ACC-999"),               # orphan sender
                row(13, receiver_account_id="ACC-998"),             # orphan receiver
                row(14, merchant_id="MER-999"), row(15, device_id="DEV-999"),   # orphan merchant / device
                row(16, status="weird"),                            # impossible state
                row(17, transaction_type="deposit", receiver_account_id=None),   # deposit needs a receiver
                row(18, channel=None)]                              # incomplete
        r = run(rows)
        expect = {"missing_transaction_id": 1, "duplicate_transaction_id": 1, "invalid_amount": 3, "invalid_currency": 1,
                  "invalid_timestamp": 1, "future_timestamp": 1, "no_account": 1, "orphan_sender_account": 1,
                  "orphan_receiver_account": 1, "orphan_merchant": 1, "orphan_device": 1, "incomplete_record": 1}
        for k, v in expect.items():
            self.assertEqual(count(r, k), v, k)
        self.assertGreaterEqual(count(r, "impossible_state"), 2)  # bad status, deposit without receiver (and no-account rows)
        self.assertEqual(r["valid"], r["records_processed"] - r["with_issues"])
        self.assertAlmostEqual(r["quality_score"], round(100 * r["valid"] / r["records_processed"], 2))
        self.assertLess(r["quality_score"], 100)

    def test_likely_duplicates_need_same_parties_amount_and_a_close_time(self):
        t = AS_OF - timedelta(hours=3)
        rows = [row(1, timestamp=t, amount=50), row(2, timestamp=t + timedelta(seconds=20), amount=50),   # likely duplicate
                row(3, timestamp=t + timedelta(minutes=10), amount=50),                                   # same parties, too late
                row(4, timestamp=t + timedelta(seconds=25), amount=51)]                                   # different amount
        r = run(rows)
        self.assertEqual(count(r, "likely_duplicate"), 1)

    def test_other_tables_and_freshness_are_computed(self):
        cust = pd.concat([CUST, pd.DataFrame({"customer_id": ["C1", "C3"], "country": [None, "KE"], "segment": ["x", "y"]})], ignore_index=True)
        acc = pd.concat([ACC, pd.DataFrame({"account_id": ["ACC-3"], "customer_id": ["GHOST"], "status": [""]})], ignore_index=True)
        r = run([row(1)], cust=cust, acc=acc)
        self.assertEqual(r["other_tables"]["customers"], {"records": 4, "duplicate_ids": 1, "incomplete_records": 1})
        self.assertEqual(r["other_tables"]["accounts"], {"records": 3, "orphan_customer_references": 1, "missing_status": 1})
        self.assertEqual(r["freshness"]["dataset_age_days"], 5.0)
        self.assertIsNotNone(r["freshness"]["latest_transaction"])

    def test_empty_transaction_table_has_no_score_instead_of_a_fake_100(self):
        r = audit(CUST, ACC, pd.DataFrame(columns=list(row(1))), MER, DEV, AS_OF, NOW)
        self.assertEqual((r["records_processed"], r["quality_score"]), (0, None))


class StoreAndApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = support.container(fresh=True)
        cls.c.settings.bootstrap_analyst_password = "analyst-password-123"
        cls.c.settings.rate_limit_per_minute = 100_000
        cls.client = TestClient(create_app(cls.c))
        cls.client.__enter__()
        tok = cls.client.post("/api/auth/login", json={"username": "analyst", "password": "analyst-password-123"}).json()["access_token"]
        cls.h = {"Authorization": f"Bearer {tok}"}

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_counts_come_from_the_store_and_a_seeded_fault_is_detected(self):
        n = self.c.store.count_transactions(None, None)
        before = self.c.store.dataset_quality()
        self.assertEqual(before["records_processed"], n)
        tx = self.c.store.tx
        self.c.store.tx = pd.concat([tx, tx.iloc[[0]].assign(sender_account_id="ACC-99999999")], ignore_index=True)  # orphan + duplicate id
        try:
            after = self.c.store.dataset_quality()
        finally:
            self.c.store.tx = tx
        self.assertEqual(after["records_processed"], n + 1)
        self.assertEqual(count(after, "orphan_sender_account"), count(before, "orphan_sender_account") + 1)
        self.assertEqual(count(after, "duplicate_transaction_id"), count(before, "duplicate_transaction_id") + 1)
        self.assertLess(after["quality_score"], before["quality_score"] + 1e-9)

    def test_endpoint_requires_auth_and_returns_the_audit(self):
        self.assertEqual(self.client.get("/api/data-quality/dataset").status_code, 401)
        d = self.client.get("/api/data-quality/dataset", headers=self.h).json()
        self.assertEqual(d["records_processed"], self.c.store.count_transactions(None, None))
        self.assertIn("quality_score", d)
        self.assertEqual(set(d["checks"]), set(TXN_CHECKS))


if __name__ == "__main__":
    unittest.main()
