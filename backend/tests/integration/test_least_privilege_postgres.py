"""The API works with a least-privilege role, and that role cannot change the schema or the append-only ledgers."""
import importlib.util
import os
import unittest
import uuid
from pathlib import Path

from app.monitoring.repository import AlertFilter
from app.security.principal import Principal
from tests import support
from tests.integration.test_failure_modes_postgres import _admin_engine, _ScratchDb, container_for

URL = os.environ.get("DATABASE_URL")
HAVE_PG = bool(URL) and importlib.util.find_spec("psycopg") is not None
SQL = Path(__file__).resolve().parents[3] / "infrastructure" / "sql" / "least_privilege_role.sql"
ADMIN = Principal("U-admin", "admin")


@unittest.skipUnless(HAVE_PG, "DATABASE_URL is not a PostgreSQL URL")
class LeastPrivilegeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url

        cls.name, owner_url = _ScratchDb.create()
        cls.role = f"fira_app_{uuid.uuid4().hex[:6]}"
        cls.pw = "least-priv-test-pw-" + uuid.uuid4().hex[:8]
        eng = create_engine(owner_url, isolation_level="AUTOCOMMIT")
        sql = SQL.read_text(encoding="utf-8").replace("fira_app", cls.role).replace(":app_password", f"'{cls.pw}'")
        with eng.connect() as conn:
            for stmt in _split(sql):
                conn.execute(text(stmt))
        eng.dispose()
        cls.url = make_url(owner_url).set(username=cls.role, password=cls.pw).render_as_string(hide_password=False)

    @classmethod
    def tearDownClass(cls):
        from sqlalchemy import text

        _ScratchDb.drop(cls.name)
        with _admin_engine(URL).connect() as conn:
            conn.execute(text(f"DROP ROLE IF EXISTS {cls.role}"))  # noqa: S608

    def test_01_the_application_workflow_runs_as_the_restricted_role(self):
        c = container_for(self.url)
        ids = [lb["entity_id"] for lb in support.labels() if lb["scenario"] in ("mule_account", "circular_transfer")]
        c.store.upsert_user({"user_id": "U-admin", "username": "admin", "password_hash": "x", "role": "admin", "active": True})
        run = c.monitoring.run(window_end=c.store.as_of(), customer_ids=ids, actor=ADMIN)
        self.assertGreater(run.alerts_created, 0)
        a = c.monitoring_repo.list_alerts(AlertFilter(limit=1))[0][0]
        c.monitoring.assign_alert(a.alert_id, "U-admin", ADMIN)
        c.monitoring.transition_alert(a.alert_id, "RESOLVED", ADMIN, "reviewed, benign pattern", "CLEARED")
        case, _ = c.monitoring.create_case(c.monitoring_repo.list_alerts(AlertFilter(statuses=["NEW"], limit=1))[0][0].customer_id, [], ADMIN) \
            if c.monitoring_repo.list_alerts(AlertFilter(statuses=["NEW"], limit=1))[1] else (None, None)
        self.assertIsNotNone(c.monitoring.data_quality_summary()["totals"])
        c.store.engine.dispose()

    def test_02_ddl_truncate_and_ledger_changes_are_refused(self):
        from sqlalchemy import create_engine, text

        eng = create_engine(self.url, isolation_level="AUTOCOMMIT")
        denied = ["CREATE TABLE evil (x int)", "DROP TABLE customers", "ALTER TABLE customers ADD COLUMN x int",
                  "TRUNCATE audit_log", "TRUNCATE transactions", "DELETE FROM customers",
                  "UPDATE audit_log SET result = 'x'", "DELETE FROM audit_log", "UPDATE config_change_log SET reason = 'x'",
                  "DELETE FROM ingestion_batches", "UPDATE alembic_version SET version_num = '9999'", "CREATE ROLE sneaky"]
        with eng.connect() as conn:
            for stmt in denied:
                with self.assertRaises(Exception, msg=stmt) as cm:
                    conn.execute(text(stmt))
                # 42501 = insufficient_privilege (the append-only triggers raise the same code for UPDATE/DELETE)
                self.assertEqual(getattr(cm.exception.orig, "sqlstate", None), "42501", stmt)
            self.assertEqual(conn.execute(text("SELECT count(*) FROM customers")).scalar() > 0, True)
        eng.dispose()

    def test_03_role_has_no_superuser_or_ddl_attributes(self):
        from sqlalchemy import text

        with _admin_engine(URL).connect() as conn:
            r = conn.execute(text("SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = :r"), {"r": self.role}).one()
        self.assertEqual(tuple(r), (False, False, False))


def _split(sql: str) -> list[str]:
    """Split a psql script on statement boundaries; DO blocks keep their $$ bodies intact."""
    out, buf, in_dollar = [], [], False
    for line in sql.splitlines():
        if line.strip().startswith("--") or not line.strip():
            continue
        buf.append(line)
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar
        if not in_dollar and line.rstrip().endswith(";"):
            out.append("\n".join(buf))
            buf = []
    return out


if __name__ == "__main__":
    unittest.main()
