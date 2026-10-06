"""OpenAPI documentation: every operation has exactly one documented tag and the contract lists the new surface."""
import unittest

from fastapi.testclient import TestClient

from app.api.tags import tag_for
from app.main import OPENAPI_TAGS, create_app
from tests import support

REQUIRED_TAGS = {"transactions", "monitoring", "alerts", "triage", "risk", "cases", "investigations", "evidence", "graph",
                 "evaluation", "health", "metrics"}


class OpenApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(create_app(support.container(fresh=True)))
        cls.schema = cls.client.get("/openapi.json").json()

    def test_every_operation_has_exactly_one_documented_tag(self):
        documented = {t["name"] for t in OPENAPI_TAGS}
        seen = set()
        for path, item in self.schema["paths"].items():
            for method, op in item.items():
                if method in ("get", "post", "put", "patch", "delete"):
                    self.assertEqual(len(op["tags"]), 1, (method, path, op["tags"]))
                    self.assertIn(op["tags"][0], documented, (method, path))
                    seen.add(op["tags"][0])
        self.assertTrue(REQUIRED_TAGS <= documented)
        self.assertTrue({"transactions", "alerts", "triage", "cases", "graph", "metrics", "health", "monitoring"} <= seen, seen)

    def test_new_endpoints_are_documented_under_the_right_tags(self):
        paths = self.schema["paths"]
        for path, method, tag in [
            ("/api/monitoring/transactions", "post", "transactions"), ("/api/monitoring/triage/{alert_id}", "get", "triage"),
            ("/api/monitoring/quality", "get", "alerts"), ("/api/monitoring/my-work", "get", "alerts"),
            ("/api/data-quality/summary", "get", "data-quality"), ("/api/config/changes", "get", "config"),
            ("/api/mule/customers/{customer_id}", "get", "investigations"), ("/api/cases/{case_id}/priority", "post", "cases"),
            ("/metrics", "get", "metrics"), ("/health", "get", "health"), ("/api/auth/logout", "post", "auth")]:
            self.assertIn(path, paths, path)
            self.assertEqual(paths[path][method]["tags"], [tag], path)

    def test_validation_and_auth_errors_are_documented_via_the_standard_schema(self):
        self.assertIn("HTTPValidationError", self.schema["components"]["schemas"])

    def test_tag_mapping_defaults(self):
        self.assertEqual(tag_for("/api/network/common-recipients"), "graph")
        self.assertEqual(tag_for("/api/customers/CUST-1/risk"), "risk")
        self.assertEqual(tag_for("/api/something-new"), "investigations")


if __name__ == "__main__":
    unittest.main()
