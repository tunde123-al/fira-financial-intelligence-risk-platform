"""Request-level hardening: login throttling, token revocation, body-size limit, production settings checks.

All state here is per process (in memory). That is adequate for the single-instance deployment this project targets;
with several instances each would need a shared store (Redis or PostgreSQL). docs/SECURITY.md states this limit.
"""
from __future__ import annotations

import threading
import time
from typing import Any

MAX_TRACKED = 10_000


class LoginThrottle:
    """Locks a username (and a client address) out after repeated failed logins within a window."""

    def __init__(self, max_failures: int = 5, window_s: float = 900.0, ip_factor: int = 4):
        self.max, self.window, self.ip_factor = max_failures, window_s, ip_factor
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        xs = [t for t in self._fails.get(key, []) if now - t < self.window]
        if xs:
            self._fails[key] = xs
        else:
            self._fails.pop(key, None)
        return xs

    def check(self, username: str, ip: str) -> tuple[bool, float]:
        """(allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            for key, limit in ((f"u:{username.lower()}", self.max), (f"i:{ip}", self.max * self.ip_factor)):
                xs = self._recent(key, now)
                if len(xs) >= limit:
                    return False, max(self.window - (now - xs[0]), 1.0)
        return True, 0.0

    def failed(self, username: str, ip: str) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._fails) > MAX_TRACKED:  # bounded memory: drop expired entries, then the oldest
                for k in list(self._fails):
                    self._recent(k, now)
                while len(self._fails) > MAX_TRACKED:
                    self._fails.pop(next(iter(self._fails)))
            for key in (f"u:{username.lower()}", f"i:{ip}"):
                self._fails.setdefault(key, []).append(now)

    def succeeded(self, username: str) -> None:
        with self._lock:
            self._fails.pop(f"u:{username.lower()}", None)


class TokenRevocations:
    """Revoked token ids (`jti`) until the token would have expired anyway."""

    def __init__(self) -> None:
        self._rev: dict[str, float] = {}
        self._lock = threading.Lock()

    def revoke(self, jti: str, exp_epoch: float) -> None:
        with self._lock:
            now = time.time()
            if len(self._rev) > MAX_TRACKED:
                self._rev = {k: v for k, v in self._rev.items() if v > now}
            self._rev[jti] = exp_epoch

    def is_revoked(self, jti: str | None) -> bool:
        if not jti:
            return False
        with self._lock:
            exp = self._rev.get(jti)
            if exp is not None and exp <= time.time():
                del self._rev[jti]
                return False
            return exp is not None

    def __len__(self) -> int:
        return len(self._rev)


class BodyLimitMiddleware:
    """Pure ASGI middleware: refuse request bodies larger than `max_bytes` (413), declared or streamed."""

    def __init__(self, app: Any, max_bytes: int):
        self.app, self.max = app, max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = next((v for k, v in scope.get("headers", []) if k == b"content-length"), None)
        if declared is not None:
            try:
                too_big = int(declared) > self.max
            except ValueError:
                too_big = True
            if too_big:
                await self._reject(send)
                return
        seen = 0
        rejected = False

        async def limited() -> Any:
            nonlocal seen, rejected
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max:
                    rejected = True
                    return {"type": "http.disconnect"}
            return msg

        started = False

        async def guard(message: Any) -> None:
            nonlocal started
            if rejected and not started:
                started = True
                await self._reject(send)
                return
            if rejected:
                return
            started = True
            await send(message)

        await self.app(scope, limited, guard)

    @staticmethod
    async def _reject(send: Any) -> None:
        body = b'{"detail":"request body too large"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


WEAK_SECRETS = {"changeme", "change-me", "secret", "password", "admin", "dev", "development", "test"}


def production_problems(s: Any) -> list[str]:
    """Reasons the settings are unsafe for FIRA_ENV=production. Empty list = acceptable."""
    problems: list[str] = []
    if not s.is_production:
        return problems
    if not s.jwt_secret or len(s.jwt_secret) < 32 or s.jwt_secret.lower() in WEAK_SECRETS:
        problems.append("JWT_SECRET must be set to a random value of at least 32 characters")
    if s.data_backend != "postgres" or not s.database_url:
        problems.append("production requires DATA_BACKEND=postgres with DATABASE_URL (the in-memory store has no persistence)")
    if "*" in s.cors_origin_list():
        problems.append("CORS_ORIGINS must not contain '*'")
    for name, pw in (("BOOTSTRAP_ADMIN_PASSWORD", s.bootstrap_admin_password),
                     ("BOOTSTRAP_ANALYST_PASSWORD", s.bootstrap_analyst_password)):
        if pw and (len(pw) < 12 or pw.lower() in WEAK_SECRETS):
            problems.append(f"{name} is shorter than 12 characters or a well-known value")
    if s.enable_api_docs:
        problems.append("ENABLE_API_DOCS must be off in production (set it only for a deliberate public demo)")
    if s.log_level.upper() == "DEBUG":
        problems.append("LOG_LEVEL=DEBUG is not allowed in production")
    return problems
