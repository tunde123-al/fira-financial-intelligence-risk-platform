"""Sensitive-field masking applied at the API boundary."""
from __future__ import annotations

import ipaddress
from typing import Any

SENSITIVE_KEYS = {"value_hash", "fingerprint", "password_hash"}


def mask_ip(ip: str | None) -> str | None:
    if not ip:
        return ip
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "***"
    if addr.version == 4:
        parts = str(addr).split(".")
        return ".".join(parts[:2] + ["x", "x"])
    return str(addr).split(":")[0] + ":****"


def mask_payload(obj: Any, enabled: bool = True) -> Any:
    """Recursively drop secret-like keys and mask IP addresses / fingerprints."""
    if not enabled:
        return obj
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in SENSITIVE_KEYS:
                if k == "fingerprint" and isinstance(v, str):
                    out[k] = v[:6] + "…"
                continue
            if k == "ip_address":
                out[k] = mask_ip(v) if isinstance(v, str) else v
            else:
                out[k] = mask_payload(v, enabled)
        return out
    if isinstance(obj, list):
        return [mask_payload(v, enabled) for v in obj]
    return obj
