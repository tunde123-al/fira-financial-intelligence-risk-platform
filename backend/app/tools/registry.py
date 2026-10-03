"""Tool registry: the only path by which the agent (and MCP clients) touch data.

Every tool has: an input schema, an output schema, a permission boundary
(minimum role), a timeout, error handling, structured logging and an audit
record. Tool results are returned as `ToolResult` envelopes — tools never raise
into the agent.
"""
from __future__ import annotations

import concurrent.futures
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.observability import METRICS, Timer, log_event
from app.data.store import utcnow
from app.schemas.domain import AuditEvent
from app.security.principal import PermissionDenied, Principal

log = logging.getLogger("fira.tools")
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="tool")


@dataclass
class ToolContext:
    principal: Principal
    services: Any
    request_id: str | None = None
    investigation_id: str | None = None
    agent_run_id: str | None = None


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    func: Callable[[ToolContext, Any], Any]
    required_role: str = "analyst"
    timeout_s: float | None = None
    side_effects: bool = False
    audit_entity: Callable[[Any], tuple[str | None, str | None]] | None = None

    def json_schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "required_role": self.required_role,
                "side_effects": self.side_effects, "input_schema": self.input_model.model_json_schema(),
                "output_schema": self.output_model.model_json_schema()}


class ToolResult(BaseModel):
    tool: str
    ok: bool
    data: Any = None
    error: str | None = None
    error_type: str | None = None
    latency_ms: int = 0

    model_config = {"arbitrary_types_allowed": True}


@dataclass
class ToolRegistry:
    default_timeout_s: float = 20.0
    tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self.tools:
            raise ValueError(f"duplicate tool {spec.name}")
        self.tools[spec.name] = spec

    def list(self, principal: Principal | None = None) -> list[dict[str, Any]]:
        return [t.json_schema() for t in self.tools.values() if principal is None or principal.has(t.required_role)]

    def invoke(self, name: str, raw_input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        timer = Timer()
        spec = self.tools.get(name)
        entity_type = entity_id = None
        if spec is None:
            return self._finish(ctx, name, raw_input, ToolResult(tool=name, ok=False, error=f"unknown tool '{name}'",
                                                                  error_type="unknown_tool"), timer, None, None)
        try:
            ctx.principal.require(spec.required_role)
        except PermissionDenied as e:
            return self._finish(ctx, name, raw_input, ToolResult(tool=name, ok=False, error=str(e),
                                                                  error_type="permission_denied"), timer, None, None)
        try:
            inp = spec.input_model.model_validate(raw_input)
        except ValidationError as e:
            msg = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()[:5])
            return self._finish(ctx, name, raw_input, ToolResult(tool=name, ok=False, error=f"invalid input: {msg}",
                                                                  error_type="invalid_input"), timer, None, None)
        if spec.audit_entity:
            entity_type, entity_id = spec.audit_entity(inp)
        timeout = spec.timeout_s or self.default_timeout_s
        fut = _EXECUTOR.submit(spec.func, ctx, inp)
        try:
            out = fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # The worker thread cannot be killed; its result is discarded. Tools are read-mostly and
            # idempotent, and side-effecting tools write in a single transaction.
            res = ToolResult(tool=name, ok=False, error=f"timed out after {timeout:.0f}s", error_type="timeout")
            return self._finish(ctx, name, raw_input, res, timer, entity_type, entity_id)
        except LookupError as e:
            res = ToolResult(tool=name, ok=False, error=str(e).strip("'\""), error_type="not_found")
            return self._finish(ctx, name, raw_input, res, timer, entity_type, entity_id)
        except PermissionDenied as e:
            res = ToolResult(tool=name, ok=False, error=str(e), error_type="permission_denied")
            return self._finish(ctx, name, raw_input, res, timer, entity_type, entity_id)
        except Exception as e:  # never leak internals to callers; details go to logs
            log_event(log, "tool_exception", logging.ERROR, tool=name, exc_type=type(e).__name__, exc=str(e)[:500])
            res = ToolResult(tool=name, ok=False, error=f"internal error ({type(e).__name__})", error_type="internal")
            return self._finish(ctx, name, raw_input, res, timer, entity_type, entity_id)
        try:
            if not isinstance(out, spec.output_model):
                out = spec.output_model.model_validate(out)
        except ValidationError:
            res = ToolResult(tool=name, ok=False, error="tool returned malformed output", error_type="malformed_output")
            return self._finish(ctx, name, raw_input, res, timer, entity_type, entity_id)
        return self._finish(ctx, name, raw_input, ToolResult(tool=name, ok=True, data=out), timer, entity_type,
                            entity_id)

    def _finish(self, ctx: ToolContext, name: str, raw_input: dict[str, Any], res: ToolResult, timer: Timer,
                entity_type: str | None, entity_id: str | None) -> ToolResult:
        res.latency_ms = timer.ms
        METRICS.inc("fira_tool_calls_total", tool=name, ok=str(res.ok).lower())
        METRICS.observe("fira_tool_latency_ms", res.latency_ms, tool=name)
        if not res.ok:
            METRICS.inc("fira_tool_errors_total", tool=name, error_type=res.error_type or "unknown")
        log_event(log, "tool_call", tool=name, ok=res.ok, error_type=res.error_type, latency_ms=res.latency_ms,
                  principal=ctx.principal.user_id, via=ctx.principal.via)
        try:
            ctx.services.store.append_audit(AuditEvent(
                ts=utcnow(), user_id=ctx.principal.user_id, role=ctx.principal.role, action="tool_call", tool=name,
                entity_type=entity_type, entity_id=entity_id, result="ok" if res.ok else (res.error_type or "error"),
                request_id=ctx.request_id,
                details={"input": _summarize(raw_input), "latency_ms": res.latency_ms, "via": ctx.principal.via,
                         "investigation_id": ctx.investigation_id, "agent_run_id": ctx.agent_run_id,
                         **({"error": res.error} if res.error else {})}))
        except Exception as e:  # audit failure must be visible but must not break the call path
            log_event(log, "audit_write_failed", logging.ERROR, tool=name, exc_type=type(e).__name__)
            METRICS.inc("fira_audit_failures_total")
        return res


def _summarize(d: dict[str, Any], max_len: int = 200) -> dict[str, Any]:
    out = {}
    for k, v in (d or {}).items():
        s = v if isinstance(v, (int, float, bool)) or v is None else str(v)
        out[k] = s[:max_len] if isinstance(s, str) else s
    return out
