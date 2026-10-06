"""Forwarded headers must never be trusted blindly: a client cannot choose the address used for lockout or rate limits."""
import re
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.main import create_app
from tests import support

ROOT = Path(__file__).resolve().parents[3]


class DeploymentCommandTest(unittest.TestCase):
    def test_no_blanket_proxy_trust_anywhere_in_deployment_files(self):
        for f in ("infrastructure/docker/render.Dockerfile", "infrastructure/docker/backend.Dockerfile", "render.yaml",
                  "docker-compose.yml"):
            text = (ROOT / f).read_text(encoding="utf-8")
            self.assertFalse(re.search(r"forwarded-allow-ips[= ]+['\"]?\*", text, re.I), f)
            self.assertNotRegex(text, r"FORWARDED_ALLOW_IPS\s*[:=]\s*['\"]?\*")


class SpoofedForwardedForTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = support.container(fresh=True)
        cls.c.settings.bootstrap_analyst_password = "analyst-password-123"
        cls.c.settings.rate_limit_per_minute = 100_000

    def test_lockout_cannot_be_evaded_by_rotating_the_header(self):
        with TestClient(create_app(self.c)) as client:  # uvicorn's default trust is 127.0.0.1 only: this peer is not trusted
            codes = [client.post("/api/auth/login", json={"username": "victim", "password": "wrong-password-1"},
                                 headers={"X-Forwarded-For": f"203.0.113.{i}"}).status_code for i in range(7)]
        self.assertEqual(codes[:5], [401] * 5)
        self.assertIn(429, codes[5:])

    def test_untrusted_peer_cannot_change_the_client_address(self):
        seen = {}

        async def app(scope, receive, send):
            seen["client"] = scope.get("client")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        wrapped = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1,::1")  # the default when nothing is configured
        TestClient(wrapped).get("/", headers={"X-Forwarded-For": "6.6.6.6", "X-Forwarded-Proto": "https"})
        self.assertNotEqual(seen["client"][0], "6.6.6.6")

    def test_a_configured_proxy_is_honoured_and_a_forged_left_entry_is_ignored(self):
        seen = {}

        async def app(scope, receive, send):
            seen["client"], seen["scheme"] = scope.get("client"), scope.get("scheme")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        wrapped = ProxyHeadersMiddleware(app, trusted_hosts="testclient")  # as FORWARDED_ALLOW_IPS=<proxy> would
        TestClient(wrapped).get("/", headers={"X-Forwarded-For": "6.6.6.6, 198.51.100.7, testclient",
                                              "X-Forwarded-Proto": "https"})
        self.assertEqual(seen["client"][0], "198.51.100.7")  # the entry added by the trusted proxy, not the forged one
        self.assertEqual(seen["scheme"], "https")


if __name__ == "__main__":
    unittest.main()
