"""Failure modes against a real PostgreSQL: unreachable database, killed connections, restart, schema behind, bad migration.

Each test uses its own throwaway database next to DATABASE_URL. Skipped unless DATABASE_URL points at PostgreSQL.
"""
import importlib.util
import os
import time
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.monitoring.repository import AlertFilter
from app.security.principal import Principal
from tests import support

URL = os.environ.get("DATABASE_URL")
HAVE_PG = bool(URL) and importlib.util.find_spec("psycopg") is not None
DB_DIR = Path(__file__).resolve().parents[2] / "app" / "db"
ADMIN = Principal("U-admin", "admin")


def _admin_engine(url):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    return create_engine(make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT")


class _ScratchDb:
    files = ("schema.sql", "schema_audit_guard.sql", "schema_monitoring.sql", "schema_production.sql")
    revision = "0004"

    @classmethod
    def create(cls, load_data=True):
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url

        from app.db.loader import load

        name = f"fira_fail_{uuid.uuid4().hex[:10]}"
        with _admin_engine(URL).connect() as conn:
            conn.execute(text(f"CREATE DATABASE {name}"))  # noqa: S608
        url = make_url(URL).set(database=name).render_as_string(hide_password=False)
        eng = create_engine(url)
        with eng.begin() as conn:
            for f in cls.files:
                conn.exec_driver_sql((DB_DIR / f).read_text(encoding="utf-8"))
            conn.exec_driver_sql("CREATE TABLE alembic_version (version_num varchar(32) NOT NULL PRIMARY KEY)")
            conn.exec_driver_sql(f"INSERT INTO alembic_version VALUES ('{cls.revision}')")
        eng.dispose()
        if load_data:
            load(url, support.small_dataset(), truncate=False)
        return name, url

    @staticmethod
    def drop(name):
        from sqlalchemy import text

        with _admin_engine(URL).connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))  # noqa: S608


def container_for(url):
    from app.services.container import build_container

    return build_container(support.make_settings(data_backend="postgres", database_url=url), load_ml=False)


@unittest.skipUnless(HAVE_PG, "DATABASE_URL is not a PostgreSQL URL")
class UnreachableDatabaseTest(unittest.TestCase):
    def test_startup_fails_fast_with_a_clear_error_and_never_prints_the_password(self):
        bad = "postgresql+psycopg://fira:super-secret-pw@127.0.0.1:9/fira"
        t0 = time.perf_counter()
        with self.assertRaises(Exception) as cm:
            container_for(bad)
        self.assertLess(time.perf_counter() - t0, 30)
        self.assertNotIn("super-secret-pw", str(cm.exception))


@unittest.skipUnless(HAVE_PG, "DATABASE_URL is not a PostgreSQL URL")
class ConnectionLossAndRestartTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name, cls.url = _ScratchDb.create()

    @classmethod
    def tearDownClass(cls):
        _ScratchDb.drop(cls.name)

    def test_01_pool_recovers_after_every_connection_is_killed(self):
        from sqlalchemy import text

        c = container_for(self.url)
        self.assertTrue(c.store.ping())
        with _admin_engine(URL).connect() as conn:  # what a database restart or failover does to open connections
            conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :d AND pid <> pg_backend_pid()"),
                         {"d": self.name})
        self.assertTrue(c.store.ping())  # pool_pre_ping discards the dead connections
        self.assertTrue(c.monitoring_repo.ping())
        c.store.engine.dispose()

    def test_02_restart_keeps_state_and_does_not_duplicate_baselines(self):
        first = container_for(self.url)
        ids = [lb["entity_id"] for lb in support.labels() if lb["scenario"] in ("mule_account", "circular_transfer")]
        first.monitoring.run(window_end=first.store.as_of(), customer_ids=ids, actor=ADMIN)
        alerts_before = first.monitoring_repo.list_alerts(AlertFilter(limit=1))[1]
        batches_before = first.monitoring_repo.list_batches(limit=100)[1]
        changes_before = len(first.monitoring_repo.list_config_changes(500))
        first.store.engine.dispose()
        again = container_for(self.url)  # the "restart"
        self.assertEqual(again.monitoring_repo.list_alerts(AlertFilter(limit=1))[1], alerts_before)
        self.assertGreater(alerts_before, 0)
        self.assertEqual(again.monitoring_repo.list_batches(limit=100)[1], batches_before)  # baseline batch not repeated
        self.assertEqual(len(again.monitoring_repo.list_config_changes(500)), changes_before)  # no phantom config changes
        rerun = again.monitoring.run(window_end=again.store.as_of(), customer_ids=ids, actor=ADMIN)
        self.assertEqual(rerun.alerts_created, 0)  # merged into the stored unresolved alerts, not duplicated
        again.store.engine.dispose()

    def test_03_readiness_over_http_reports_the_schema_revision(self):
        c = container_for(self.url)
        c.settings.rate_limit_per_minute = 100_000
        with TestClient(create_app(c)) as client:
            d = client.get("/health/ready").json()
            self.assertEqual((d["status"], d["checks"]["schema_revision"], d["checks"]["schema_current"]),
                             ("ready", "0004", True))
        c.store.engine.dispose()


