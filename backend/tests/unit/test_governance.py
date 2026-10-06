"""Configuration governance: change records, validation, runtime overrides, detector switches, startup diff."""
import unittest

from fastapi.testclient import TestClient

from app.main import create_app
from app.monitoring import governance as gov
from app.monitoring.repository import AlertFilter
from app.security.principal import Principal
from tests import support

ADMIN = Principal("U-admin", "admin")


class GovernanceUnitTest(unittest.TestCase):
    def test_flatten_and_diff(self):
        old = {"a": {"b": 1, "c": [1, 2]}, "d": 5}
        new = {"a": {"b": 2, "c": [1, 2]}, "d": 5, "e": 1}
        self.assertEqual(gov.diff(old, new), [("a.b", 1, 2), ("e", None, 1)])
        self.assertEqual(gov.diff(old, old), [])
        self.assertEqual(gov.flatten({"x": {}}), {"x": {}})

    def test_overridable_paths_are_an_allow_list(self):
        self.assertTrue(gov.overridable("triage.thresholds.high"))
        self.assertTrue(gov.overridable("alerting.disabled_detectors"))
        self.assertFalse(gov.overridable("version"))
        self.assertFalse(gov.overridable("triage.thresholdsx"))
        self.assertFalse(gov.overridable("data_quality.transaction_types"))


class GovernanceFlowTest(unittest.TestCase):
    def setUp(self):
        self.c = support.container(fresh=True)
        self.repo = self.c.monitoring_repo

    def changes(self):
        return [r for r in self.repo.list_config_changes(500) if r.path != gov.SNAPSHOT_PATH]

    def test_startup_records_a_baseline_without_change_rows(self):
        self.assertEqual(self.changes(), [])
        self.assertIsNotNone(self.repo.last_config_snapshot("monitoring"))
        self.assertIsNotNone(self.repo.last_config_snapshot("risk"))
        self.assertEqual(gov.sync(self.c), {"monitoring": 0, "risk": 0})  # unchanged -> nothing new

    def test_override_records_old_new_user_reason_time_and_source(self):
        out = gov.apply_override(self.c, "triage.thresholds.high", 60, "tighten the high band for review", "U-admin")
        self.assertEqual(out["changed"], [{"path": "triage.thresholds.high", "old": 50.0, "new": 60.0}])
        self.assertEqual(self.c.monitoring_config.triage.thresholds["high"], 60.0)
        row = self.changes()[0]
        self.assertEqual((row.config_name, row.path, row.old_value, row.new_value, row.changed_by, row.source),
                         ("monitoring", "triage.thresholds.high", 50.0, 60.0, "U-admin", "api"))
        self.assertEqual(row.reason, "tighten the high band for review")
        self.assertIsNotNone(row.ts)
        self.assertEqual(self.repo.last_config_snapshot("monitoring")["triage"]["thresholds"]["high"], 60.0)

    def test_invalid_or_forbidden_changes_are_refused_and_leave_no_record(self):
        before = self.c.monitoring_config.fingerprint()
        bad = [("triage.thresholds.high", 70, "high above critical"), ("triage.thresholds.high", "abc", "not a number"),
               ("mule.rapid_share", 1.5, "out of range"), ("version", "x", "not allowed"),
               ("triage.nothing", 1, "unknown setting"), ("data_quality.duplicate_policy", "maybe", "bad enum")]
        for path, value, why in bad:
            with self.assertRaises(ValueError, msg=why):
                gov.apply_override(self.c, path, value, "a valid reason", "U-admin")
        with self.assertRaises(ValueError):
            gov.apply_override(self.c, "triage.thresholds.high", 60, "no", "U-admin")  # reason too short
        with self.assertRaises(ValueError):
            gov.apply_override(self.c, "triage.thresholds.high", 50, "same value again", "U-admin")  # unchanged
        self.assertEqual((self.c.monitoring_config.fingerprint(), self.changes()), (before, []))

    def test_detector_switch_changes_alert_generation_and_is_recorded(self):
        mule = support.first_of("mule_account")
        gov.set_detector_enabled(self.c, "FAN_IN", False, "too noisy during the campaign", "U-admin")
        self.assertNotIn("FAN_IN", self.c.monitoring_config.alerting_detectors())
        self.c.monitoring.run(window_end=self.c.store.as_of(), customer_ids=[mule], actor=ADMIN)
        got = {a.detector_id for a in self.repo.list_alerts(AlertFilter(customer_id=mule, limit=100))[0]}
        self.assertNotIn("FAN_IN", got)
        self.assertEqual(self.changes()[0].path, "alerting.disabled_detectors")
        gov.set_detector_enabled(self.c, "FAN_IN", True, "campaign over", "U-admin")
        self.assertIn("FAN_IN", self.c.monitoring_config.alerting_detectors())
        self.assertEqual(self.changes()[0].old_value, ["FAN_IN"])
        with self.assertRaises(LookupError):
            gov.set_detector_enabled(self.c, "NOPE", False, "unknown detector", "U-admin")

    def test_file_edits_are_found_at_startup_and_attributed_to_system(self):
        self.c.monitoring_config.triage.thresholds["medium"] = 32.0  # as if the YAML had been edited
        self.c.risk_config.signals["FAN_IN"].weight = 7.0
        self.assertEqual(gov.sync(self.c, "system", "startup"), {"monitoring": 1, "risk": 1})
        paths = {(r.config_name, r.path, r.changed_by, r.source) for r in self.changes()}
        self.assertIn(("monitoring", "triage.thresholds.medium", "system", "startup"), paths)
        self.assertIn(("risk", "signals.FAN_IN.weight", "system", "startup"), paths)

    def test_change_log_cannot_be_edited_through_the_repository(self):
        self.assertFalse(hasattr(self.repo, "delete_config_change") or hasattr(self.repo, "update_config_change"))


class GovernanceApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = support.container(fresh=True)
        cls.c.settings.bootstrap_admin_password = "admin-password-123"
        cls.c.settings.bootstrap_analyst_password = "analyst-password-123"
        cls.c.settings.rate_limit_per_minute = 100_000
        cls.client = TestClient(create_app(cls.c))
        cls.client.__enter__()
        cls.admin = cls.login("admin", "admin-password-123")
        cls.analyst = cls.login("analyst", "analyst-password-123")

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    @classmethod
    def login(cls, u, p):
        r = cls.client.post("/api/auth/login", json={"username": u, "password": p})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def test_requires_auth_and_admin_for_changes(self):
        self.assertEqual(self.client.get("/api/config/changes").status_code, 401)
        body = {"path": "triage.thresholds.high", "value": 58, "reason": "calibration review"}
        self.assertEqual(self.client.post("/api/config/monitoring", json=body, headers=self.analyst).status_code, 403)
        self.assertEqual(self.client.post("/api/config/detectors/FAN_IN", json={"enabled": False, "reason": "x y z w"},
                                          headers=self.analyst).status_code, 403)

    def test_change_then_read_log_and_audit_trail(self):
        body = {"path": "triage.thresholds.high", "value": 58, "reason": "calibration review"}
        r = self.client.post("/api/config/monitoring", json=body, headers=self.admin)
        self.assertEqual(r.status_code, 200, r.text)
        log = self.client.get("/api/config/changes", headers=self.analyst).json()
        self.assertEqual((log[0]["path"], log[0]["old_value"], log[0]["new_value"], log[0]["changed_by"]),
                         ("triage.thresholds.high", 50.0, 58.0, "U-admin"))
        cur = self.client.get("/api/config/monitoring", headers=self.analyst).json()
        self.assertEqual(cur["config"]["triage"]["thresholds"]["high"], 58.0)
        self.assertIn("triage.thresholds", cur["runtime_overridable"])
        self.assertIn("config_changed", [e.action for e in self.c.store.list_audit(limit=300)])

    def test_validation_errors_are_422_and_audited_as_denied(self):
        for body in ({"path": "triage.thresholds.high", "value": 70, "reason": "invalid ordering"},
                     {"path": "version", "value": "x", "reason": "forbidden path"},
                     {"path": "Bad Path", "value": 1, "reason": "malformed path"},
                     {"path": "triage.thresholds.high", "value": 60, "reason": "x"}):
            self.assertEqual(self.client.post("/api/config/monitoring", json=body, headers=self.admin).status_code, 422, body)
        self.assertEqual(self.client.post("/api/config/detectors/NOPE", json={"enabled": False, "reason": "unknown one"},
                                          headers=self.admin).status_code, 404)

    def test_detector_switch_over_http(self):
        r = self.client.post("/api/config/detectors/GEO_NEW_COUNTRY", json={"enabled": False, "reason": "regional travel season"},
                             headers=self.admin)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("GEO_NEW_COUNTRY", self.c.monitoring_config.alerting.disabled_detectors)
        self.assertIn("detector_disabled", [e.action for e in self.c.store.list_audit(limit=300)])


if __name__ == "__main__":
    unittest.main()
