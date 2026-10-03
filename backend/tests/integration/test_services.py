"""Integration tests against real services. Each test class is skipped unless the
corresponding service is configured (they all run in CI via docker services):

  DATABASE_URL  -> PostgreSQL (schema applied, synthetic data loaded by the test)
  NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD -> Neo4j
  QDRANT_URL    -> Qdrant
FastAPI/MCP tests need the corresponding Python packages.
"""
import importlib.util
import os
import unittest
import uuid

import numpy as np

from tests import support

HAS = lambda m: importlib.util.find_spec(m) is not None  # noqa: E731


@unittest.skipUnless(os.environ.get("DATABASE_URL") and HAS("sqlalchemy") and HAS("psycopg"), "PostgreSQL not configured")
class PostgresStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine, text

        from app.data.frame_store import FrameStore
        from app.data.sql_store import SqlStore
        from app.db.loader import load

        url = os.environ["DATABASE_URL"]
        eng = create_engine(url)
        schema = (support.make_settings().risk_config_path.parents[1] / "db" / "schema.sql").read_text(encoding="utf-8")
        with eng.begin() as conn:
            conn.exec_driver_sql(schema)
        load(url, support.small_dataset(), truncate=True)
        cls.sql = SqlStore(url)
        cls.frames = FrameStore(support.small_dataset())
        with eng.connect() as conn:
            cls.n_tx = conn.execute(text("SELECT count(*) FROM transactions")).scalar()

    def test_load_counts(self):
        self.assertEqual(self.n_tx, len(self.frames.tx))

    def test_reads_match_frame_store(self):
        cid = support.first_of("mule_account")
        self.assertEqual(self.sql.get_customer(cid).model_dump(), self.frames.get_customer(cid).model_dump())
        accs = [a.account_id for a in self.sql.accounts_for_customer(cid)]
        a = self.sql.transactions_for_accounts(accs, None, None)
        b = self.frames.transactions_for_accounts(accs, None, None)
        self.assertEqual(sorted(a.transaction_id), sorted(b.transaction_id))
        self.assertAlmostEqual(float(a.amount_usd.sum()), float(b.amount_usd.sum()), places=1)
        self.assertEqual(self.sql.as_of(), self.frames.as_of())

    def test_risk_engine_identical_on_both_stores(self):
        from app.graph.networkx_backend import NetworkXGraph
        from app.risk.engine import RiskEngine

        cfg = support.container().risk_config
        for scen in ("circular_transfer", "dormant_reactivation", "device_sharing_ring"):
            cid = support.first_of(scen)
            x = RiskEngine(self.sql, NetworkXGraph.from_store(self.sql), cfg).assess_customer(cid)
            y = RiskEngine(self.frames, NetworkXGraph.from_store(self.frames), cfg).assess_customer(cid)
            self.assertEqual(x.signal_types(), y.signal_types(), scen)
            self.assertAlmostEqual(x.score, y.score, places=1)

    def test_writes_round_trip(self):
        from app.data.store import utcnow
        from app.schemas.domain import AuditEvent, Investigation

        iid = f"INV-IT{uuid.uuid4().hex[:8].upper()}"
        self.sql.create_investigation(Investigation(investigation_id=iid, subject_id="CUST-10001",
                                                    subject_type="customer", status="open", created_at=utcnow(),
                                                    signals=["A"], report={"k": 1}))
        upd = self.sql.update_investigation(iid, status="pending_review", report={"k": 2}, signals=["A", "B"])
        self.assertEqual((upd.status, upd.report, upd.signals), ("pending_review", {"k": 2}, ["A", "B"]))
        self.sql.append_audit(AuditEvent(ts=utcnow(), action="it_test", result="ok", details={"x": 1}))
        self.assertEqual(self.sql.list_audit(limit=1, action="it_test")[0].details, {"x": 1})


