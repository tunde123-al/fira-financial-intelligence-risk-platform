"""Data-quality gate: validation reason codes, batch accounting, quarantine, coverage and failure handling."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import pandas as pd

from app.monitoring.ingest import Lookups, QualityRules, code_group, missing_ids, validate_batch
from app.security.principal import Principal
from app.synthetic import reference as ref
from tests import support

ADMIN = Principal("U-admin", "admin")
NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


def lookups(existing=(), statuses=None, recent=None, latest=None):
    owners = {"ACC-1": "CUST-1", "ACC-2": "CUST-2", "ACC-3": "CUST-3"}
    statuses = statuses or {}
    return Lookups(
        owners=lambda ids: {a: owners[a] for a in ids if a in owners},
        account_status=lambda ids: {a: statuses.get(a, "active") for a in ids if a in owners},
        known_devices=lambda ids: {i for i in ids if i == "DEV-1"},
        known_merchants=lambda ids: {i for i in ids if i == "MER-1"},
        existing_ids=lambda ids: {i for i in ids if i in set(existing)},
        recent_transactions=lambda accs, a, b: recent if recent is not None else pd.DataFrame(),
        latest_timestamp=lambda: latest, reference_time=lambda: NOW)


def row(**kw):
    base = {"transaction_id": "TXN-1", "timestamp": (NOW - timedelta(hours=1)).isoformat(),
            "sender_account_id": "ACC-1", "receiver_account_id": "ACC-2", "amount": 100.0, "currency": "USD",
            "transaction_type": "transfer", "channel": "web"}
    base.update(kw)
    return base


def codes(res):
    return [r["code"] for r in res.rejected]


class ValidatorRulesTest(unittest.TestCase):
    def check(self, expected_code, **kw):
        res = validate_batch([row(**kw)], lookups())
        self.assertEqual(codes(res), [expected_code], res.rejected)
        self.assertEqual(len(res.accepted), 0)

    def test_valid_row_is_accepted_with_usd_amount_filled(self):
        res = validate_batch([row()], lookups())
        self.assertEqual((len(res.accepted), res.rejected), (1, []))
        self.assertAlmostEqual(float(res.accepted["amount_usd"].iloc[0]), 100.0)

    def test_malformed_codes(self):
        self.check("MISSING_FIELD", amount=None)
        self.check("INVALID_TIMESTAMP", timestamp="yesterday-ish")
        self.check("INVALID_AMOUNT", amount="abc")
        self.check("INVALID_AMOUNT", amount=float("inf"))
        self.check("INVALID_CURRENCY", currency="US")
        self.check("INVALID_IP", ip_address="999.1.1.1")
        self.check("INVALID_STATUS", status="weird")
        self.check("INVALID_ID_FORMAT", transaction_id="; DROP TABLE x")
        self.check("MALFORMED_ROW", surprise=1)
        self.assertEqual(codes(validate_batch(["not an object"], lookups())), ["MALFORMED_ROW"])

    def test_invalid_value_codes(self):
        self.check("NON_POSITIVE_AMOUNT", amount=0)
        self.check("NON_POSITIVE_AMOUNT", amount=-3)
        self.check("UNSUPPORTED_CURRENCY", currency="ZZZ")
        self.check("INVALID_TYPE", transaction_type="teleport")
        self.check("INVALID_CHANNEL", channel="fax")
        self.check("TIMESTAMP_OUT_OF_RANGE", timestamp=(NOW + timedelta(days=3)).isoformat())
        self.check("TIMESTAMP_OUT_OF_RANGE", timestamp=(NOW - timedelta(days=4000)).isoformat())
        self.check("NO_ACCOUNT", sender_account_id=None, receiver_account_id=None)
        self.check("DIRECTION_MISMATCH", transaction_type="deposit", receiver_account_id=None)
        self.check("DIRECTION_MISMATCH", transaction_type="cash_withdrawal", sender_account_id=None)

    def test_referential_codes(self):
        self.check("UNKNOWN_ACCOUNT", sender_account_id="ACC-404")
        self.check("UNKNOWN_DEVICE", device_id="DEV-404")
        self.check("UNKNOWN_MERCHANT", merchant_id="MER-404", transaction_type="card_payment",
                   receiver_account_id=None)
        res = validate_batch([row(device_id="DEV-1")], lookups())
        self.assertEqual(len(res.accepted), 1)

    def test_closed_account_rejected_unless_disabled(self):
        lk = lookups(statuses={"ACC-1": "closed"})
        self.assertEqual(codes(validate_batch([row()], lk)), ["ACCOUNT_CLOSED"])
        res = validate_batch([row()], lk, QualityRules(reject_closed_accounts=False))
        self.assertEqual(len(res.accepted), 1)

    def test_duplicates(self):
        res = validate_batch([row(), row(), row(transaction_id="TXN-2", amount=5)], lookups())
        self.assertEqual((len(res.accepted), codes(res)), (2, ["DUPLICATE_ID"]))
        res = validate_batch([row()], lookups(existing=["TXN-1"]))
        self.assertEqual(codes(res), ["DUPLICATE_ID"])

    def test_likely_duplicate_same_parties_amount_within_window(self):
        recent = pd.DataFrame([{"sender_account_id": "ACC-1", "receiver_account_id": "ACC-2", "amount": 100.0,
                                "currency": "USD", "timestamp": pd.Timestamp(NOW - timedelta(hours=1, seconds=20))}])
        res = validate_batch([row(transaction_id="TXN-9")], lookups(recent=recent))
        self.assertEqual(codes(res), ["LIKELY_DUPLICATE"])
        res = validate_batch([row(transaction_id="TXN-9")], lookups(recent=recent),
                             QualityRules(duplicate_policy="accept"))
        self.assertEqual(len(res.accepted), 1)
        res = validate_batch([row(transaction_id="TXN-9", amount=101)], lookups(recent=recent))
        self.assertEqual(len(res.accepted), 1)

    def test_late_arrival_is_accepted_and_flagged(self):
        res = validate_batch([row(timestamp=(NOW - timedelta(days=3)).isoformat())], lookups(latest=NOW))
        self.assertEqual((len(res.accepted), res.late_ids), (1, ["TXN-1"]))
        res = validate_batch([row()], lookups(latest=NOW))
        self.assertEqual(res.late_ids, [])

    def test_missing_transaction_id_generated_or_required(self):
        res = validate_batch([row(transaction_id=None)], lookups())
        self.assertEqual((len(res.accepted), res.ids_generated), (1, 1))
        res = validate_batch([row(transaction_id=None)], lookups(), require_transaction_id=True)
        self.assertEqual(codes(res), ["MISSING_TRANSACTION_ID"])

    def test_rejected_payload_masks_ip_and_every_code_has_a_group(self):
        res = validate_batch([row(ip_address="203.0.113.7", amount=-1)], lookups())
        self.assertEqual(res.rejected[0]["payload"]["ip_address"], "203.0.x.x")
        self.assertEqual({code_group(c) for c in res.reason_counts},
                         {"invalid"})
        for c in ("MALFORMED_ROW", "DUPLICATE_ID", "UNKNOWN_ACCOUNT", "ACCOUNT_CLOSED"):
            self.assertIn(code_group(c), {"malformed", "duplicate", "invalid", "referential"})

    def test_batch_size_limit(self):
        with self.assertRaises(ValueError):
            validate_batch([row()] * 50_001, lookups())

    def test_missing_ids_from_count_ids_and_sequence(self):
        none = lambda ids: set()  # noqa: E731
        self.assertEqual(missing_ids(None, set(), none, 0), (None, []))
        self.assertEqual(missing_ids({"count": 10}, set(), none, 7), (3, []))
        self.assertEqual(missing_ids({"count": 3}, set(), none, 7), (0, []))
        self.assertEqual(missing_ids({"ids": ["TXN-1", "TXN-2", "TXN-3"]}, {"TXN-1"}, none, 1), (2, ["TXN-2", "TXN-3"]))
        self.assertEqual(missing_ids({"ids": ["TXN-1", "TXN-2"]}, set(), lambda ids: {"TXN-2"}, 0), (1, ["TXN-1"]))
        n, sample = missing_ids({"sequence": {"prefix": "TXN-", "start": 1, "end": 5}}, {"TXN-1", "TXN-2"}, none, 2)
        self.assertEqual((n, sample), (3, ["TXN-3", "TXN-4", "TXN-5"]))


class BatchAccountingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = support.container(fresh=True)
        cls.store = cls.c.store
        cls.end = cls.store.as_of()
        labels = {lb["entity_id"]: lb["scenario"] for lb in support.labels()}
        cls.pool = [c for c, s in labels.items() if s == "normal"]
        cls.used = 0

    def rows(self, n, base_id, usd=50.0, start_hours=5):
        cid = self.pool[type(self).used]
        type(self).used += 1
        acc = self.store.accounts_for_customer(cid)[0]
        return [{"transaction_id": f"TXN-{base_id + i}", "timestamp": (self.end - timedelta(hours=start_hours - i)).isoformat(),
                 "sender_account_id": acc.account_id, "amount": round((usd + 37 * i) * ref.FX_PER_USD[acc.currency], 2),
                 "currency": acc.currency, "amount_usd": usd + 37 * i, "transaction_type": "transfer", "channel": "web",
                 "external_counterparty": f"EXT-DQ{base_id}"} for i in range(n)]

    def test_mixed_batch_accounting_is_consistent_and_quarantined(self):
        rows = self.rows(4, 888100)
        rows[1]["amount"] = -1
        rows[2]["sender_account_id"] = "ACC-99999999"
        rows.append({"timestamp": "garbage"})
        out = self.c.monitoring.ingest(rows, ADMIN, run_monitoring=False, source="unit-test")
        b = out["batch"]
        self.assertEqual((b["received"], b["processed"], b["rejected"], b["failed"]), (5, 2, 3, 0))
        self.assertEqual(b["received"], b["processed"] + b["rejected"] + b["failed"])
        self.assertEqual(b["malformed"], 1)
        persisted = self.c.monitoring_repo.get_batch(b["batch_id"])
        self.assertEqual(persisted.received, 5)
        listing = self.c.monitoring.list_rejected(b["batch_id"], None, None, 50, 0)
        self.assertEqual(listing["total"], 3)
        self.assertEqual({r["reason_code"] for r in listing["items"]},
                         {"NON_POSITIVE_AMOUNT", "UNKNOWN_ACCOUNT", "MISSING_FIELD"})
        only = self.c.monitoring.list_rejected(b["batch_id"], "UNKNOWN_ACCOUNT", None, 50, 0)
        self.assertEqual(only["total"], 1)

    def test_expected_count_gives_missing_and_coverage(self):
        rows = self.rows(3, 888200)
        before = self.c.monitoring.data_quality_summary()["totals"]
        out = self.c.monitoring.ingest(rows, ADMIN, run_monitoring=False, expected={"count": 5})
        self.assertEqual((out["batch"]["expected_count"], out["batch"]["missing"]), (5, 2))
        after = self.c.monitoring.data_quality_summary()["totals"]
        self.assertEqual(after["expected"], (before["expected"] or 0) + 5)
        self.assertEqual(after["coverage"], round(after["received"] / after["expected"], 6)
                         if before["batches_with_expected"] == 0 else after["coverage"])

    def test_sequence_expectation_counts_gaps(self):
        rows = self.rows(3, 888300)
        out = self.c.monitoring.ingest(rows, ADMIN, run_monitoring=False,
                                       expected={"sequence": {"prefix": "TXN-", "start": 888300, "end": 888304}})
        self.assertEqual(out["batch"]["missing"], 2)
        self.assertEqual(out["batch"]["details"]["missing_sample"], ["TXN-888303", "TXN-888304"])

    def test_no_expectation_means_no_coverage_figure(self):
        b = self.c.monitoring.register_seed_batch("seed-without-manifest", None, 100, 100)
        self.assertIsNone(b.expected_count)
        self.assertIsNone(b.missing)

    def test_late_rows_are_counted(self):
        rows = self.rows(2, 888400, start_hours=24 * 20)  # 20 days older than the newest stored transaction
        out = self.c.monitoring.ingest(rows, ADMIN, run_monitoring=False)
        self.assertEqual((out["batch"]["processed"], out["batch"]["late"]), (2, 2))

    def test_storage_failure_is_recorded_and_surfaced(self):
        rows = self.rows(2, 888500)
        with mock.patch.object(self.store, "ingest_transactions", side_effect=OSError("disk full")):
            with self.assertRaises(RuntimeError):
                self.c.monitoring.ingest(rows, ADMIN, run_monitoring=False)
        b = self.c.monitoring_repo.list_batches(limit=1)[0][0]
        self.assertEqual((b.status, b.failed, b.processed, b.received), ("FAILED", 2, 0, 2))
        self.assertNotIn("disk full", b.error or "")  # no internal message stored
        self.assertIsNone(self.store.get_transaction("TXN-888500"))

    def test_summary_uses_real_batch_values(self):
        s = self.c.monitoring.data_quality_summary()
        t = s["totals"]
        self.assertEqual(t["received"], t["processed"] + t["rejected"] + t["failed"])
        self.assertAlmostEqual(t["processing_success"], t["processed"] / t["received"], places=5)
        self.assertGreater(t["batches"], 0)
        self.assertIn("definitions", s)


if __name__ == "__main__":
    unittest.main()
