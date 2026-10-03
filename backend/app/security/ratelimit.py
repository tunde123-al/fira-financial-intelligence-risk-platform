"""In-process token-bucket rate limiter.

Per-process state: with several API replicas, put a shared limiter (e.g. Redis or
the API gateway / load balancer) in front instead; see SECURITY.md.
"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, per_minute: int, burst: int | None = None):
        self.rate = per_minute / 60.0
        self.capacity = float(burst or max(per_minute // 4, 10))
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= cost:
                self._buckets[key] = (tokens - cost, now)
                return True, 0.0
            self._buckets[key] = (tokens, now)
            return False, (cost - tokens) / self.rate if self.rate else 60.0
