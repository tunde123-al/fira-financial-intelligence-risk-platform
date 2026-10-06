"""Deployment-related behaviour: database URL normalisation and optional static serving of the built UI."""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings, normalize_database_url
from app.main import create_app
from tests import support


class DatabaseUrlTest(unittest.TestCase):
    def test_managed_postgres_urls_get_the_driver(self):
        self.assertEqual(normalize_database_url("postgres://u:p@h:5432/db"), "postgresql+psycopg://u:p@h:5432/db")
        self.assertEqual(normalize_database_url("postgresql://u:p@h/db"), "postgresql+psycopg://u:p@h/db")

    def test_explicit_drivers_and_empty_values_are_untouched(self):
        self.assertEqual(normalize_database_url("postgresql+psycopg://u:p@h/db"), "postgresql+psycopg://u:p@h/db")
        self.assertEqual(normalize_database_url("sqlite:///x.db"), "sqlite:///x.db")
        self.assertIsNone(normalize_database_url(None))
        self.assertEqual(normalize_database_url(""), "")

    def test_settings_normalise_on_load(self):
        self.assertEqual(Settings(database_url="postgres://u:p@h/db").database_url, "postgresql+psycopg://u:p@h/db")


class StaticFrontendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dist = Path(tempfile.mkdtemp(prefix="fira-dist-"))
        (cls.dist / "index.html").write_text("<!doctype html><title>FIRA test ui</title><div id=root></div>", encoding="utf-8")
        (cls.dist / "app.js").write_text("console.log('x')", encoding="utf-8")
        cls.c = support.container(fresh=True)
        cls.c.settings.frontend_dist_dir = cls.dist
        cls.client = TestClient(create_app(cls.c))
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_ui_is_served_at_root_with_a_content_security_policy(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("FIRA test ui", r.text)
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(self.client.get("/app.js").status_code, 200)

    def test_api_routes_and_docs_keep_precedence_over_the_static_catch_all(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/api/monitoring/alerts").status_code, 401)  # real route, not the UI
        docs = self.client.get("/docs")
        self.assertEqual(docs.status_code, 200)
        self.assertNotIn("Content-Security-Policy", docs.headers)  # Swagger UI loads assets from a CDN
        self.assertNotIn("Content-Security-Policy", self.client.get("/api/monitoring/alerts").headers)
        self.assertEqual(self.client.get("/ready").status_code, 200)

    def test_unknown_static_paths_are_404_not_an_api_response(self):
        self.assertEqual(self.client.get("/definitely-missing.js").status_code, 404)

    def test_without_a_dist_dir_nothing_is_mounted(self):
        c = support.container(fresh=True)
        with TestClient(create_app(c)) as client:
            self.assertEqual(client.get("/").status_code, 404)
            self.assertNotIn("Content-Security-Policy", client.get("/health").headers)


if __name__ == "__main__":
    unittest.main()
