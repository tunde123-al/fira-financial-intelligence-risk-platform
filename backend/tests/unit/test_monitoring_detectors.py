"""Detector behaviour for the monitoring upgrade: STRUCTURING, FAN_OUT, ingestion validation."""
import unittest
from datetime import timedelta

import pandas as pd

from app.monitoring.detectors import detector_results
from app.security.principal import Principal
from app.synthetic import reference as ref
from tests import support

ADMIN = Principal("U-admin", "admin")


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = support.container(fresh=True)
        cls.store = cls.c.store
        cls.end = cls.store.as_of()
        # normal customers that have a single account and are not part of any injected scenario
        labels = {lb["entity_id"]: lb["scenario"] for lb in support.labels()}
        cls.pool = [c for c, s in labels.items() if s == "normal"]
        cls._used = 0

    @classmethod
    def customer(cls):
        cid = cls.pool[cls._used]
        cls._used += 1
        return cid

    def rows(self, cid, usds, start, step_hours=2.0, ttype="transfer", counterparties=None):
        acc = self.store.accounts_for_customer(cid)[0]
        cur = acc.currency
        out = []
        for i, usd in enumerate(usds):
            out.append({
                "timestamp": start + timedelta(hours=i * step_hours), "sender_account_id": acc.account_id,
                "amount": round(usd * ref.FX_PER_USD[cur], 2), "currency": cur, "amount_usd": float(usd),
                "transaction_type": ttype, "channel": "web", "country": self.store.get_customer(cid).country,
                "status": "completed",
                "external_counterparty": (counterparties[i] if counterparties else "EXT-TEST-1"),
            })
        return out

    def ingest(self, rows):
        res = self.c.monitoring.ingest(rows, ADMIN, run_monitoring=False)
        self.assertEqual(res["rejected_count"], 0, res["rejected"])
        return res

    def assess(self, cid):
        return self.c.risk_engine.assess_customer(cid, self.end, 30, 90)


class StructuringTest(_Base):
    def test_triggers_on_three_near_threshold_transactions_in_24h(self):
        cid = self.customer()
        self.ingest(self.rows(cid, [9000, 9200, 9100], self.end - timedelta(hours=10)))
        a = self.assess(cid)
        self.assertIn("STRUCTURING", a.signal_types())
        sig = next(s for s in a.signals if s.signal_type == "STRUCTURING")
        self.assertEqual(sig.observed_value, 3)
        self.assertEqual(len([e for e in sig.evidence if e.kind == "transaction"]), 3)
        res = [r for r in detector_results(a) if r.detector_id == "STRUCTURING"][0]
        self.assertTrue(res.triggered)
        self.assertEqual(len(res.transaction_ids), 3)
        self.assertGreater(res.risk_contribution, 0)

    def test_two_transactions_do_not_trigger(self):
        cid = self.customer()
        self.ingest(self.rows(cid, [9000, 9200], self.end - timedelta(hours=10)))
        self.assertNotIn("STRUCTURING", self.assess(cid).signal_types())

    def test_amounts_at_or_above_the_threshold_do_not_count(self):
        cid = self.customer()
        self.ingest(self.rows(cid, [10000, 10500, 12000], self.end - timedelta(hours=10)))
        self.assertNotIn("STRUCTURING", self.assess(cid).signal_types())

    def test_lower_bound_is_inclusive_and_below_it_is_ignored(self):
        inside, below = self.customer(), self.customer()
        self.ingest(self.rows(inside, [8000, 8000, 8000], self.end - timedelta(hours=10)))
        self.ingest(self.rows(below, [7999, 7999, 7999, 7999], self.end - timedelta(hours=10)))
        self.assertIn("STRUCTURING", self.assess(inside).signal_types())
        self.assertNotIn("STRUCTURING", self.assess(below).signal_types())

    def test_transactions_outside_the_window_do_not_trigger(self):
        cid = self.customer()
        # 3 transactions spread over 3 days: never 3 within any 24 hours
        self.ingest(self.rows(cid, [9000, 9000, 9000], self.end - timedelta(hours=70), step_hours=30))
        self.assertNotIn("STRUCTURING", self.assess(cid).signal_types())

    def test_total_must_reach_the_threshold(self):
        cid = self.customer()
        # min_total_ratio=1.0 of USD 10,000: three transactions of USD 8,000 sum to 24,000 -> fires; so
        # check the configured knob by raising it above the achievable total
        cfg = self.c.risk_config.model_copy(deep=True)
        cfg.signals["STRUCTURING"].params["min_total_ratio"] = 5.0
        from app.risk.engine import RiskEngine

        self.ingest(self.rows(cid, [8000, 8000, 8000], self.end - timedelta(hours=10)))
        eng = RiskEngine(self.store, self.c.graph, cfg)
        self.assertNotIn("STRUCTURING", eng.assess_customer(cid, self.end, 30, 90).signal_types())

    def test_disabled_detector_is_not_evaluated(self):
        cid = self.customer()
        cfg = self.c.risk_config.model_copy(deep=True)
        cfg.signals["STRUCTURING"].enabled = False
        from app.risk.engine import RiskEngine

        self.ingest(self.rows(cid, [9000, 9000, 9000], self.end - timedelta(hours=10)))
        a = RiskEngine(self.store, self.c.graph, cfg).assess_customer(cid, self.end, 30, 90)
        self.assertNotIn("STRUCTURING", a.signal_types())
        self.assertIn("STRUCTURING", [n.signal_type for n in a.not_evaluated])


