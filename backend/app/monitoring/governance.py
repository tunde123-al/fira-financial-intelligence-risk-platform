"""Configuration governance: every change to a governed setting leaves a record.

Governed configurations are the monitoring configuration (alert thresholds, triage thresholds, mule bands, data-quality
rules, disabled detectors) and the risk configuration (signal weights, enabled flags, bands). A change record holds the
configuration name, the setting path, old value, new value, who changed it, when, why and by which route.

How changes are detected:

* API changes (`apply_override`, detector toggle, approved risk-config version) write their records directly, with the
  acting user and the reason they supplied.
* Edits made to the YAML files between runs are found at start-up by comparing a canonical snapshot with the last
  recorded one. They are attributed to "system" with source "startup": the process cannot know who edited a file.
  The first snapshot is a baseline and produces no change rows.

Runtime overrides apply to the running process only; the YAML file stays the source of truth, so a restart reverts an
override and that revert is itself recorded. The log is append-only (database triggers in PostgreSQL).
"""
from __future__ import annotations

import copy
from typing import Any

from app.data.store import utcnow
from app.monitoring.models import ConfigChange

# paths (and their sub-paths) that may be changed at runtime through the API
OVERRIDABLE = (
    "triage.thresholds", "triage.overdue_hours", "triage.enabled", "mule.bands", "mule.fan_in_min", "mule.fan_out_min",
    "mule.rapid_hours", "mule.rapid_share", "mule.retention_max", "mule.min_usd",
    "alerting.supporting_min_customer_score", "alerting.min_detector_points", "alerting.disabled_detectors",
    "deduplication.resolved_cooldown_days", "data_quality.late_after_hours", "data_quality.max_future_hours",
    "data_quality.likely_duplicate_window_seconds", "data_quality.duplicate_policy",
    "data_quality.reject_closed_accounts",
)
SNAPSHOT_PATH = "(snapshot)"


def snapshots(c: Any) -> dict[str, dict[str, Any]]:
    return {"monitoring": c.monitoring_config.model_dump(mode="json"), "risk": c.risk_config.model_dump(mode="json")}


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Leaf values by dotted path. Lists are compared as whole values."""
    if isinstance(value, dict) and value:
        out: dict[str, Any] = {}
        for k, v in value.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    return {prefix: value}


def diff(old: dict[str, Any], new: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    a, b = flatten(old), flatten(new)
    return [(p, a.get(p), b.get(p)) for p in sorted(set(a) | set(b)) if a.get(p) != b.get(p)]


def record(repo: Any, name: str, old: dict[str, Any] | None, new: dict[str, Any], changed_by: str, source: str,
           reason: str | None = None) -> list[ConfigChange]:
    """Write change rows for `old` -> `new` (nothing when old is None: baseline) and a fresh snapshot row."""
    now = utcnow()
    rows = [ConfigChange(ts=now, config_name=name, path=p, old_value=o, new_value=n, changed_by=changed_by,
                         reason=reason, source=source) for p, o, n in diff(old, new)] if old is not None else []
    for r in rows:
        repo.add_config_change(r)
    if old is None or rows:
        repo.add_config_change(ConfigChange(ts=now, config_name=name, path=SNAPSHOT_PATH, new_value=new,
                                            changed_by=changed_by, reason="baseline" if old is None else reason,
                                            source=source if old is not None else "baseline"))
    return rows


def sync(c: Any, changed_by: str = "system", source: str = "startup", reason: str | None = None) -> dict[str, int]:
    """Compare the live configuration with the last recorded snapshot and record the differences."""
    out: dict[str, int] = {}
    for name, snap in snapshots(c).items():
        last = c.monitoring_repo.last_config_snapshot(name)
        out[name] = len(record(c.monitoring_repo, name, last, snap, changed_by, source, reason))
    return out


def _set_path(tree: dict[str, Any], path: str, value: Any) -> None:
    keys = path.split(".")
    node = tree
    for k in keys[:-1]:
        if not isinstance(node.get(k), dict):
            raise ValueError(f"unknown setting {path}")
        node = node[k]
    if keys[-1] not in node:
        raise ValueError(f"unknown setting {path}")
    node[keys[-1]] = value


def overridable(path: str) -> bool:
    return any(path == p or path.startswith(p + ".") for p in OVERRIDABLE)


def apply_override(c: Any, path: str, value: Any, reason: str, actor_id: str) -> dict[str, Any]:
    """Change one monitoring setting in the running process, validated, with a change record."""
    from app.monitoring.config import MonitoringConfig

    if not overridable(path):
        raise ValueError(f"{path} cannot be changed at runtime; allowed: {', '.join(OVERRIDABLE)}")
    if not reason or len(reason.strip()) < 5:
        raise ValueError("a reason of at least 5 characters is required")
    old_dump = c.monitoring_config.model_dump(mode="json")
    new_dump = copy.deepcopy(old_dump)
    _set_path(new_dump, path, value)
    new_cfg = MonitoringConfig(**new_dump)  # validation (pydantic) rejects out-of-range or malformed values
    changed = diff(old_dump, new_cfg.model_dump(mode="json"))
    if not changed:
        raise ValueError("value is unchanged")
    rows = record(c.monitoring_repo, "monitoring", old_dump, new_cfg.model_dump(mode="json"), actor_id, "api",
                  reason.strip())
    c.monitoring_config = new_cfg
    return {"changed": [{"path": r.path, "old": r.old_value, "new": r.new_value} for r in rows],
            "fingerprint": new_cfg.fingerprint(),
            "note": "applies to the running process; the YAML file is unchanged and a restart reverts it"}


def set_detector_enabled(c: Any, detector_id: str, enabled: bool, reason: str, actor_id: str) -> dict[str, Any]:
    from app.risk.catalog import CATALOG

    if detector_id not in CATALOG:
        raise LookupError(f"unknown detector {detector_id}")
    disabled = set(c.monitoring_config.alerting.disabled_detectors)
    disabled = disabled - {detector_id} if enabled else disabled | {detector_id}
    return apply_override(c, "alerting.disabled_detectors", sorted(disabled), reason, actor_id)
