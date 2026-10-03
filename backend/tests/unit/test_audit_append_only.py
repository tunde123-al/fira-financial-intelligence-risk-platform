"""Audit log append-only guarantees that can be checked without a database.

The database-level enforcement (triggers) is exercised by tests/integration/test_services.py::AuditAppendOnlyTest.
"""
import importlib.util
import re
import unittest
from pathlib import Path

from app.data.frame_store import FrameStore
from app.data.sql_store import SQL, SqlStore
from app.data.store import DataStore

DB_DIR = Path(__file__).resolve().parents[2] / "app" / "db"
MIGRATIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _audit_methods(cls) -> set[str]:
    return {n for n in dir(cls) if "audit" in n.lower() and not n.startswith("_")}


class AuditApplicationPathTest(unittest.TestCase):
    def test_store_interface_only_appends_and_lists(self):
        for cls in (DataStore, FrameStore, SqlStore):
            self.assertEqual(_audit_methods(cls), {"append_audit", "list_audit"}, cls.__name__)

    def test_sql_never_mutates_audit_log(self):
        for name, stmt in SQL.items():
            sql = stmt if isinstance(stmt, str) else stmt[0]
            self.assertNotRegex(sql, r"(?i)\b(update|delete\s+from|truncate)\b\s+audit_log", name)

    def test_frame_store_assigns_increasing_ids(self):
        from app.data.store import utcnow
        from app.schemas.domain import AuditEvent
        from tests import support

        store = FrameStore(support.small_dataset())
        for _ in range(3):
            store.append_audit(AuditEvent(ts=utcnow(), action="unit", result="ok"))
        ids = [e.id for e in reversed(store.list_audit(limit=10, action="unit"))]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 3)


class AuditGuardMigrationTest(unittest.TestCase):
    def test_guard_sql_covers_update_delete_and_truncate(self):
        sql = (DB_DIR / "schema_audit_guard.sql").read_text(encoding="utf-8")
        self.assertRegex(sql, r"BEFORE UPDATE OR DELETE ON audit_log")
        self.assertRegex(sql, r"BEFORE TRUNCATE ON audit_log")
        self.assertIn("append-only", sql)

    def test_revision_0002_chains_after_0001_and_applies_the_guard(self):
        path = next(MIGRATIONS.glob("0002_*.py"))
        spec = importlib.util.spec_from_file_location("rev0002", path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except ModuleNotFoundError:  # alembic is a runtime dependency; fall back to a source check
            src = path.read_text(encoding="utf-8")
            self.assertRegex(src, r'down_revision = "0001"')
            self.assertIn("schema_audit_guard.sql", src)
            return
        self.assertEqual((mod.revision, mod.down_revision), ("0002", "0001"))
        self.assertIn("schema_audit_guard.sql", re.sub(r"\s+", " ", path.read_text(encoding="utf-8")))