class FanOutTest(_Base):
    def customer(self):  # type: ignore[override]
        """Next customer with no transfers to other beneficiaries in the window (natural data would add to the count)."""
        while True:
            cid = type(self).pool[type(self)._used]
            type(self)._used += 1
            if self.assess(cid).metrics.get("FAN_OUT", {}).get("observed", 0) == 0:
                return cid

    def test_many_distinct_beneficiaries_trigger(self):
        cid = self.customer()
        self.ingest(self.rows(cid, [50] * 9, self.end - timedelta(days=2), step_hours=1,
                              counterparties=[f"EXT-FAN-{i}" for i in range(9)]))
        a = self.assess(cid)
        self.assertIn("FAN_OUT", a.signal_types())
        self.assertEqual(next(s for s in a.signals if s.signal_type == "FAN_OUT").observed_value, 9)

    def test_few_beneficiaries_do_not_trigger(self):
        cid = self.customer()
        self.ingest(self.rows(cid, [50] * 5, self.end - timedelta(days=2), step_hours=1,
                              counterparties=[f"EXT-FEW-{i}" for i in range(5)]))
        self.assertNotIn("FAN_OUT", self.assess(cid).signal_types())

    def test_repeat_payments_to_one_beneficiary_do_not_trigger(self):
        cid = self.customer()
        self.ingest(self.rows(cid, [50] * 12, self.end - timedelta(days=2), step_hours=1))
        self.assertNotIn("FAN_OUT", self.assess(cid).signal_types())


class ScoreBreakdownTest(_Base):
    def test_category_breakdown_adds_up_to_the_score(self):
        cid = self.customer()
        self.ingest(self.rows(cid, [9000, 9100, 9200], self.end - timedelta(hours=10)))
        a = self.assess(cid)
        self.assertGreater(a.score, 0)
        self.assertAlmostEqual(sum(x.points for x in a.category_breakdown), a.score, delta=0.2)
        self.assertEqual(a.category_breakdown[0].category, "transaction_behaviour")

    def test_empty_window_yields_no_signals_and_results_stay_well_formed(self):
        cid = self.customer()
        a = self.c.risk_engine.assess_customer(cid, self.end - timedelta(days=400), 30, 90)
        self.assertEqual(a.signals, [])
        self.assertEqual(a.score, 0.0)
        res = detector_results(a)
        self.assertTrue(all(not r.triggered for r in res))
        self.assertTrue(any("not evaluated" in r.reason or r.reason in ("below threshold", "no data") for r in res))

    def test_detection_result_has_no_confidence_field(self):
        cid = self.customer()
        self.ingest(self.rows(cid, [9000, 9100, 9200], self.end - timedelta(hours=10)))
        res = detector_results(self.assess(cid))[0]
        self.assertNotIn("confidence", res.model_dump())
        self.assertIn("supporting_transactions", res.explain())


