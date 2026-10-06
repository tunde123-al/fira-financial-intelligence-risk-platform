"""Case management over HTTP: states, RBAC, priority, notes, evidence, decisions, timeline, investigator queue."""
import unittest
from datetime import datetime

from fastapi.testclient import TestClient

from app.main import create_app
from app.security.auth import hash_password
from tests import support


class CaseManagementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = support.container(fresh=True)
        cls.c.settings.bootstrap_admin_password = "admin-password-123"
        cls.c.settings.bootstrap_analyst_password = "analyst-password-123"
        cls.c.settings.rate_limit_per_minute = 100_000
        cls.c.store.upsert_user({"user_id": "U-analyst2", "username": "analyst2", "role": "analyst", "active": True,
                                 "password_hash": hash_password("analyst2-password-123")})
        cls.client = TestClient(create_app(cls.c))
        cls.client.__enter__()
        cls.admin = cls.login("admin", "admin-password-123")
        cls.alice = cls.login("analyst", "analyst-password-123")
        cls.bob = cls.login("analyst2", "analyst2-password-123")
        # a customer can have only one open case, so each test takes its own customer that has alerts
        labels = support.labels()
        cls.pool = [lb["entity_id"] for lb in labels if lb["scenario"] in ("mule_account", "device_sharing_ring",
                                                                           "circular_transfer", "account_takeover")]
        r = cls.client.post("/api/monitoring/run", headers=cls.admin, json={"customer_ids": cls.pool})
        assert r.status_code == 200, r.text
        cls.used = 0

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    @classmethod
    def login(cls, u, p):
        r = cls.client.post("/api/auth/login", json={"username": u, "password": p})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def post(self, path, body=None, h=None):
        return self.client.post(path, headers=h or self.alice, json=body or {})

    def get(self, path, h=None, **kw):
        return self.client.get(path, headers=h or self.alice, **kw)

    def new_case(self, priority="medium"):
        while type(self).used < len(self.pool):
            cust = self.pool[type(self).used]
            type(self).used += 1
            alerts = self.get("/api/monitoring/alerts", params={"customer_id": cust, "limit": 50}).json()["items"]
            open_ = [a["alert_id"] for a in alerts if a["status"] != "RESOLVED" and not a["case_id"]]
            if not open_:
                continue
            r = self.post("/api/cases", {"customer_id": cust, "alert_ids": open_[:1], "priority": priority})
            self.assertIn(r.status_code, (200, 201), r.text)
            return r.json()["case"]
        self.skipTest("no more customers with open alerts")

    def test_01_case_has_required_fields_and_starts_open(self):
        cs = self.new_case()
        for k in ("case_id", "customer_id", "status", "priority", "title", "assigned_to", "opened_at", "created_at"):
            self.assertIn(k, cs)
        self.assertEqual(cs["status"], "OPEN")

    def test_02_invalid_transitions_are_rejected(self):
        cs = self.new_case()
        cid = cs["case_id"]
        self.assertEqual(self.post(f"/api/cases/{cid}/transition", {"status": "PENDING_REVIEW"}).status_code, 409)
        self.assertEqual(self.post(f"/api/cases/{cid}/transition", {"status": "CLOSED"}).status_code, 422)  # not an allowed target
        self.assertEqual(self.post(f"/api/cases/{cid}/transition", {"status": "INVESTIGATING"}).status_code, 200)
        self.assertEqual(self.post(f"/api/cases/{cid}/transition", {"status": "PENDING_REVIEW"}).status_code, 200)
        self.assertEqual(self.post(f"/api/cases/{cid}/transition", {"status": "INVESTIGATING"}).status_code, 200)

    def test_03_close_only_via_decision_and_closed_is_terminal(self):
        cid = self.new_case()["case_id"]
        self.post(f"/api/cases/{cid}/transition", {"status": "INVESTIGATING"})
        self.assertEqual(self.post(f"/api/cases/{cid}/decision", {"decision": "CLEARED", "reason": "no"}).status_code, 422)
        r = self.post(f"/api/cases/{cid}/decision", {"decision": "CLEARED", "reason": "reviewed, benign pattern"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.get(f"/api/cases/{cid}").json()["case"]["status"], "CLOSED")
        for body in ({"status": "INVESTIGATING"},):
            self.assertEqual(self.post(f"/api/cases/{cid}/transition", body).status_code, 409)
        self.assertEqual(self.post(f"/api/cases/{cid}/priority", {"priority": "high", "reason": "late change"}).status_code, 409)
        self.assertEqual(self.post(f"/api/cases/{cid}/assign", {"assignee": "U-admin"}, h=self.admin).status_code, 409)

    def test_04_assignment_rbac(self):
        cid = self.new_case()["case_id"]
        self.assertEqual(self.post(f"/api/cases/{cid}/assign", {"assignee": "U-analyst"}).status_code, 200)
        self.assertEqual(self.post(f"/api/cases/{cid}/assign", {"assignee": "U-analyst2"}).status_code, 403)  # not to others
        self.assertEqual(self.post(f"/api/cases/{cid}/assign", {"assignee": "U-analyst2"}, h=self.bob).status_code, 403)  # taken
        self.assertEqual(self.post(f"/api/cases/{cid}/assign", {"assignee": "U-analyst2"}, h=self.admin).status_code, 200)  # reassign
        self.assertEqual(self.post(f"/api/cases/{cid}/transition", {"status": "INVESTIGATING"}).status_code, 403)  # alice lost it
        self.assertEqual(self.post(f"/api/cases/{cid}/assign", {"assignee": "U-nobody"}, h=self.admin).status_code, 422)

    def test_05_priority_change_needs_reason_and_is_recorded(self):
        cid = self.new_case("medium")["case_id"]
        self.assertEqual(self.post(f"/api/cases/{cid}/priority", {"priority": "high", "reason": "x"}).status_code, 422)
        self.assertEqual(self.post(f"/api/cases/{cid}/priority", {"priority": "medium", "reason": "same value"}).status_code, 409)
        self.assertEqual(self.post(f"/api/cases/{cid}/priority", {"priority": "urgent", "reason": "bad value"}).status_code, 422)
        r = self.post(f"/api/cases/{cid}/priority", {"priority": "critical", "reason": "new evidence of fast onward movement"})
        self.assertEqual((r.status_code, r.json()["priority"]), (200, "critical"))
        wb = self.get(f"/api/cases/{cid}/workbench").json()
        ev = [e for e in wb["timeline"] if e["event"] == "priority_changed"]
        self.assertEqual((ev[-1]["detail"]["previous"], ev[-1]["detail"]["current"]), ("medium", "critical"))
        audits = [e.action for e in self.c.store.list_audit(limit=300)]
        self.assertIn("case_priority_changed", audits)

    def test_06_notes_evidence_and_chronological_timeline(self):
        cid = self.new_case()["case_id"]
        self.assertEqual(self.post(f"/api/cases/{cid}/notes", {"body": "first look: pass-through pattern"}).status_code, 201)
        r = self.post(f"/api/cases/{cid}/evidence", {"kind": "transaction", "ref": "TXN-NOPE", "note": "x"})
        self.assertIn(r.status_code, (404, 422))  # unknown evidence is refused, not stored
        wb = self.get(f"/api/cases/{cid}/workbench").json()
        stamps = [datetime.fromisoformat(e["ts"]) for e in wb["timeline"]]
        self.assertEqual(stamps, sorted(stamps))
        self.assertTrue(all(s.tzinfo is not None for s in stamps))
        self.assertTrue(wb["notes"])
        self.assertIn("note", {e["source"] for e in wb["timeline"]})

    def test_07_my_work_endpoint_and_scoping(self):
        cid = self.new_case()["case_id"]
        self.post(f"/api/cases/{cid}/assign", {"assignee": "U-analyst"})
        w = self.get("/api/monitoring/my-work").json()
        self.assertIn(cid, [c["case_id"] for c in w["open_cases"]])
        for k in ("open_alerts", "high_priority", "overdue", "recently_escalated", "recently_confirmed", "recently_cleared"):
            self.assertIn(k, w)
        self.assertNotIn(cid, [c["case_id"] for c in self.get("/api/monitoring/my-work", h=self.bob).json()["open_cases"]])
        self.assertEqual(self.client.get("/api/monitoring/my-work").status_code, 401)

    def test_08_queue_filters_and_sorting_by_triage(self):
        r = self.get("/api/monitoring/alerts", params={"sort": "triage_score", "order": "desc", "limit": 100})
        scores = [a["triage_score"] for a in r.json()["items"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        top = r.json()["items"][0]["triage_priority"]
        f = self.get("/api/monitoring/alerts", params={"priority": top}).json()
        self.assertTrue(f["items"] and all(a["triage_priority"] == top for a in f["items"]))
        self.assertEqual(self.get("/api/monitoring/alerts", params={"priority": "URGENT"}).status_code, 422)
        t = self.get(f"/api/monitoring/triage/{f['items'][0]['alert_id']}").json()
        self.assertEqual(len(t["factors"]), 12)
        self.assertEqual(self.post("/api/monitoring/triage/recompute", {}).status_code, 403)  # admin only
        self.assertEqual(self.post("/api/monitoring/triage/recompute", {}, h=self.admin).status_code, 200)

    def test_09_quality_and_feedback_endpoints(self):
        q = self.get("/api/monitoring/quality").json()
        self.assertIn("not_computed", q)
        self.assertNotIn("false_positive_rate", q["overall"])
        self.assertIsInstance(self.get("/api/monitoring/feedback").json(), list)


if __name__ == "__main__":
    unittest.main()
