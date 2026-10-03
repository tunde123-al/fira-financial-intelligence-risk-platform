"""Risk configuration model, loading and versioning."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class SignalConfig(BaseModel):
    weight: float = Field(ge=0, le=100)
    group: str | None = None
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)

    def p(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)


class ScoreConfig(BaseModel):
    cap: float = 100
    bands: dict[str, float] = Field(default_factory=lambda: {"low": 0.0, "medium": 25.0, "high": 50.0, "critical": 75.0})
    investigation_threshold: float = 40
    ramp: float = Field(default=1.0, gt=0)


class RiskConfig(BaseModel):
    version: str = "unversioned"
    score: ScoreConfig = Field(default_factory=ScoreConfig)
    group_caps: dict[str, float] = Field(default_factory=dict)
    signals: dict[str, SignalConfig]
    high_risk_jurisdictions: list[str] = Field(default_factory=list)

    @field_validator("signals", mode="before")
    @classmethod
    def _split_params(cls, v: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, cfg in (v or {}).items():
            if isinstance(cfg, SignalConfig):
                out[name] = cfg
                continue
            cfg = dict(cfg)
            base = {k: cfg.pop(k) for k in ("weight", "group", "enabled") if k in cfg}
            params = cfg.pop("params", {})
            params.update(cfg)
            out[name] = {**base, "params": params}
        return out

    def signal(self, name: str) -> SignalConfig | None:
        s = self.signals.get(name)
        return s if s and s.enabled else None

    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(exclude={"version"}), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    def band(self, score: float) -> str:
        best = "low"
        for name, lo in sorted(self.score.bands.items(), key=lambda kv: kv[1]):
            if score >= lo:
                best = name
        return best


def load_risk_config(path: Path) -> RiskConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return RiskConfig(**data)


def to_yaml(cfg: RiskConfig) -> str:
    d = cfg.model_dump()
    sigs = {}
    for name, s in d["signals"].items():
        sigs[name] = {"weight": s["weight"], **({"group": s["group"]} if s["group"] else {}),
                      **({} if s["enabled"] else {"enabled": False}), **s["params"]}
    d["signals"] = sigs
    return yaml.safe_dump(d, sort_keys=False)
