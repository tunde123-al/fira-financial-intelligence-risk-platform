"""HTTP-level tests for monitoring, alerts, cases, the workbench and network endpoints (in-memory stack)."""
import unittest

from fastapi.testclient import TestClient

from app.main import create_app
from app.security.auth import hash_password
from app.synthetic.reference import FX_PER_USD
from tests import support


class MonitoringApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = support.container(fresh=True)
        cls.c.settings.bootstrap_admin_password = "admin-password-123"
        cls.c.settings.bootstrap_analyst_password = "analyst-password-123"
        cls.c.settings.rate_limit_per_minute = 100_000  # the limiter itself is covered by other tests
        cls.c.store.upsert_user({"user_id": "U-analyst2", "username": "analyst2", "role": "analyst", "active": True,
                                 "password_hash": hash_password("analyst2-password-123")})
        cls.client = TestClient(create_app(cls.c))
        cls.client.__enter__()
        cls.admin = cls.login("admin", "admin-password-123")
        cls.alice = cls.login("analyst", "analyst-password-123")
        cls.bob = cls.login("analyst2", "analyst2-password-123")
        cls.mule = support.first_of("mule_account")
        r = cls.client.post("/api/monitoring/run", headers=cls.admin, json={"customer_ids": [cls.mule]})
        assert r.status_code == 200, r.text
        cls.first_run = r.json()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    @classmethod
    def login(cls, user, pw):
        r = cls.client.post("/api/auth/login", json={"username": user, "password": pw})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def get(self, path, h=None, **kw):
        return self.client.get(path, headers=h or self.alice, **kw)

    def post(self, path, body=None, h=None):
        return self.client.post(path, headers=h or self.alice, json=body or {})

    def alerts(self, **params):
        r = self.get("/api/monitoring/alerts", params={"customer_id": self.mule, "limit": 200, **params})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # ------------------------------------------------------------------ access control
    def test_01_requires_authentication(self):
        for path in ("/api/monitoring/alerts", "/api/cases", "/api/monitoring/kpis", "/api/monitoring/detectors",
                     "/api/network/customers/CUST-10001/counterparties"):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        self.assertEqual(self.client.post("/api/cases", json={"customer_id": "CUST-10001"}).status_code, 401)

    def test_02_operational_endpoints_are_admin_only(self):
        self.assertEqual(self.post("/api/monitoring/run", {}).status_code, 403)
        body = {"transactions": [{"timestamp": "2026-09-30T10:00:00Z", "sender_account_id": "ACC-200001",
                                  "amount": 10, "currency": "USD", "transaction_type": "transfer", "channel": "web"}]}
        self.assertEqual(self.post("/api/monitoring/transactions", body).status_code, 403)

    def test_03_run_is_recorded_and_visible(self):
        self.assertGreaterEqual(self.first_run["alerts_created"], 1)
        runs = self.get("/api/monitoring/runs").json()
        self.assertEqual(runs[0]["run_id"], self.first_run["run_id"])
        det = {d["detector_id"]: d for d in self.get("/api/monitoring/detectors").json()["detectors"]}
        self.assertTrue(det["STRUCTURING"]["creates_alerts"])
        self.assertFalse(det["NETWORK_EXPOSURE"]["creates_alerts"])

    # ------------------------------------------------------------------ queue
    def test_10_filters_sorting_and_validation(self):
        data = self.alerts()
        self.assertEqual(data["total"], len(data["items"]))
        first = data["items"][0]
        self.assertEqual(self.alerts(status="NEW")["total"], data["total"])
        self.assertEqual(self.alerts(status="RESOLVED")["total"], 0)
        self.assertTrue(all(a["detector_id"] == first["detector_id"] for a in self.alerts(detector=first["detector_id"])["items"]))
        lo = min(a["risk_score"] for a in data["items"])
        hi_only = self.alerts(min_risk=lo + 0.01)["items"]
        self.assertTrue(all(a["risk_score"] >= lo + 0.01 for a in hi_only))
        self.assertLessEqual(len(hi_only), data["total"])
        asc = self.alerts(sort="risk_score", order="asc")["items"]
        self.assertEqual([a["risk_score"] for a in asc], sorted(a["risk_score"] for a in asc))
        self.assertGreaterEqual(self.alerts(q=first["detector_id"].lower())["total"], 1)
        self.assertEqual(self.alerts(assigned_to="unassigned")["total"], data["total"])
        self.assertEqual(self.get("/api/monitoring/alerts", params={"status": "BOGUS"}).status_code, 422)
        self.assertEqual(self.get("/api/monitoring/alerts", params={"detector": "'; DROP TABLE x;--"}).status_code, 422)
        self.assertEqual(self.get("/api/monitoring/alerts", params={"sort": "password"}).status_code, 422)
        self.assertEqual(self.get("/api/monitoring/alerts", params={"limit": 100000}).status_code, 422)
        self.assertEqual(self.get("/api/monitoring/alerts", params={"date_from": "not-a-date"}).status_code, 422)

    def test_11_alert_detail(self):
        a = self.alerts()["items"][0]
        d = self.get(f"/api/monitoring/alerts/{a['alert_id']}").json()
        self.assertEqual(d["alert"]["alert_id"], a["alert_id"])
        self.assertEqual(d["events"][0]["event_type"], "created")
        self.assertEqual(self.get("/api/monitoring/alerts/MAL-FFFFFFFFFFFF").status_code, 404)
        self.assertEqual(self.get("/api/monitoring/alerts/not-an-id").status_code, 422)

    # ------------------------------------------------------------------ workflow
    def fresh_alert(self, scenario):
        cid = support.first_of(scenario)
        self.post("/api/monitoring/run", {"customer_ids": [cid]}, h=self.admin)
        items = self.get("/api/monitoring/alerts", params={"customer_id": cid, "status": "NEW"}).json()["items"]
        self.assertTrue(items, scenario)
        return items[0]

    def test_20_alert_transitions_over_http(self):
        a = self.fresh_alert("geographic_anomaly")
        aid = a["alert_id"]
        r = self.post(f"/api/monitoring/alerts/{aid}/transition", {"status": "TRIAGED"})
        self.assertEqual((r.status_code, r.json()["status"], r.json()["assigned_to"]), (200, "TRIAGED", "U-analyst"))
        self.assertEqual(self.post(f"/api/monitoring/alerts/{aid}/transition", {"status": "INVESTIGATING"}, h=self.bob).status_code, 403)
        self.assertEqual(self.post(f"/api/monitoring/alerts/{aid}/assign", {"assignee": "U-analyst2"}, h=self.bob).status_code, 403)
        self.assertEqual(self.post(f"/api/monitoring/alerts/{aid}/transition", {"status": "NEW"}).status_code, 422)
        self.assertEqual(self.post(f"/api/monitoring/alerts/{aid}/transition", {"status": "ESCALATED"}).status_code, 409)
        self.assertEqual(self.post(f"/api/monitoring/alerts/{aid}/resolve", {"resolution": "CLEARED", "reason": "x"}).status_code, 422)
        r = self.post(f"/api/monitoring/alerts/{aid}/resolve", {"resolution": "FALSE_POSITIVE", "reason": "benign business activity"})
        self.assertEqual((r.status_code, r.json()["status"], r.json()["resolution"]), (200, "RESOLVED", "FALSE_POSITIVE"))
        self.assertEqual(self.post(f"/api/monitoring/alerts/{aid}/transition", {"status": "INVESTIGATING"}).status_code, 409)
        r = self.post(f"/api/monitoring/alerts/{aid}/assign", {"assignee": "U-analyst2"}, h=self.admin)
        self.assertEqual(r.status_code, 409)  # resolved alerts cannot be assigned

    def test_21_admin_can_assign_to_a_known_user_only(self):
        a = self.fresh_alert("transaction_burst")
        r = self.post(f"/api/monitoring/alerts/{a['alert_id']}/assign", {"assignee": "U-analyst2"}, h=self.admin)
        self.assertEqual((r.status_code, r.json()["assigned_to"]), (200, "U-analyst2"))
        self.assertEqual(self.post(f"/api/monitoring/alerts/{a['alert_id']}/assign", {"assignee": "U-ghost"}, h=self.admin).status_code, 422)
        self.assertEqual(self.post(f"/api/monitoring/alerts/{a['alert_id']}/assign", {"assignee": "no prefix"}, h=self.admin).status_code, 422)

    def test_30_case_flow_over_http_with_workbench_notes_evidence_and_decision(self):
        a = self.fresh_alert("mule_account")
        r = self.post(f"/api/monitoring/alerts/{a['alert_id']}/case", {})
        self.assertEqual(r.status_code, 201, r.text)
        case = r.json()["case"]
        cid = case["case_id"]
        self.assertTrue(r.json()["created"])
        self.assertEqual(self.get("/api/cases", params={"status": "OPEN", "assigned_to": "me"}).json()["total"] >= 1, True)
        self.assertEqual(self.get("/api/cases", params={"status": "NOPE"}).status_code, 422)
        self.assertEqual(self.get(f"/api/cases/{cid}").json()["alerts"][0]["case_id"], cid)

        wb = self.get(f"/api/cases/{cid}/workbench")
        self.assertEqual(wb.status_code, 200, wb.text)
        w = wb.json()
        for key in ("case", "customer", "risk", "triggered_rules", "related_transactions", "recent_transactions",
                    "account_activity", "counterparties", "network", "related_alerts", "documents", "evidence",
                    "notes", "timeline", "decision"):
            self.assertIn(key, w, key)
        self.assertTrue(w["triggered_rules"] and w["risk"]["score_components"] and w["risk"]["explanation"])
        self.assertEqual({x["window"] for x in w["account_activity"]["windows"]}, {"1h", "24h", "7d", "30d"})
        self.assertTrue(w["network"]["graph"]["nodes"])
        self.assertIn("direct", w["counterparties"])
        self.assertTrue(all(d["evidence_class"] == "DOCUMENT_EVIDENCE" for d in w["documents"]))
        self.assertNotIn("value_hash", str(w))  # masking applies to the workbench too

        # investigation (existing agent) is linked, evidence is classified, narrative is labelled
        inv = self.post(f"/api/cases/{cid}/investigate", {})
        self.assertEqual(inv.status_code, 200, inv.text)
        w2 = self.get(f"/api/cases/{cid}/workbench").json()
        self.assertEqual(w2["investigation"]["investigation_id"], inv.json()["investigation_id"])
        classes = {e["evidence_class"] for e in w2["evidence"]["investigation"]}
        self.assertTrue(classes <= {"DATABASE_FACT", "RULE_RESULT", "GRAPH_RESULT", "DOCUMENT_EVIDENCE"})
        self.assertTrue(all(c["ai_generated"] is False for c in w2["investigation"]["narrative"]))  # no LLM configured

        # notes and evidence
        self.assertEqual(self.post(f"/api/cases/{cid}/notes", {"body": "Customer unreachable by phone."}).status_code, 201)
        self.assertEqual(self.post(f"/api/cases/{cid}/notes", {"body": "x"}, h=self.bob).status_code, 403)
        tid = (a["transaction_ids"] or [None])[0]
        if tid:
            self.assertEqual(self.post(f"/api/cases/{cid}/evidence", {"kind": "transaction", "ref": tid}).status_code, 201)
        self.assertEqual(self.post(f"/api/cases/{cid}/evidence", {"kind": "transaction", "ref": "TXN-987654321012"}).status_code, 404)
        self.assertEqual(self.post(f"/api/cases/{cid}/evidence", {"kind": "freeform", "ref": "made up fact"}).status_code, 422)

        # decision: only the assignee (or an admin), a reason is mandatory, the case must be in progress
        self.assertEqual(self.post(f"/api/cases/{cid}/decision", {"decision": "CLEARED", "reason": "nothing"}, h=self.bob).status_code, 403)
        self.assertEqual(self.post(f"/api/cases/{cid}/decision", {"decision": "CLEARED", "reason": "no"}).status_code, 422)
        self.assertEqual(self.post(f"/api/cases/{cid}/decision", {"decision": "CLEARED", "reason": "nothing to see"}).status_code, 409)
        self.assertEqual(self.post(f"/api/cases/{cid}/transition", {"status": "INVESTIGATING"}).status_code, 200)
        r = self.post(f"/api/cases/{cid}/decision", {"decision": "CONFIRMED_SUSPICIOUS", "reason": "Fan-in from unrelated senders confirmed."})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["case"]["status"], "CLOSED")
        self.assertEqual(r.json()["investigation"]["decision"], "confirm")
        self.assertEqual(self.get(f"/api/monitoring/alerts/{a['alert_id']}").json()["alert"]["resolution"], "CONFIRMED_SUSPICIOUS")
        self.assertEqual(self.post(f"/api/cases/{cid}/notes", {"body": "too late"}).status_code, 409)
        # the existing investigation is consistent with the case decision
        inv_state = self.get(f"/api/investigations/{inv.json()['investigation_id']}").json()
        self.assertEqual((inv_state["status"], inv_state["conclusion"]), ("closed", "confirmed_suspicious"))

        audit = self.get("/api/audit", h=self.admin, params={"limit": 2000}).json()
        actions = {e["action"] for e in audit if e.get("entity_id") in {cid, a["alert_id"]}}
        self.assertTrue({"case_created", "decision_recorded", "case_closed", "investigation_note_added",
                         "alert_status_changed"} <= actions, actions)

    def test_31_case_for_unknown_customer_and_foreign_alert(self):
        self.assertEqual(self.post("/api/cases", {"customer_id": "CUST-99999999"}).status_code, 404)
        a = self.fresh_alert("high_risk_merchant")
        other = support.first_of("dormant_reactivation")
        self.assertEqual(self.post("/api/cases", {"customer_id": other, "alert_ids": [a["alert_id"]]}).status_code, 422)
        self.assertEqual(self.get("/api/cases/CASE-FFFFFFFFFFFF").status_code, 404)
        self.assertEqual(self.get("/api/cases/CASE-FFFFFFFFFFFF/workbench").status_code, 404)

    # ------------------------------------------------------------------ audit, risk, network, ops
    def test_40_audit_is_read_only_over_http(self):
        for method in ("put", "patch", "delete"):
            r = getattr(self.client, method)("/api/audit", headers=self.admin)
            self.assertEqual(r.status_code, 405, method)
        self.assertEqual(self.client.delete("/api/cases/CASE-FFFFFFFFFFFF", headers=self.admin).status_code, 405)
        self.assertEqual(self.client.delete("/api/monitoring/alerts/MAL-FFFFFFFFFFFF", headers=self.admin).status_code, 405)

    def test_41_risk_endpoint_is_explainable(self):
        r = self.get(f"/api/risk/customer/{self.mule}")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["detector_results"] and d["explanation"] and d["score_components"])
        self.assertAlmostEqual(sum(x["points"] for x in d["score_components"]), d["score"], delta=0.2)
        self.assertTrue(d["supporting_transactions"])
        self.assertTrue(all("confidence" not in r for r in d["detector_results"]))
        self.assertEqual(d["not_in_score"], ["customer profile", "document evidence"])

    def test_42_network_questions(self):
        d1 = self.get(f"/api/network/customers/{self.mule}/counterparties", params={"degree": 1}).json()
        d2 = self.get(f"/api/network/customers/{self.mule}/counterparties", params={"degree": 2}).json()
        self.assertTrue(d1["counterparties"])
        self.assertTrue(all(c["degree"] == 1 for c in d1["counterparties"]))
        self.assertTrue(any(c["degree"] == 2 for c in d2["counterparties"]))
        sb = self.get(f"/api/network/customers/{self.mule}/shared-beneficiaries").json()
        self.assertIn("shared_beneficiaries", sb)
        circ = support.first_of("circular_transfer")
        cyc = self.get(f"/api/network/customers/{circ}/cycles", params={"days": 60}).json()["cycles"]
        self.assertTrue(any(c["time_ordered"] for c in cyc))
        accs = [a["account_id"] for a in self.get(f"/api/customers/{circ}").json()["accounts"]]
        cr = self.get("/api/network/common-recipients", params={"accounts": ",".join(accs + ["ACC-200001"]), "min_sources": 2})
        self.assertEqual(cr.status_code, 200)
        self.assertEqual(self.get("/api/network/common-recipients", params={"accounts": "x; drop"}).status_code, 422)
        self.assertEqual(self.get("/api/network/customers/CUST-99999999/counterparties").status_code, 404)
        self.assertEqual(self.get("/api/network/customers/NOT-ID/counterparties").status_code, 422)
        fr = self.get(f"/api/network/flagged-recipients/{self.mule}")
        self.assertEqual(fr.status_code, 200)

    def test_43_activity_windows(self):
        d = self.get(f"/api/customers/{self.mule}/activity-windows").json()
        self.assertEqual([w["window"] for w in d["windows"]], ["1h", "24h", "7d", "30d"])
        one = self.get(f"/api/customers/{self.mule}/activity-windows", params={"windows": "7d"}).json()
        self.assertEqual(len(one["windows"]), 1)
        self.assertEqual(self.get(f"/api/customers/{self.mule}/activity-windows", params={"windows": "2d"}).status_code, 422)

    def test_44_health_ready_and_dashboard_kpis(self):
        for path in ("/ready", "/health/ready"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(r.json()["checks"]["monitoring_store"], "ok")
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        dash = self.get("/api/dashboard").json()
        m = dash["monitoring"]
        self.assertGreaterEqual(m["alerts_generated"], 1)
        self.assertIn("cases_by_status", m)
        self.assertEqual(m["alerts_generated"], sum(m["alerts_by_status"].values()))

    def test_45_ingest_over_http_validates_and_monitors(self):
        import datetime as dt

        acc = self.c.store.accounts_for_customer(support.first_of("normal"))[0]
        end = self.c.store.as_of()
        rows = [{"timestamp": (end - dt.timedelta(hours=6 - i)).isoformat(), "sender_account_id": acc.account_id,
                 "amount": 9200 * 1.0, "currency": "USD", "amount_usd": 9200, "transaction_type": "transfer",
                 "channel": "web", "external_counterparty": "EXT-HTTP"} for i in range(3)]
        for r in rows:  # USD 9,200 expressed in the account's currency
            r["amount"] = round(9200 * FX_PER_USD[acc.currency], 2)
            r["currency"] = acc.currency
        bad = {"timestamp": end.isoformat(), "sender_account_id": "ACC-200001", "amount": -5, "currency": "USD",
               "transaction_type": "transfer", "channel": "web"}
        rb = self.post("/api/monitoring/transactions", {"transactions": [bad]}, h=self.admin)
        self.assertEqual(rb.status_code, 200, rb.text)  # bad rows are quarantined, not a request failure
        self.assertEqual((rb.json()["accepted"], rb.json()["rejected"][0]["code"]), (0, "NON_POSITIVE_AMOUNT"))
        r = self.post("/api/monitoring/transactions", {"transactions": rows}, h=self.admin)
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual((out["accepted"], out["rejected_count"]), (3, 0))
        self.assertIn("monitoring_run", out)
        unknown = dict(rows[0], sender_account_id="ACC-99999999")
        r2 = self.post("/api/monitoring/transactions", {"transactions": [unknown]}, h=self.admin).json()
        self.assertEqual((r2["accepted"], r2["rejected_count"]), (0, 1))
        r3 = self.post("/api/monitoring/transactions", {"transactions": [dict(rows[0], bogus=1)]}, h=self.admin).json()
        self.assertEqual((r3["accepted"], r3["rejected"][0]["code"]), (0, "MALFORMED_ROW"))
        self.assertEqual(self.post("/api/monitoring/transactions", {"transactions": [], "x": 1}, h=self.admin).status_code, 422)

    def test_46_errors_do_not_leak_internals(self):
        r = self.get("/api/monitoring/alerts/MAL-FFFFFFFFFFFF")
        self.assertNotIn("Traceback", r.text)
        self.assertNotIn("sqlalchemy", r.text.lower())


if __name__ == "__main__":
    unittest.main()
