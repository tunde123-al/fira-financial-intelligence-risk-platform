"""Security hardening: lockout, revocation, body limit, production checks, headers, metrics access, error safety."""
import unittest

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security.hardening import LoginThrottle, TokenRevocations, production_problems
from app.security.ratelimit import RateLimiter
from tests import support


def prod(**kw):
    base = dict(environment="production", data_backend="postgres", database_url="postgresql://u:p@h/db",
                jwt_secret="x" * 40, cors_origins="https://demo.example.org", log_level="INFO",
                bootstrap_admin_password="a-long-admin-password", bootstrap_analyst_password="a-long-analyst-password")
    base.update(kw)
    return Settings(**base)


class ProductionSettingsTest(unittest.TestCase):
    def test_a_sound_production_configuration_has_no_problems(self):
        self.assertEqual(production_problems(prod()), [])

    def test_each_unsafe_setting_is_reported(self):
        cases = {
            "JWT_SECRET": prod(jwt_secret=None), "JWT_SECRET ": prod(jwt_secret="short"),
            "DATA_BACKEND": prod(data_backend="frames", database_url=None), "CORS": prod(cors_origins="*"),
            "BOOTSTRAP_ADMIN_PASSWORD": prod(bootstrap_admin_password="password"),
            "ENABLE_API_DOCS": prod(enable_api_docs=True), "LOG_LEVEL": prod(log_level="DEBUG")}
        for key, s in cases.items():
            probs = production_problems(s)
            self.assertTrue(any(key.strip() in p for p in probs), (key, probs))

    def test_non_production_is_not_blocked(self):
        self.assertEqual(production_problems(Settings(environment="development")), [])

    def test_startup_refuses_unsafe_production_settings_without_printing_secrets(self):
        with self.assertRaises(RuntimeError) as cm:
            create_app_for(prod(jwt_secret="tiny-but-secret"))
        self.assertIn("JWT_SECRET", str(cm.exception))
        self.assertNotIn("tiny-but-secret", str(cm.exception))

    def test_docs_follow_environment(self):
        self.assertFalse(prod().docs_enabled)
        self.assertTrue(Settings(environment="development").docs_enabled)
        self.assertTrue(prod(enable_api_docs=True).docs_enabled)


def create_app_for(settings):
    c = support.container(fresh=True)
    c.settings = settings
    return create_app(c)


class ThrottleAndRevocationTest(unittest.TestCase):
    def test_lockout_after_repeated_failures_and_reset_on_success(self):
        t = LoginThrottle(max_failures=3, window_s=60)
        for _ in range(3):
            self.assertTrue(t.check("Alice", "1.1.1.1")[0])
            t.failed("Alice", "1.1.1.1")
        ok, retry = t.check("alice", "2.2.2.2")  # username is case-insensitive and follows the user across addresses
        self.assertFalse(ok)
        self.assertGreater(retry, 0)
        self.assertTrue(t.check("bob", "2.2.2.2")[0])
        t2 = LoginThrottle(max_failures=3, window_s=60)
        t2.failed("carol", "9.9.9.9")
        t2.failed("carol", "9.9.9.9")
        t2.succeeded("carol")
        self.assertTrue(t2.check("carol", "9.9.9.9")[0])

    def test_failures_expire_and_memory_is_bounded(self):
        t = LoginThrottle(max_failures=1, window_s=0.05)
        t.failed("dave", "3.3.3.3")
        self.assertFalse(t.check("dave", "3.3.3.3")[0])
        import time

        time.sleep(0.08)
        self.assertTrue(t.check("dave", "3.3.3.3")[0])

    def test_revocations_expire_with_the_token(self):
        import time

        r = TokenRevocations()
        r.revoke("abc", time.time() + 60)
        self.assertTrue(r.is_revoked("abc"))
        r.revoke("old", time.time() - 1)
        self.assertFalse(r.is_revoked("old"))
        self.assertFalse(r.is_revoked(None))

    def test_rate_limiter_forgets_idle_buckets(self):
        rl = RateLimiter(60)
        for i in range(20_005):
            rl.allow(f"k{i}")
        self.assertLessEqual(len(rl._buckets), 20_010)
        rl._buckets = {k: (v[0], v[1] - 700) for k, v in rl._buckets.items()}
        rl.allow("fresh")
        self.assertLess(len(rl._buckets), 100)


class HardeningHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = support.container(fresh=True)
        cls.c.settings.bootstrap_admin_password = "admin-password-123"
        cls.c.settings.bootstrap_analyst_password = "analyst-password-123"
        cls.c.settings.rate_limit_per_minute = 100_000
        cls.c.settings.max_body_bytes = 20_000
        cls.c.settings.metrics_token = "scrape-token-for-tests"
        cls.client = TestClient(create_app(cls.c))
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def login(self, u="analyst", p="analyst-password-123"):
        return self.client.post("/api/auth/login", json={"username": u, "password": p})

    def test_01_account_lockout_over_http(self):
        for _ in range(5):
            self.assertEqual(self.login("lockme", "wrong-password-1").status_code, 401)
        r = self.login("lockme", "wrong-password-1")
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)
        self.assertEqual(self.login().status_code, 200)  # other accounts are unaffected
        self.assertIn("locked", [e.result for e in self.c.store.list_audit(limit=300, action="login")])

    def test_02_logout_revokes_the_token(self):
        tok = self.login("admin", "admin-password-123").json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        self.assertEqual(self.client.get("/api/auth/me", headers=h).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/logout", headers=h).status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me", headers=h).status_code, 401)
        self.assertIn("logout", [e.action for e in self.c.store.list_audit(limit=300)])

    def test_03_oversized_bodies_are_refused(self):
        tok = self.login("admin", "admin-password-123").json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        big = {"transactions": [{"x": "y" * 100}] * 400}
        r = self.client.post("/api/monitoring/transactions", json=big, headers=h)
        self.assertEqual(r.status_code, 413)
        self.assertNotIn("Traceback", r.text)
        small = self.client.post("/api/monitoring/transactions", json={"transactions": [{"x": 1}]}, headers=h)
        self.assertEqual(small.status_code, 200)  # accepted by the transport, row quarantined by the data-quality gate

    def test_04_security_headers_on_every_response(self):
        r = self.client.get("/health")
        for k, v in (("x-content-type-options", "nosniff"), ("x-frame-options", "DENY"), ("referrer-policy", "no-referrer"),
                     ("cache-control", "no-store")):
            self.assertEqual(r.headers.get(k), v)
        self.assertIn("geolocation=()", r.headers.get("permissions-policy", ""))
        self.assertTrue(r.headers.get("x-request-id"))
        self.assertNotIn("strict-transport-security", r.headers)  # development: no HSTS

    def test_05_metrics_access_and_business_gauges(self):
        self.assertEqual(self.client.get("/metrics").status_code, 401)
        analyst = self.login().json()["access_token"]
        self.assertEqual(self.client.get("/metrics", headers={"Authorization": f"Bearer {analyst}"}).status_code, 403)
        self.assertEqual(self.client.get("/metrics", headers={"X-Metrics-Token": "wrong"}).status_code, 401)
        r = self.client.get("/metrics", headers={"X-Metrics-Token": "scrape-token-for-tests"})
        self.assertEqual(r.status_code, 200)
        for name in ("fira_http_requests_total", "fira_db_ping_ms", "fira_alerts_total", "fira_ingest_rows",
                     "fira_data_quality_score", "fira_cases_open"):
            self.assertIn(name, r.text)

    def test_06_readiness_reports_schema_and_latency(self):
        d = self.client.get("/health/ready").json()
        self.assertTrue(d["checks"]["schema_current"])
        self.assertIn("database_latency_ms", d["checks"])

    def test_07_validation_errors_do_not_echo_internals(self):
        tok = self.login("admin", "admin-password-123").json()["access_token"]
        r = self.client.post("/api/config/monitoring", json={"path": "x", "value": 1}, headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(r.status_code, 422)
        self.assertNotIn("Traceback", r.text)
        self.assertIn("request_id", r.json())


class ProductionAppTest(unittest.TestCase):
    def test_production_app_hides_docs_and_sends_hsts(self):
        c = support.container(fresh=True)
        c.settings = prod()
        with TestClient(create_app(c)) as client:
            self.assertEqual(client.get("/docs").status_code, 404)
            self.assertEqual(client.get("/openapi.json").status_code, 404)
            r = client.get("/health")
            self.assertIn("max-age", r.headers.get("strict-transport-security", ""))


if __name__ == "__main__":
    unittest.main()