@unittest.skipUnless(os.environ.get("DATABASE_URL") and HAS("sqlalchemy") and HAS("psycopg"), "PostgreSQL not configured")
class AuditAppendOnlyTest(unittest.TestCase):
    """audit_log rejects UPDATE/DELETE/TRUNCATE at the database level (trigger from revision 0002)."""

    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine

        db_dir = support.make_settings().risk_config_path.parents[1] / "db"
        cls.engine = create_engine(os.environ["DATABASE_URL"])
        with cls.engine.begin() as conn:
            conn.exec_driver_sql((db_dir / "schema.sql").read_text(encoding="utf-8"))
            conn.exec_driver_sql((db_dir / "schema_audit_guard.sql").read_text(encoding="utf-8"))
        cls.marker = f"it_append_only_{uuid.uuid4().hex[:8]}"

    def setUp(self):
        from app.data.sql_store import SqlStore
        from app.data.store import utcnow
        from app.schemas.domain import AuditEvent

        SqlStore(os.environ["DATABASE_URL"]).append_audit(AuditEvent(ts=utcnow(), action=self.marker, result="ok"))

    def _count(self):
        from sqlalchemy import text

        with self.engine.connect() as conn:
            return conn.execute(text("SELECT count(*) FROM audit_log WHERE action = :a"), {"a": self.marker}).scalar()

    def _assert_rejected(self, statement: str):
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError

        before = self._count()
        with self.assertRaises(DBAPIError) as cm:
            with self.engine.begin() as conn:
                conn.execute(text(statement), {"a": self.marker})
        self.assertIn("append-only", str(cm.exception))
        self.assertEqual(self._count(), before)

    def test_insert_and_select_still_work(self):
        self.assertGreaterEqual(self._count(), 1)

    def test_update_is_rejected(self):
        self._assert_rejected("UPDATE audit_log SET result = 'tampered' WHERE action = :a")

    def test_delete_is_rejected(self):
        self._assert_rejected("DELETE FROM audit_log WHERE action = :a")

    def test_truncate_is_rejected(self):
        self._assert_rejected("TRUNCATE audit_log")