@unittest.skipUnless(HAVE_PG, "DATABASE_URL is not a PostgreSQL URL")
class SchemaBehindTest(unittest.TestCase):
    def test_a_database_that_missed_a_migration_is_reported_not_ready(self):
        from sqlalchemy import create_engine

        name, url = _ScratchDb.create()
        try:
            eng = create_engine(url)
            with eng.begin() as conn:
                conn.exec_driver_sql("UPDATE alembic_version SET version_num = '0003'")  # as if 0004 had not been applied
            eng.dispose()
            c = container_for(url)
            with TestClient(create_app(c)) as client:
                r = client.get("/health/ready")
                self.assertEqual(r.status_code, 503)
                self.assertFalse(r.json()["checks"]["schema_current"])
                self.assertEqual(r.json()["checks"]["schema_revision"], "0003")
                self.assertEqual(client.get("/health").status_code, 200)  # liveness is unaffected
            c.store.engine.dispose()
        finally:
            _ScratchDb.drop(name)


@unittest.skipUnless(HAVE_PG, "DATABASE_URL is not a PostgreSQL URL")
class BadMigrationTest(unittest.TestCase):
    def test_a_failing_migration_rolls_back_completely(self):
        """Migration 0004 runs in one transaction (alembic transaction_per_migration): a failure leaves nothing behind."""
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url

        name = f"fira_mig_{uuid.uuid4().hex[:10]}"
        with _admin_engine(URL).connect() as conn:
            conn.execute(text(f"CREATE DATABASE {name}"))  # noqa: S608
        url = make_url(URL).set(database=name).render_as_string(hide_password=False)
        try:
            eng = create_engine(url)
            with eng.begin() as conn:
                for f in ("schema.sql", "schema_audit_guard.sql", "schema_monitoring.sql"):
                    conn.exec_driver_sql((DB_DIR / f).read_text(encoding="utf-8"))
            sql = (DB_DIR / "schema_production.sql").read_text(encoding="utf-8") + "\nSELECT 1/0;"
            with self.assertRaises(Exception), eng.begin() as conn:
                conn.exec_driver_sql(sql)
            with eng.connect() as conn:
                tables = {r[0] for r in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
                cols = {r[0] for r in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'monitoring_alerts'"))}
            self.assertFalse({"ingestion_batches", "rejected_transactions", "config_change_log"} & tables)
            self.assertNotIn("triage_score", cols)
            # and the migration is repeatable: applying it cleanly afterwards works
            with eng.begin() as conn:
                conn.exec_driver_sql((DB_DIR / "schema_production.sql").read_text(encoding="utf-8"))
                conn.exec_driver_sql((DB_DIR / "schema_production.sql").read_text(encoding="utf-8"))  # idempotent
            eng.dispose()
        finally:
            _ScratchDb.drop(name)


if __name__ == "__main__":
    unittest.main()
