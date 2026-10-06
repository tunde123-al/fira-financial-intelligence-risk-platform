"""OpenAPI tag assignment: every operation gets exactly one documented tag, chosen from its path."""
from __future__ import annotations

from typing import Any

# (path prefix, tag) checked in order; the first match wins
RULES: list[tuple[str, str]] = [
    ("/health", "health"), ("/ready", "health"), ("/metrics", "metrics"),
    ("/api/auth", "auth"), ("/api/audit", "audit"),
    ("/api/monitoring/transactions", "transactions"), ("/api/transactions", "transactions"),
    ("/api/monitoring/triage", "triage"),
    ("/api/monitoring/alerts", "alerts"), ("/api/monitoring/my-work", "alerts"),
    ("/api/monitoring/quality", "alerts"), ("/api/monitoring/feedback", "alerts"), ("/api/alerts", "alerts"),
    ("/api/monitoring", "monitoring"),
    ("/api/cases", "cases"),
    ("/api/mule", "investigations"), ("/api/investigations", "investigations"),
    ("/api/evidence", "evidence"),
    ("/api/network", "graph"), ("/api/graph", "graph"),
    ("/api/copilot", "copilot"), ("/api/data-quality", "data-quality"), ("/api/config", "config"),
    ("/api/evaluation", "evaluation"), ("/api/improvement", "evaluation"),
    ("/api/risk", "risk"),
]
DEFAULT_TAG = "investigations"


def tag_for(path: str) -> str:
    if "/risk" in path and path.startswith("/api/customers"):
        return "risk"
    for prefix, tag in RULES:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix):
            return tag
    return DEFAULT_TAG


def retag(schema: dict[str, Any]) -> dict[str, Any]:
    for path, item in schema.get("paths", {}).items():
        for method, op in item.items():
            if method in ("get", "post", "put", "patch", "delete") and isinstance(op, dict):
                op["tags"] = [tag_for(path)]
    return schema