@unittest.skipUnless(os.environ.get("NEO4J_URI") and HAS("neo4j"), "Neo4j not configured")
class Neo4jGraphTest(unittest.TestCase):
    def test_neo4j_matches_networkx(self):
        from app.graph.neo4j_backend import Neo4jGraph
        from app.graph.networkx_backend import NetworkXGraph

        store = support.container().store
        nx_g = NetworkXGraph.from_store(store)
        g = Neo4jGraph(os.environ["NEO4J_URI"], os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"])
        g.load_projection(store.graph_edges(None, None))
        self.assertEqual(g.stats()["nodes"], nx_g.stats()["nodes"])
        cid = support.first_of("device_sharing_ring")
        self.assertEqual([d.model_dump() for d in g.shared_devices(cid)], [d.model_dump() for d in nx_g.shared_devices(cid)])
        cyc = support.first_of("circular_transfer")
        accs = [a.account_id for a in store.accounts_for_customer(cyc)]
        self.assertEqual(g.candidate_cycles(accs, 5), nx_g.candidate_cycles(accs, 5))
        g.close()


@unittest.skipUnless(os.environ.get("QDRANT_URL"), "Qdrant not configured")
class QdrantTest(unittest.TestCase):
    def test_upsert_search_filter(self):
        from app.retrieval.vector_store import QdrantVectorStore

        vs = QdrantVectorStore(os.environ["QDRANT_URL"], f"fira_test_{uuid.uuid4().hex[:6]}")
        vs.recreate(3)
        vs.upsert(["a", "b"], np.array([[1, 0, 0], [0, 1, 0]], dtype=float),
                  [{"doc_type": "x", "document_id": "D1"}, {"doc_type": "y", "document_id": "D2"}])
        self.assertEqual(vs.count(), 2)
        self.assertEqual(vs.search(np.array([1.0, 0, 0]), 1)[0]["chunk_id"], "a")
        self.assertEqual(vs.search(np.array([1.0, 0, 0]), 1, {"doc_type": "y"})[0]["chunk_id"], "b")


@unittest.skipUnless(HAS("mcp"), "MCP SDK not installed")
class McpTest(unittest.TestCase):
    def test_mcp_tools_authorised_and_audited(self):
        import asyncio
        import json

        from app.mcp.server import AuthError, build_server, principal_from_key

        with self.assertRaises(AuthError):
            principal_from_key("wrong", {"k": "analyst"})
        c = support.container()
        p = principal_from_key("k", {"k": "analyst"})
        server = build_server(c, p)

        async def go():
            names = [t.name for t in await server.list_tools()]
            r = await server.call_tool("risk_analysis", {"entity_id": support.first_of("mule_account")})
            return names, r

        names, r = asyncio.run(go())
        self.assertEqual(set(names), {"customer_lookup", "transaction_analysis", "graph_search", "risk_analysis",
                                      "document_search", "investigation_history"})
        content = r.content if hasattr(r, "content") else r[0]
        data = json.loads(content[0].text)
        self.assertTrue(data["ok"])
        self.assertTrue(any(a.user_id == p.user_id and a.tool == "get_risk_signals" for a in c.store.list_audit(50)))


@unittest.skipUnless(HAS("fastapi") and HAS("httpx"), "FastAPI test client not installed")
class ApiStackTest(unittest.TestCase):
    """HTTP-level acceptance flow (runs in CI)."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from app.main import create_app

        cls.c = support.container(fresh=True)
        cls.c.settings.bootstrap_admin_password = "admin-password-123"
        cls.c.settings.bootstrap_analyst_password = "analyst-password-123"
        cls.client = TestClient(create_app(cls.c))
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def login(self, user, pw):
        r = self.client.post("/api/auth/login", json={"username": user, "password": pw})
        self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def test_acceptance_flow_over_http(self):
        cl = self.client
        self.assertEqual(cl.get("/health").json(), {"status": "ok"})
        self.assertEqual(cl.get("/api/customers/CUST-10001").status_code, 401)
        h = self.login("analyst", "analyst-password-123")
        cid = support.first_of("mule_account")
        self.assertEqual(cl.get(f"/api/search?q={cid}", headers=h).json()[0]["entity_id"], cid)
        prof = cl.get(f"/api/customers/{cid}", headers=h).json()
        self.assertNotIn("value_hash", str(prof))
        self.assertEqual(cl.get(f"/api/customers/{cid}/transactions", headers=h).status_code, 200)
        self.assertEqual(cl.get("/api/customers/DROP TABLE", headers=h).status_code, 422)
        r = cl.post("/api/agent/investigate", headers=h, json={"request": f"Investigate customer {cid} last 30 days"})
        self.assertEqual(r.status_code, 200, r.text)
        inv = r.json()["investigation_id"]
        self.assertTrue(cl.get(f"/api/investigations/{inv}/evidence", headers=h).json())
        self.assertTrue(cl.get(f"/api/investigations/{inv}/graph", headers=h).json()["nodes"])
        r = cl.post(f"/api/investigations/{inv}/decision", headers=h,
                    json={"decision": "confirm", "rationale": "Confirmed fan-in from unrelated senders."})
        self.assertEqual(r.json()["investigation"]["status"], "closed")
        self.assertEqual(cl.get("/api/audit", headers=h).status_code, 200)
        self.assertEqual(cl.get("/metrics", headers=h).status_code, 403)
        self.assertEqual(cl.post("/api/evaluation/run", headers=h, json={}).status_code, 403)
        ha = self.login("admin", "admin-password-123")
        self.assertEqual(cl.get("/metrics", headers=ha).status_code, 200)
        self.assertIn("X-Request-ID", cl.get("/health").headers)


if __name__ == "__main__":
    unittest.main()
