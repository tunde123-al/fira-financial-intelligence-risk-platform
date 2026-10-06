"""Operational alert-quality metrics: only valid denominators, honest about what cannot be computed."""
import unittest
from datetime import datetime, timedelta, timezone

from app.monitoring import quality as ql
from app.monitoring.models import MonitoringAlert
from app.monitoring.repository import AlertFilter
from app.security.principal import Principal
from tests import support
from tests.unit.test_monitoring_alerts import make_users

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)
ADMIN = Principal("U-admin", "admin")
ALICE = Principal("U-alice", "analyst")


def alert(i, status="NEW", resolution=None, priority="HIGH", detector="FAN_IN", hours_to_resolve=5, triggered=NOW):
    return MonitoringAlert(
        alert_id=f"MAL-{i:012X}", customer_id=f"CUST-{i}", detector_id=detector, alert_type=detector, category="x",
        severity="high", risk_score=50, risk_contribution=10, status=status, description="d", triggered_at=triggered,
        created_at=triggered, updated_at=triggered, last_seen_at=triggered, resolution=resolution,
        resolution_reason="because" if resolution else None, resolved_by="U-alice" if resolution else None,
        resolved_at=triggered + timedelta(hours=hours_to_resolve) if resolution else None, triage_priority=priority,
        triage_score=60.0)


class QualityMathTest(unittest.TestCase):
    def setUp(self):
        self.alerts = ([alert(i, "RESOLVED", "CONFIRMED_SUSPICIOUS") for i in range(1, 4)]
                       + [alert(i, "RESOLVED", "CLEARED") for i in range(4, 6)]
                       + [alert(6, "RESOLVED", "FALSE_POSITIVE")]
                       + [alert(i) for i in range(7, 11)])

    def test_rates_use_decided_alerts_as_denominator(self):
        o = ql.alert_quality(self.alerts)["overall"]
        self.assertEqual((o["alerts"], o["decided"], o["open"]), (10, 6, 4))
        self.assertEqual((o["confirmed"], o["cleared"], o["false_positive"]), (3, 2, 1))
        self.assertEqual(o["confirmation_rate"], 0.5)
        self.assertEqual(o["false_discovery_rate"], 0.5)
        self.assertEqual(o["closure_rate"], 0.6)
        self.assertAlmostEqual(o["confirmation_rate"] + o["false_discovery_rate"], 1.0)
        self.assertTrue(o["low_sample"])

    def test_no_fpr_or_recall_is_reported(self):
        q = ql.alert_quality(self.alerts)
        flat = str(q["overall"]).lower() + str(q["by_priority"]).lower()
        self.assertNotIn("false_positive_rate", flat)
        self.assertNotIn("recall", flat)
        self.assertIn("false_positive_rate", q["not_computed"])
        self.assertIn("recall", q["not_computed"])

    def test_undefined_rates_are_null_not_zero(self):
        o = ql.alert_quality([alert(1), alert(2)])["overall"]
        self.assertIsNone(o["confirmation_rate"])
        self.assertIsNone(o["false_discovery_rate"])
        self.assertEqual(o["closure_rate"], 0.0)
        empty = ql.alert_quality([])["overall"]
        self.assertIsNone(empty["closure_rate"])

    def test_breakdowns_and_time_to_decision(self):
        a = self.alerts + [alert(20, "RESOLVED", "CONFIRMED_SUSPICIOUS", priority="CRITICAL", detector="STRUCTURING",
                                 hours_to_resolve=1)]
        q = ql.alert_quality(a)
        self.assertEqual(q["by_priority"]["CRITICAL"]["confirmation_rate"], 1.0)
        self.assertEqual(list(q["by_priority"])[0], "CRITICAL")  # ordered by severity of priority
        self.assertEqual(q["by_detector"]["STRUCTURING"]["decided"], 1)
        self.assertEqual(q["time_to_decision_hours"]["n"], 7)
        self.assertEqual(q["time_to_decision_hours"]["median"], 5.0)

    def test_window_filters_by_trigger_time(self):
        old = alert(30, "RESOLVED", "CLEARED", triggered=NOW - timedelta(days=90))
        q = ql.alert_quality(self.alerts + [old], since=NOW - timedelta(days=30))
        self.assertEqual(q["overall"]["alerts"], 10)

    def test_feedback_rows_record_decision_reason_investigator_and_time(self):
        rows = ql.feedback_rows(self.alerts)
        self.assertEqual(len(rows), 6)
        r = rows[0]
        for k in ("alert_id", "decision", "reason", "investigator", "decided_at"):
            self.assertIsNotNone(r[k])
        self.assertTrue(all(x["decision"] in ("CLEARED", "FALSE_POSITIVE", "CONFIRMED_SUSPICIOUS") for x in rows))


class QualityServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = support.container(fresh=True)
        make_users(cls.c.store)
        cls.mon, cls.repo = cls.c.monitoring, cls.c.monitoring_repo
        cls.end = cls.c.store.as_of()
        cls.mule = support.first_of("mule_account")
        cls.mon.run(window_end=cls.end, customer_ids=[cls.mule], actor=ADMIN)

    def test_outcome_flow_is_recorded_without_changing_detection(self):
        items, _ = self.repo.list_alerts(AlertFilter(customer_id=self.mule, limit=50))
        self.assertGreaterEqual(len(items), 2)
        fp = items[0]
        self.mon.assign_alert(fp.alert_id, "U-alice", ADMIN)
        self.mon.transition_alert(fp.alert_id, "RESOLVED", ALICE, "benign recurring payments", "FALSE_POSITIVE")
        cfg_before = self.c.monitoring_config.fingerprint()
        rows = self.mon.feedback()
        mine = next(r for r in rows if r["alert_id"] == fp.alert_id)
        self.assertEqual((mine["decision"], mine["investigator"], mine["reason"]),
                         ("FALSE_POSITIVE", "U-alice", "benign recurring payments"))
        self.assertIsNotNone(mine["decided_at"])
        q = self.mon.alert_quality()
        self.assertGreaterEqual(q["overall"]["false_positive"], 1)
        self.assertEqual(self.c.monitoring_config.fingerprint(), cfg_before)  # no automatic threshold change

    def test_my_work_groups_by_priority_and_is_scoped_to_the_caller(self):
        items, _ = self.repo.list_alerts(AlertFilter(customer_id=self.mule, statuses=["NEW"], limit=50))
        if not items:
            self.skipTest("no open alert")
        self.mon.assign_alert(items[0].alert_id, "U-alice", ADMIN)
        w = self.mon.my_work(ALICE)
        ids = {a["alert_id"] for a in w["open_alerts"]}
        self.assertIn(items[0].alert_id, ids)
        scores = [a["triage_score"] for a in w["open_alerts"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(w["counts"]["open"], len(w["open_alerts"]))
        self.assertEqual(self.mon.my_work(Principal("U-bob", "analyst"))["counts"]["open"], 0)
        self.assertIn("assumptions", w["overdue_note"])
        self.assertEqual(w["counts"]["overdue"], len(w["overdue"]))


if __name__ == "__main__":
    unittest.main()
