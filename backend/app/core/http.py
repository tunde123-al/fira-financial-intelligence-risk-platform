"""Outbound HTTP helpers."""
from __future__ import annotations

from urllib.parse import urlparse


def require_http_url(url: str) -> str:
    """Reject non-HTTP(S) schemes (e.g. file:) before any urllib call."""
    scheme = urlparse(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"only http(s) URLs are allowed, got {scheme!r}")
    return url
