"""The monitoring workflow tests, re-run against PostgreSQL (SqlStore + SqlMonitoringRepo).

Each test class gets a fresh, throwaway database created next to DATABASE_URL, so nothing is shared with
other suites. Skipped unless DATABASE_URL points at a PostgreSQL server.
"""
import importlib.util
import os
import unittest
import uuid
from pathlib import Path

from app.monitoring.repository import AlertFilter
from tests import support
from tests.unit.test_monitoring_alerts import MonitoringFlowTest, make_users

HAS = lambda m: importlib.util.find_spec(m) is not None  # noqa: E731
URL = os.environ.get("DATABASE_URL")
DB_DIR = Path(__file__).resolve().parents[2] / "app" / "db"


def _admin_engine(url):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    return create_engine(make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT")


class _PgMixin:
    """Creates a throwaway database with revisions 0001-0004 applied and a container on top of it."""

    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url

        from app.db.loader import load
        from app.services.container import build_container

        cls.dbname = f"fira_it_{uuid.uuid4().hex[:10]}"
        with _admin_engine(URL).connect() as conn:
            conn.execute(text(f"CREATE DATABASE {cls.dbname}"))  # noqa: S608
        cls.url = make_url(URL).set(database=cls.dbname).render_as_string(hide_password=False)
        eng = create_engine(cls.url)
        with eng.begin() as conn:
            for f in ("schema.sql", "schema_audit_guard.sql", "schema_monitoring.sql",
                      "schema_production.sql"):
                conn.exec_driver_sql((DB_DIR / f).read_text(encoding="utf-8"))
        eng.dispose()
        load(cls.url, support.small_dataset(), truncate=False)
        settings = support.make_settings(data_backend="postgres", database_url=cls.url)
        cls.c = build_container(settings, load_ml=False)
        make_users(cls.c.store)
        cls.mon = cls.c.monitoring
        cls.repo = cls.c.monitoring_repo
        cls.end = cls.c.store.as_of()
        cls.mule = support.first_of("mule_account")

    @classmethod
    def tearDownClass(cls):
        from sqlalchemy import text

        cls.c.store.engine.dispose()
        with _admin_engine(URL).connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {cls.dbname} WITH (FORCE)"))  # noqa: S608


@unittest.skipUnless(URL and HAS("sqlalchemy") and HAS("psycopg"), "PostgreSQL not configured")
class PostgresMonitoringFlowTest(_PgMixin, MonitoringFlowTest):
    """Same assertions as the in-memory flow, executed against real constraints, indexes and triggers."""


@unittest.skipUnless(URL and HAS("sqlalchemy") and HAS("psycopg"), "PostgreSQL not configured")
class MonitoringSchemaConstraintsTest(_PgMixin, unittest.TestCase):
    """Database-level guarantees that do not depend on the application code."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mon.run(window_end=cls.end, customer_ids=[cls.mule], actor=None)

    def _raises(self, sql, **params):
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError

        with self.assertRaises(DBAPIError) as cm:
            with self.c.store.engine.begin() as conn:
                conn.execute(text(sql), params)
        return str(cm.exception)

    def _alert(self):
        items, _ = self.repo.list_alerts(AlertFilter(customer_id=self.mule, limit=5))
        return items[0]

    def _case(self):
        """An unclosed case for the mule customer plus one alert that belongs to it."""
        from app.security.principal import Principal

        a = self._alert()
        case = self.repo.find_open_case(a.customer_id)
        if case is None:
            case, _ = self.mon.create_case(a.customer_id, [a.alert_id], Principal("U-alice", "analyst"))
        return case, self.repo.alerts_for_case(case.case_id)[0]

    def test_ingestion_stores_inet_and_rejects_bad_ips_without_failing_the_batch(self):
        from datetime import timedelta

        from app.security.principal import Principal
        from app.synthetic.reference import FX_PER_USD

        acc = self.c.store.accounts_for_customer(self.mule)[0]
        base = {"sender_account_id": acc.account_id, "currency": acc.currency, "transaction_type": "transfer",
                "channel": "web", "external_counterparty": "EXT-IT", "timestamp": self.end - timedelta(hours=2),
                "amount": round(50 * FX_PER_USD[acc.currency], 2), "amount_usd": 50.0}
        rows = [dict(base, transaction_id="TXN-888000001", ip_address="203.0.113.7"),
                dict(base, transaction_id="TXN-888000002", ip_address="not-an-ip")]
        out = self.mon.ingest(rows, Principal("U-admin", "admin"), run_monitoring=False)
        self.assertEqual((out["accepted"], out["rejected_count"]), (1, 1))
        self.assertEqual(self.c.store.get_transaction("TXN-888000001").ip_address, "203.0.113.7")
        self.assertIsNone(self.c.store.get_transaction("TXN-888000002"))

    def test_the_indexes_the_alert_queue_relies_on_exist(self):
        from sqlalchemy import text

        with self.c.store.engine.connect() as conn:
            names = {r[0] for r in conn.execute(text(
                "SELECT indexname FROM pg_indexes WHERE tablename IN ('monitoring_alerts', 'cases', "
                "'monitoring_alert_transactions', 'case_notes', 'case_events', 'monitoring_alert_events')"))}
        for expected in ("ux_alert_unresolved_per_detector", "ix_malerts_status_triggered", "ix_malerts_severity",
                         "ix_malerts_detector", "ix_malerts_customer", "ix_malerts_assigned", "ix_malerts_risk",
                         "ix_malerts_case", "ux_one_open_case_per_customer", "ix_cases_status", "ix_cn_case",
                         "ix_ce_case", "ix_mae_alert", "ix_mat_txn"):
            self.assertIn(expected, names)

    def test_unresolved_alert_per_customer_and_detector_is_unique(self):
        a = self._alert()
        msg = self._raises(
            "INSERT INTO monitoring_alerts (alert_id, customer_id, detector_id, alert_type, category, severity, "
            "risk_score, risk_contribution, status, description, triggered_at, created_at, updated_at, last_seen_at) "
            "VALUES ('MAL-ABCDEF123456', :c, :d, 'x', 'x', 'low', 1, 1, 'NEW', 'dup', now(), now(), now(), now())",
            c=a.customer_id, d=a.detector_id)
        self.assertIn("ux_alert_unresolved_per_detector", msg)

    def test_resolution_is_only_valid_when_resolved(self):
        a = self._alert()
        self.assertIn("monitoring_alerts_check", self._raises(
            "UPDATE monitoring_alerts SET resolution = 'CLEARED' WHERE alert_id = :a", a=a.alert_id))
        self.assertIn("check", self._raises(
            "UPDATE monitoring_alerts SET status = 'RESOLVED' WHERE alert_id = :a", a=a.alert_id).lower())
        self.assertIn("check", self._raises(
            "UPDATE monitoring_alerts SET resolution = 'ESCALATED', status = 'RESOLVED', resolved_at = now() "
            "WHERE alert_id = :a", a=a.alert_id).lower())

    def test_invalid_status_and_severity_are_rejected(self):
        a = self._alert()
        self.assertIn("check", self._raises("UPDATE monitoring_alerts SET status = 'DONE' WHERE alert_id = :a",
                                            a=a.alert_id).lower())
        self.assertIn("check", self._raises("UPDATE monitoring_alerts SET severity = 'urgent' WHERE alert_id = :a",
                                            a=a.alert_id).lower())

    def test_alert_transactions_have_foreign_keys(self):
        a = self._alert()
        self.assertIn("foreign key", self._raises(
            "INSERT INTO monitoring_alert_transactions (alert_id, transaction_id) VALUES (:a, 'TXN-NOPE')",
            a=a.alert_id).lower())

    def test_history_tables_are_append_only(self):
        a = self._alert()
        self.assertIn("append-only", self._raises(
            "UPDATE monitoring_alert_events SET actor = 'tampered' WHERE alert_id = :a", a=a.alert_id))
        self.assertIn("append-only", self._raises(
            "DELETE FROM monitoring_alert_events WHERE alert_id = :a", a=a.alert_id))

    def test_case_notes_evidence_and_events_are_append_only(self):
        from app.security.principal import Principal

        alice = Principal("U-alice", "analyst")
        case, a = self._case()
        note = self.mon.add_note(case.case_id, "a note that must never change", alice)
        self.assertIn("append-only", self._raises("UPDATE case_notes SET body = 'x' WHERE note_id = :n", n=note.note_id))
        self.assertIn("append-only", self._raises("DELETE FROM case_notes WHERE note_id = :n", n=note.note_id))
        self.assertIn("append-only", self._raises("DELETE FROM case_events WHERE case_id = :c", c=case.case_id))
        self.mon.add_evidence(case.case_id, "alert", a.alert_id, alice)
        self.assertIn("append-only", self._raises("DELETE FROM case_evidence WHERE case_id = :c", c=case.case_id))

    def test_one_unclosed_case_per_customer_is_enforced_by_the_database(self):
        case, _ = self._case()
        msg = self._raises(
            "INSERT INTO cases (case_id, case_number, customer_id, status, priority, title, opened_at, created_at, "
            "updated_at) VALUES ('CASE-ABCDEF123456', 'FC-2026-999999', :c, 'OPEN', 'low', 't', now(), now(), now())",
            c=case.customer_id)
        self.assertIn("ux_one_open_case_per_customer", msg)

    def test_a_closed_case_needs_a_closing_decision(self):
        case, _ = self._case()
        self.assertIn("check", self._raises(
            "UPDATE cases SET status = 'CLOSED', closed_at = now() WHERE case_id = :c", c=case.case_id).lower())


@unittest.skipUnless(URL and HAS("sqlalchemy") and HAS("psycopg"), "PostgreSQL not configured")
class MonitoringAtomicityTest(_PgMixin, unittest.TestCase):
    """A workflow operation commits as a unit; a failure part-way leaves no half-applied state or audit entry."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mon.run(window_end=cls.end, customer_ids=[cls.mule], actor=None)

    def _investigating_case(self):
        from app.security.principal import Principal

        alice = Principal("U-alice", "analyst")
        a = self.repo.list_alerts(AlertFilter(customer_id=self.mule, limit=1))[0][0]
        case = self.repo.find_open_case(self.mule)
        if case is None:
            case, _ = self.mon.create_case(self.mule, [a.alert_id], alice)
        if case.status == "OPEN":
            self.mon.transition_case(case.case_id, "INVESTIGATING", alice)
        return alice, self.repo.get_case(case.case_id)

    def test_failed_decision_rolls_everything_back_and_leaves_no_audit_entry(self):
        alice, case = self._investigating_case()
        alerts_before = {x.alert_id: x.status for x in self.repo.alerts_for_case(case.case_id)}
        audit_before = len([e for e in self.c.store.list_audit(limit=5000) if e.entity_id == case.case_id])

        original = self.repo.add_case_event

        def boom(ev):
            if ev.event_type == "decision_recorded":
                raise RuntimeError("simulated failure after the case row was updated")
            return original(ev)

        self.repo.add_case_event = boom
        try:
            with self.assertRaises(RuntimeError):
                self.mon.decide_case(case.case_id, "CLEARED", "this decision must not persist", alice)
        finally:
            self.repo.add_case_event = original
        after = self.repo.get_case(case.case_id)
        self.assertEqual((after.status, after.decision, after.closed_at), (case.status, case.decision, None))
        self.assertEqual({x.alert_id: x.status for x in self.repo.alerts_for_case(case.case_id)}, alerts_before)
        audit_after = len([e for e in self.c.store.list_audit(limit=5000) if e.entity_id == case.case_id])
        self.assertEqual(audit_after, audit_before)  # nothing recorded for the rolled-back decision
        # and the operation still works afterwards
        out = self.mon.decide_case(case.case_id, "CLEARED", "decision after the failed attempt", alice)
        self.assertEqual(out["case"].status, "CLOSED")

    def test_failed_alert_creation_leaves_no_orphan_rows(self):
        from app.data.store import utcnow
        from app.monitoring.models import MonitoringAlert

        now = utcnow()
        alert = MonitoringAlert(alert_id="MAL-ABCDEF000001", customer_id=self.mule, detector_id="TEST_ATOMIC",
                                alert_type="TEST", category="x", severity="low", risk_score=1, risk_contribution=1,
                                description="d", triggered_at=now, created_at=now, updated_at=now, last_seen_at=now,
                                transaction_ids=["TXN-DOES-NOT-EXIST"])
        with self.assertRaises(Exception):  # foreign key on the evidence link fails after the alert row is inserted
            with self.repo.transaction():
                self.repo.create_alert(alert)
        self.assertIsNone(self.repo.get_alert("MAL-ABCDEF000001"))


class DataQualitySchemaTest(_PgMixin, unittest.TestCase):
    """Batch ledger, quarantine and config log: round trips, constraints and append-only triggers."""

    def _batch(self, **kw):
        from app.data.store import new_id, utcnow
        from app.monitoring.models import IngestionBatch

        now = utcnow()
        base = dict(batch_id=new_id("BAT"), source="it", started_at=now, finished_at=now, received=5, processed=3,
                    rejected=2, duplicates=1, malformed=1, expected_count=6, missing=1)
        base.update(kw)
        return IngestionBatch(**base)

    def test_batch_and_rejected_rows_round_trip_and_totals(self):
        from app.data.store import utcnow
        from app.monitoring.models import RejectedRow

        before = self.repo.quality_totals()  # the container already recorded the dataset as a baseline batch
        b = self._batch()
        rej = [RejectedRow(batch_id=b.batch_id, row_number=i, reason_code=c, reason_group=g, reason="r",
                           payload={"ip_address": "10.1.x.x"}, created_at=utcnow())
               for i, (c, g) in enumerate([("DUPLICATE_ID", "duplicate"), ("INVALID_AMOUNT", "malformed")])]
        self.repo.save_batch(b, rej)
        got = self.repo.get_batch(b.batch_id)
        self.assertEqual((got.received, got.processed, got.missing), (5, 3, 1))
        items, total = self.repo.list_rejected(batch_id=b.batch_id, reason_group="duplicate")
        self.assertEqual((total, items[0].reason_code, items[0].payload["ip_address"]), (1, "DUPLICATE_ID", "10.1.x.x"))
        t = self.repo.quality_totals()
        self.assertEqual((t["received"] - before["received"], t["processed"] - before["processed"]), (5, 3))
        self.assertEqual(t["expected"] - before["expected"], 6)
        self.assertEqual(t["coverage"], round(min((before["received"] + 5) / t["expected"], 1.0), 6))

    def test_accounting_check_constraint(self):
        from sqlalchemy.exc import IntegrityError

        with self.assertRaises(IntegrityError):
            self.repo.save_batch(self._batch(received=5, processed=3, rejected=3), [])

    def test_ledger_is_append_only(self):
        from sqlalchemy import text

        b = self._batch()
        self.repo.save_batch(b, [])
        for sql in ("UPDATE ingestion_batches SET received = 99 WHERE batch_id = :b",
                    "DELETE FROM ingestion_batches WHERE batch_id = :b"):
            with self.assertRaises(Exception) as cm, self.c.store.engine.begin() as conn:
                conn.execute(text(sql), {"b": b.batch_id})
            self.assertIn("ingestion_batches is append-only", str(cm.exception))
        self.assertEqual(self.repo.get_batch(b.batch_id).received, 5)

    def test_config_change_log_round_trip_and_snapshot(self):
        from app.data.store import utcnow
        from app.monitoring.models import ConfigChange

        self.repo.add_config_change(ConfigChange(ts=utcnow(), config_name="monitoring", path="alerting.x",
                                                 old_value=1, new_value=2, changed_by="U-admin", reason="t",
                                                 source="api"))
        self.repo.add_config_change(ConfigChange(ts=utcnow(), config_name="monitoring", path="(snapshot)",
                                                 new_value={"a": 1}, changed_by="system", source="startup"))
        self.assertEqual(self.repo.last_config_snapshot("monitoring"), {"a": 1})
        self.assertEqual(self.repo.list_config_changes(limit=5)[1].path, "alerting.x")

    def test_triage_columns_and_priority_filter(self):
        self.assertEqual(self.repo.list_alerts(AlertFilter(priorities=["CRITICAL"]))[1], 0)


if __name__ == "__main__":
    unittest.main()
