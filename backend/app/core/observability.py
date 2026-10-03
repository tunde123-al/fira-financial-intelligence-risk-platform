"""Structured JSON logging, request-scoped context and in-process metrics.

Context variables carry request_id / user_id / investigation_id / agent_run_id so
every log line emitted while serving a request (including from tools and the
agent) is correlated without passing ids through every call.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_id", default=None)
investigation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("investigation_id", default=None)
agent_run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("agent_run_id", default=None)

_CTX_VARS = {"request_id": request_id_var, "user_id": user_id_var, "investigation_id": investigation_id_var,
             "agent_run_id": agent_run_id_var}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname, "logger": record.name, "msg": record.getMessage(),
        }
        for k, var in _CTX_VARS.items():
            v = var.get()
            if v:
                payload[k] = v
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        root.setLevel(level)
        return
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root.handlers = [h]
    root.setLevel(level)


def log_event(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, msg, extra={"fields": fields})


@contextmanager
def bind(**values: str | None) -> Iterator[None]:
    tokens = []
    for k, v in values.items():
        if k in _CTX_VARS:
            tokens.append((_CTX_VARS[k], _CTX_VARS[k].set(v)))
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


class Metrics:
    """Minimal thread-safe counters/histograms exposed in Prometheus text format."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self.sums: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, int]] = {}

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self.counters[key] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            s, n = self.sums.get(key, (0.0, 0))
            self.sums[key] = (s + value, n + 1)

    def render(self) -> str:
        lines = []
        with self._lock:
            for (name, labels), v in sorted(self.counters.items()):
                lines.append(f"{name}{_labels(labels)} {v}")
            for (name, labels), (s, n) in sorted(self.sums.items()):
                lines.append(f"{name}_sum{_labels(labels)} {s}")
                lines.append(f"{name}_count{_labels(labels)} {n}")
        return "\n".join(lines) + "\n"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"counters": {f"{n}{_labels(lb)}": v for (n, lb), v in self.counters.items()},
                    "timers": {f"{n}{_labels(lb)}": {"sum": s, "count": c, "avg": s / c if c else 0}
                               for (n, lb), (s, c) in self.sums.items()}}


def _labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}"


METRICS = Metrics()


class Timer:
    def __init__(self) -> None:
        self.t0 = time.perf_counter()

    @property
    def ms(self) -> int:
        return int((time.perf_counter() - self.t0) * 1000)
