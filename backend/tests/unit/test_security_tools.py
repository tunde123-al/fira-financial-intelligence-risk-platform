import time
import unittest

from pydantic import BaseModel

from app.security.auth import (
    AuthenticationError,
    create_token,
    decode_token,
    hash_password,
    jwt_secret,
    verify_password,
)
from app.security.masking import mask_ip, mask_payload
from app.security.principal import PermissionDenied, Principal
from app.security.ratelimit import RateLimiter
from app.tools.registry import ToolContext, ToolRegistry, ToolSpec
from tests import support


class SecurityTest(unittest.TestCase):
    def test_password_hashing(self):
        h = hash_password("correct horse battery")
        self.assertTrue(verify_password("correct horse battery", h))
        self.assertFalse(verify_password("wrong password!!", h))
        self.assertFalse(verify_password("x", "garbage"))
        with self.assertRaises(ValueError):
            hash_password("short")

    def test_tokens(self):
        s = support.make_settings()
        tok = create_token(s, "U-1", "alice", "analyst")["access_token"]
        p = decode_token(s, tok)
        self.assertEqual((p.user_id, p.role), ("U-1", "analyst"))
        with self.assertRaises(AuthenticationError):
            decode_token(s, tok[:-2] + "xx")
        expired = support.make_settings(jwt_ttl_minutes=-1)
        with self.assertRaises(AuthenticationError):
            decode_token(expired, create_token(expired, "U-1", "alice", "analyst")["access_token"])
        other = support.make_settings(jwt_secret="another-secret-another-secret-another")
        with self.assertRaises(AuthenticationError):
            decode_token(other, tok)

    def test_production_requires_strong_secret(self):
        with self.assertRaises(RuntimeError):
            jwt_secret(support.make_settings(environment="production", jwt_secret=None))
        with self.assertRaises(RuntimeError):
            jwt_secret(support.make_settings(environment="production", jwt_secret="short"))

    def test_masking(self):
        self.assertEqual(mask_ip("102.49.10.200"), "102.49.x.x")
        out = mask_payload({"value_hash": "abc", "ip_address": "1.2.3.4", "fingerprint": "abcdef123456",
                            "nested": [{"password_hash": "h", "ok": 1}]})
        self.assertNotIn("value_hash", out)
        self.assertEqual(out["ip_address"], "1.2.x.x")
        self.assertEqual(out["fingerprint"], "abcdef…")
        self.assertEqual(out["nested"], [{"ok": 1}])
        self.assertEqual(mask_payload({"ip_address": "1.2.3.4"}, enabled=False)["ip_address"], "1.2.3.4")

    def test_rate_limiter(self):
        rl = RateLimiter(per_minute=60, burst=2)
        self.assertTrue(rl.allow("k")[0])
        self.assertTrue(rl.allow("k")[0])
        ok, retry = rl.allow("k")
        self.assertFalse(ok)
        self.assertGreater(retry, 0)
        self.assertTrue(rl.allow("other")[0])

    def test_roles(self):
        self.assertTrue(Principal("a", "admin").has("analyst"))
        self.assertFalse(Principal("a", "analyst").has("admin"))
        with self.assertRaises(PermissionDenied):
            Principal("a", "analyst").require("admin")


class In(BaseModel):
    x: int


class Out(BaseModel):
    y: int


class _Store:
    def __init__(self):
        self.audit = []

    def append_audit(self, e):
        self.audit.append(e)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        self.reg = ToolRegistry(default_timeout_s=0.5)
        self.reg.register(ToolSpec("ok", "", In, Out, lambda c, i: Out(y=i.x * 2)))
        self.reg.register(ToolSpec("admin_only", "", In, Out, lambda c, i: Out(y=1), required_role="admin"))
        self.reg.register(ToolSpec("slow", "", In, Out, lambda c, i: (time.sleep(2), Out(y=1))[1]))
        self.reg.register(ToolSpec("bad_output", "", In, Out, lambda c, i: {"nope": True}))
        self.reg.register(ToolSpec("boom", "", In, Out, lambda c, i: 1 / 0))
        self.reg.register(ToolSpec("missing", "", In, Out, lambda c, i: (_ for _ in ()).throw(LookupError("x gone"))))
        svc = type("S", (), {"store": self.store})()
        self.ctx = ToolContext(principal=Principal("u", "analyst"), services=svc)

    def test_success_and_audit(self):
        r = self.reg.invoke("ok", {"x": 2}, self.ctx)
        self.assertTrue(r.ok)
        self.assertEqual(r.data.y, 4)
        self.assertEqual(self.store.audit[-1].tool, "ok")
        self.assertEqual(self.store.audit[-1].result, "ok")

    def test_failures_are_enveloped(self):
        cases = {"admin_only": ({"x": 1}, "permission_denied"), "ok": ({"x": "nan"}, "invalid_input"),
                 "slow": ({"x": 1}, "timeout"), "bad_output": ({"x": 1}, "malformed_output"),
                 "boom": ({"x": 1}, "internal"), "missing": ({"x": 1}, "not_found"),
                 "nope": ({}, "unknown_tool")}
        for tool, (args, et) in cases.items():
            r = self.reg.invoke(tool, args, self.ctx)
            self.assertFalse(r.ok, tool)
            self.assertEqual(r.error_type, et, tool)
        self.assertTrue(all(a.result != "ok" for a in self.store.audit))

    def test_internal_errors_do_not_leak_details(self):
        r = self.reg.invoke("boom", {"x": 1}, self.ctx)
        self.assertEqual(r.error, "internal error (ZeroDivisionError)")


if __name__ == "__main__":
    unittest.main()
