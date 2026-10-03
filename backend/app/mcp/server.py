"""MCP server exposing FIRA investigator tools.

Every MCP tool delegates to the same `ToolRegistry` used by the agent, so input
validation, role checks, timeouts and audit logging are identical. The caller's
role comes from the API key presented at startup (FIRA_MCP_API_KEY), mapped via
MCP_API_KEYS="key1:analyst,key2:admin". Without a valid key the server refuses
to start.

Run (stdio transport, e.g. from an MCP client config):
    FIRA_MCP_API_KEY=... python -m app.mcp.server
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any

from app.security.principal import Principal

try:  # MCP Python SDK >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - MCP SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef,attr-defined]


class AuthError(PermissionError):
    pass


def principal_from_key(api_key: str | None, key_roles: dict[str, str]) -> Principal:
    if not api_key or api_key not in key_roles:
        raise AuthError("invalid or missing MCP API key")
    key_id = hashlib.sha256(api_key.encode()).hexdigest()[:10]
    return Principal(user_id=f"mcp:{key_id}", role=key_roles[api_key], via="mcp")


def build_server(container: Any, principal: Principal) -> Any:
    from app.tools.registry import ToolContext

    server = _Server(name="fira", instructions=(
        "Financial Intelligence & Risk Agent tools. All outputs are decision support for authorised investigators; "
        "they are not determinations of wrongdoing. Data is synthetic in development deployments."))

    def call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        res = container.registry.invoke(tool, args, ToolContext(principal=principal, services=container))
        if not res.ok:
            return {"ok": False, "error": res.error, "error_type": res.error_type}
        data = res.data.model_dump(mode="json") if hasattr(res.data, "model_dump") else res.data
        from app.security.masking import mask_payload

        return {"ok": True, "data": mask_payload(data, container.settings.pii_masking)}

    @server.tool(name="customer_lookup", description="Customer profile with masked identifiers and accounts.")
    def customer_lookup(customer_id: str) -> dict[str, Any]:
        return call("get_customer", {"customer_id": customer_id})

    @server.tool(name="transaction_analysis",
                 description="Behavioural statistics (window vs baseline) and transaction-level anomalies.")
    def transaction_analysis(customer_id: str, lookback_days: int = 30) -> dict[str, Any]:
        stats = call("get_transaction_statistics", {"customer_id": customer_id, "lookback_days": lookback_days})
        anomalies = call("detect_anomalies", {"customer_id": customer_id, "lookback_days": lookback_days})
        return {"statistics": stats, "anomalies": anomalies}

    @server.tool(name="graph_search", description="Bounded relationship neighbourhood and shared devices of an entity.")
    def graph_search(kind: str, entity_id: str, depth: int = 2) -> dict[str, Any]:
        out = {"neighbourhood": call("get_related_entities", {"kind": kind, "entity_id": entity_id, "depth": depth,
                                                               "limit": 150})}
        if kind == "customer":
            out["shared_devices"] = call("find_shared_device", {"customer_id": entity_id})
        return out

    @server.tool(name="risk_analysis", description="Deterministic risk signals and explainable score.")
    def risk_analysis(entity_id: str, entity_type: str = "customer", lookback_days: int = 30) -> dict[str, Any]:
        return call("get_risk_signals", {"entity_type": entity_type, "entity_id": entity_id,
                                         "lookback_days": lookback_days})

    @server.tool(name="document_search", description="Hybrid search over policies and procedures with provenance.")
    def document_search(query: str, k: int = 5) -> dict[str, Any]:
        return call("search_documents", {"query": query, "k": k})

    @server.tool(name="investigation_history", description="Previous investigations and analyst decisions.")
    def investigation_history(subject_ids: list[str]) -> dict[str, Any]:
        return call("get_previous_investigations", {"subject_ids": subject_ids, "limit": 20})

    return server


def main() -> None:
    from app.config import get_settings
    from app.services.container import build_container

    settings = get_settings()
    try:
        principal = principal_from_key(os.environ.get("FIRA_MCP_API_KEY"), settings.mcp_key_roles())
    except AuthError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        raise SystemExit(2) from None
    server = build_server(build_container(), principal)
    server.run()  # stdio transport


if __name__ == "__main__":
    main()
