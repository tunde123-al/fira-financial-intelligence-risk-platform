"""Application configuration.

All configuration comes from environment variables (12-factor). Secrets are never
hard-coded: the defaults below are either non-secret or deliberately unusable
(e.g. an empty JWT secret makes the API refuse to start in production mode).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value is not None else default


class Settings(BaseModel):
    environment: Literal["development", "test", "production"] = "development"

    # --- storage -----------------------------------------------------------
    # DATA_BACKEND=postgres uses PostgreSQL; DATA_BACKEND=frames uses the
    # in-process pandas store built from the synthetic dataset files (used for
    # offline evaluation and for tests that must run without infrastructure).
    data_backend: Literal["postgres", "frames"] = "postgres"
    database_url: str | None = None
    dataset_dir: Path = REPO_ROOT / "data" / "seeds"

    graph_backend: Literal["neo4j", "networkx"] = "networkx"
    neo4j_uri: str | None = None
    neo4j_user: str | None = None
    neo4j_password: str | None = None

    vector_backend: Literal["qdrant", "memory"] = "memory"
    qdrant_url: str | None = None
    qdrant_api_key: str | None = None
    qdrant_collection: str = "fira_documents"

    documents_dir: Path = REPO_ROOT / "documents"
    model_dir: Path = REPO_ROOT / "data" / "models"
    risk_config_path: Path = BACKEND_ROOT / "app" / "risk" / "default_config.yaml"

    # --- LLM ---------------------------------------------------------------
    llm_provider: Literal["none", "anthropic", "openai", "ollama"] = "none"
    llm_model: str | None = None
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_timeout_s: int = 60
    llm_max_tokens: int = 1500
    # USD per million tokens, used only for cost reporting
    llm_cost_input_per_mtok: float = 0.0
    llm_cost_output_per_mtok: float = 0.0

    embedding_provider: Literal["lsa", "openai"] = "lsa"
    embedding_model: str | None = None

    # --- security ----------------------------------------------------------
    jwt_secret: str | None = None
    jwt_ttl_minutes: int = 60
    mcp_api_keys: str | None = None  # "key1:analyst,key2:admin"
    rate_limit_per_minute: int = 120
    cors_origins: str = "http://localhost:5173,http://localhost:8080"
    bootstrap_admin_password: str | None = None
    bootstrap_analyst_password: str | None = None

    # --- privacy -----------------------------------------------------------
    pii_masking: bool = True
    audit_retention_days: int = 365 * 5
    episode_retention_days: int = 365 * 2

    # --- agent -------------------------------------------------------------
    default_lookback_days: int = 30
    baseline_days: int = 90
    tool_timeout_s: float = 20.0
    agent_max_report_attempts: int = 2

    log_level: str = "INFO"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def mcp_key_roles(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for pair in (self.mcp_api_keys or "").split(","):
            if ":" in pair:
                key, role = pair.split(":", 1)
                out[key.strip()] = role.strip()
        return out


def load_settings() -> Settings:
    data: dict[str, Any] = {
        "environment": _env("FIRA_ENV", "development"),
        "data_backend": _env("DATA_BACKEND", "postgres"),
        "database_url": _env("DATABASE_URL"),
        "graph_backend": _env("GRAPH_BACKEND", "networkx"),
        "neo4j_uri": _env("NEO4J_URI"),
        "neo4j_user": _env("NEO4J_USER"),
        "neo4j_password": _env("NEO4J_PASSWORD"),
        "vector_backend": _env("VECTOR_BACKEND", "memory"),
        "qdrant_url": _env("QDRANT_URL"),
        "qdrant_api_key": _env("QDRANT_API_KEY"),
        "qdrant_collection": _env("QDRANT_COLLECTION", "fira_documents"),
        "llm_provider": _env("LLM_PROVIDER", "none"),
        "llm_model": _env("LLM_MODEL"),
        "llm_api_key": _env("LLM_API_KEY"),
        "llm_base_url": _env("LLM_BASE_URL"),
        "llm_timeout_s": _env_int("LLM_TIMEOUT_S", 60),
        "llm_max_tokens": _env_int("LLM_MAX_TOKENS", 1500),
        "llm_cost_input_per_mtok": float(_env("LLM_COST_INPUT_PER_MTOK", "0") or 0),
        "llm_cost_output_per_mtok": float(_env("LLM_COST_OUTPUT_PER_MTOK", "0") or 0),
        "embedding_provider": _env("EMBEDDING_PROVIDER", "lsa"),
        "embedding_model": _env("EMBEDDING_MODEL"),
        "jwt_secret": _env("JWT_SECRET"),
        "jwt_ttl_minutes": _env_int("JWT_TTL_MINUTES", 60),
        "mcp_api_keys": _env("MCP_API_KEYS"),
        "rate_limit_per_minute": _env_int("RATE_LIMIT_PER_MINUTE", 120),
        "cors_origins": _env("CORS_ORIGINS", "http://localhost:5173,http://localhost:8080"),
        "bootstrap_admin_password": _env("BOOTSTRAP_ADMIN_PASSWORD"),
        "bootstrap_analyst_password": _env("BOOTSTRAP_ANALYST_PASSWORD"),
        "pii_masking": _env_bool("PII_MASKING", True),
        "audit_retention_days": _env_int("AUDIT_RETENTION_DAYS", 365 * 5),
        "episode_retention_days": _env_int("EPISODE_RETENTION_DAYS", 365 * 2),
        "default_lookback_days": _env_int("DEFAULT_LOOKBACK_DAYS", 30),
        "baseline_days": _env_int("BASELINE_DAYS", 90),
        "tool_timeout_s": float(_env("TOOL_TIMEOUT_S", "20") or 20),
        "log_level": _env("LOG_LEVEL", "INFO"),
    }
    for key, env_name in (
        ("dataset_dir", "DATASET_DIR"),
        ("documents_dir", "DOCUMENTS_DIR"),
        ("model_dir", "MODEL_DIR"),
        ("risk_config_path", "RISK_CONFIG_PATH"),
    ):
        if _env(env_name):
            data[key] = Path(_env(env_name))  # type: ignore[arg-type]
    return Settings(**{k: v for k, v in data.items() if v is not None})


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


__all__ = ["Settings", "get_settings", "load_settings", "reset_settings_cache", "Field"]
