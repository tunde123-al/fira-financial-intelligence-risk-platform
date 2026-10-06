"""Failure modes that do not need infrastructure: dependency outages, invalid configuration, isolated failures."""
import unittest
from unittest import mock

import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.monitoring.config import MonitoringConfig, load_monitoring_config
from app.security.principal import Principal
from app.services.container import build_container
from tests import support

ADMIN = Principal("U-admin", "admin")


class DependencyOutageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = support.container(fresh=True)
        cls.c.settings.bootstrap_admin_password = "admin-password-123"
        cls.c.settings.rate_limit_per_minute = 100_000
        cls.client = TestClient(create_app(cls.c))
        cls.client.__enter__()
        r = cls.client.post("/api/auth/login", json={"username": "admin", "password": "admin-password-123"})
        cls.h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_database_down_makes_readiness_fail_but_liveness_stays_up(self):
        with mock.patch.object(self.c.store, "ping", side_effect=RuntimeError("connection refused")):
            r = self.client.get("/health/ready")
            self.assertEqual(r.status_code, 503)
            self.assertIn("down", r.json()["checks"]["database"])
            self.assertNotIn("Traceback", r.text)
            self.assertNotIn("connection refused", r.text)  # the cause is logged, not returned
            self.assertEqual(self.client.get("/health").status_code, 200)

    def test_vector_store_down_degrades_readiness_but_core_endpoints_keep_working(self):
        with mock.patch.object(self.c.vectors, "ping", return_value=False):
            r = self.client.get("/health/ready")
            self.assertEqual((r.status_code, r.json()["status"]), (503, "degraded"))
            self.assertEqual(r.json()["checks"]["vector_store"], "down")
            self.assertEqual(self.client.get("/api/monitoring/alerts", headers=self.h).status_code, 200)
            self.assertEqual(self.client.get("/api/data-quality/summary", headers=self.h).status_code, 200)

    def test_unexpected_exception_in_a_handler_returns_a_safe_500_with_a_request_id(self):
        with mock.patch.object(self.c.monitoring, "data_quality_summary", side_effect=KeyError("secret-internal-name")):
            r = self.client.get("/api/data-quality/summary", headers=self.h)
            self.assertEqual(r.status_code, 500)
            self.assertNotIn("secret-internal-name", r.text)
            self.assertTrue(r.json()["request_id"])

    def test_unreachable_qdrant_does_not_stop_the_application_from_starting(self):
        s = support.make_settings(vector_backend="qdrant", qdrant_url="http://127.0.0.1:9")
        c = build_container(s, load_ml=False)
        self.assertFalse(c.vectors.ping())
        c.monitoring.run(window_end=c.store.as_of(), customer_ids=[support.first_of("mule_account")], actor=ADMIN)


class InvalidConfigurationTest(unittest.TestCase):
    def write(self, tmp, mutate):
        base = yaml.safe_load(open(support.make_settings().monitoring_config_path, encoding="utf-8").read())
        mutate(base)
        p = tmp / "m.yaml"
        p.write_text(yaml.safe_dump(base), encoding="utf-8")
        return p

    def test_invalid_values_are_rejected_with_a_validation_error(self):
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        for name, mutate in (("thresholds", lambda d: d["triage"]["thresholds"].update(high=70)),
                             ("lookback", lambda d: d.update(lookback_days=0)),
                             ("mule share", lambda d: d["mule"].update(rapid_share=2)),
                             ("policy", lambda d: d["data_quality"].update(duplicate_policy="maybe"))):
            with self.assertRaises(ValidationError, msg=name):
                load_monitoring_config(self.write(tmp, mutate))

    def test_a_valid_file_loads_and_unknown_keys_do_not_silently_change_behaviour(self):
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        cfg = load_monitoring_config(self.write(tmp, lambda d: d["triage"]["thresholds"].update(high=60)))
        self.assertEqual(cfg.triage.thresholds["high"], 60.0)
        self.assertEqual(MonitoringConfig().triage.thresholds["high"], 50.0)

    def test_container_refuses_to_start_with_a_broken_monitoring_file(self):
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        bad = self.write(tmp, lambda d: d["triage"]["thresholds"].update(high=70))
        s = support.make_settings(monitoring_config_path=bad)
        with self.assertRaises(ValidationError):
            build_container(s, load_ml=False)

    def test_missing_risk_config_file_fails_fast(self):
        s = support.make_settings(risk_config_path=support.make_settings().risk_config_path.with_name("nope.yaml"))
        with self.assertRaises(FileNotFoundError):
            build_container(s, load_ml=False)


class IsolatedFailureTest(unittest.TestCase):
    def test_one_failing_customer_does_not_stop_a_monitoring_run(self):
        c = support.container(fresh=True)
        ids = [lb["entity_id"] for lb in support.labels() if lb["scenario"] in ("mule_account", "circular_transfer")][:6]
        real = c.risk_engine.assess_customer

        def flaky(cid, *a, **k):
            if cid == ids[0]:
                raise RuntimeError("boom")
            return real(cid, *a, **k)

        with mock.patch.object(c.risk_engine, "assess_customer", side_effect=flaky):
            run = c.monitoring.run(window_end=c.store.as_of(), customer_ids=ids, actor=ADMIN)
        self.assertEqual(run.errors, 1)
        self.assertGreater(run.alerts_created, 0)
        self.assertEqual(run.customers_evaluated, len(ids))

    def test_audit_failure_is_surfaced_not_swallowed_in_a_workflow(self):
        from tests.unit.test_monitoring_alerts import make_users

        c = support.container(fresh=True)
        make_users(c.store)
        ids = [support.first_of("mule_account")]
        c.monitoring.run(window_end=c.store.as_of(), customer_ids=ids, actor=ADMIN)
        from app.monitoring.repository import AlertFilter

        a = c.monitoring_repo.list_alerts(AlertFilter(limit=1))[0][0]
        with mock.patch.object(c.store, "append_audit", side_effect=RuntimeError("audit store down")):
            with self.assertRaises(RuntimeError):
                c.monitoring.assign_alert(a.alert_id, "U-admin", ADMIN)

    def test_ingest_rejects_nothing_silently_when_all_rows_fail_validation(self):
        c = support.container(fresh=True)
        out = c.monitoring.ingest([{"nope": 1}, "x", {"timestamp": "bad"}], ADMIN, run_monitoring=False)
        self.assertEqual((out["accepted"], out["rejected_count"]), (0, 3))
        self.assertEqual(out["batch"]["received"], out["batch"]["rejected"])


if __name__ == "__main__":
    unittest.main()