class IngestionValidationTest(_Base):
    def _try(self, row_overrides):
        cid = self.customer()
        base = self.rows(cid, [100], self.end - timedelta(hours=1))[0]
        base.update(row_overrides)
        return self.c.monitoring.ingest([base], ADMIN, run_monitoring=False)

    def test_rejects_non_positive_amount(self):
        r = self._try({"amount": -5})
        self.assertEqual((r["accepted"], r["rejected_count"]), (0, 1))
        self.assertIn("amount", r["rejected"][0]["reason"])

    def test_rejects_invalid_ip_address_but_accepts_valid_ones(self):
        r = self._try({"ip_address": "999.1.1.1; DROP TABLE x"})
        self.assertEqual((r["accepted"], r["rejected_count"]), (0, 1))
        self.assertIn("ip_address", r["rejected"][0]["reason"])
        self.assertEqual(self._try({"ip_address": "203.0.113.7"})["accepted"], 1)
        self.assertEqual(self._try({"ip_address": "2001:db8::1"})["accepted"], 1)

    def test_rejects_unknown_account(self):
        r = self._try({"sender_account_id": "ACC-99999999"})
        self.assertEqual(r["accepted"], 0)
        self.assertIn("unknown sender_account_id", r["rejected"][0]["reason"])

    def test_rejects_bad_currency_and_status_and_timestamp(self):
        self.assertEqual(self._try({"currency": "ZZZ"})["accepted"], 0)
        self.assertEqual(self._try({"status": "weird"})["accepted"], 0)
        self.assertEqual(self._try({"timestamp": "not-a-date"})["accepted"], 0)

    def test_rejects_duplicate_ids_and_accepts_valid_rows(self):
        cid = self.customer()
        rows = self.rows(cid, [100, 200], self.end - timedelta(hours=3))
        rows[0]["transaction_id"] = rows[1]["transaction_id"] = "TXN-777000001"
        r = self.c.monitoring.ingest(rows, ADMIN, run_monitoring=False)
        # the first occurrence is kept, the repeat inside the batch is quarantined
        self.assertEqual((r["accepted"], r["rejected_count"], r["rejected"][0]["code"]), (1, 1, "DUPLICATE_ID"))
        self.assertEqual(r["batch"]["duplicates"], 1)
        rows[1]["transaction_id"] = "TXN-777000002"
        ok = self.c.monitoring.ingest(rows, ADMIN, run_monitoring=False)
        self.assertEqual((ok["accepted"], ok["rejected"][0]["code"]), (1, "DUPLICATE_ID"))  # id 1 exists in the store
        again = self.c.monitoring.ingest(rows[:1], ADMIN, run_monitoring=False)
        self.assertEqual((again["accepted"], again["rejected_count"]), (0, 1))

    def test_empty_batch_is_an_error(self):
        with self.assertRaises(ValueError):
            self.c.monitoring.ingest([], ADMIN)

    def test_ingested_transaction_is_queryable(self):
        cid = self.customer()
        rows = self.rows(cid, [321.5], self.end - timedelta(hours=2))
        rows[0]["transaction_id"] = "TXN-777000099"
        self.c.monitoring.ingest(rows, ADMIN, run_monitoring=False)
        t = self.store.get_transaction("TXN-777000099")
        self.assertIsNotNone(t)
        self.assertAlmostEqual(t.amount_usd, 321.5)
        self.assertIsInstance(pd.Timestamp(t.timestamp), pd.Timestamp)


if __name__ == "__main__":
    unittest.main()
