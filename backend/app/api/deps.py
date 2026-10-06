"""FastAPI dependencies: container access, authentication, authorization, auditing."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.observability import request_id_var, user_id_var
from app.data.store import utcnow
from app.schemas.domain import AuditEvent
from app.security.auth import AuthenticationError, decode_claims
from app.security.masking import mask_payload
from app.security.principal import Principal

bearer = HTTPBearer(auto_error=False)


def get_container(request: Request) -> Any:
    c = getattr(request.app.state, "container", None)
    if c is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "service is starting")
    return c


def get_principal(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer),
                  c: Any = Depends(get_container)) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = decode_claims(c.settings, creds.credentials)
        revoked = getattr(request.app.state, "revocations", None)
        if revoked is not None and revoked.is_revoked(claims.get("jti")):
            raise AuthenticationError("token revoked")
    except AuthenticationError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token",
                            headers={"WWW-Authenticate": "Bearer"}) from None
    p = Principal(user_id=str(claims["sub"]), role=str(claims["role"]), via="api")
    request.state.claims = claims
    user_id_var.set(p.user_id)
    request.state.principal = p
    return p


def require_role(role: str) -> Callable[..., Principal]:
    def dep(p: Principal = Depends(get_principal)) -> Principal:
        if not p.has(role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role '{role}'")
        return p
    return dep


def audit(c: Any, p: Principal | None, action: str, result: str = "ok", entity_type: str | None = None,
          entity_id: str | None = None, **details: Any) -> None:
    c.store.append_audit(AuditEvent(ts=utcnow(), user_id=p.user_id if p else None, role=p.role if p else None,
                                    action=action, entity_type=entity_type, entity_id=entity_id, result=result,
                                    request_id=request_id_var.get(), details=details))


def present(c: Any, p: Principal, payload: Any, unmask: bool = False) -> Any:
    """Serialise and apply PII masking unless an admin explicitly asked to unmask (audited by caller)."""
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    elif isinstance(payload, list):
        payload = [x.model_dump(mode="json") if hasattr(x, "model_dump") else x for x in payload]
    enabled = c.settings.pii_masking and not (unmask and p.has("admin"))
    return mask_payload(payload, enabled)


_ERROR_STATUS = {"not_found": 404, "invalid_input": 422, "permission_denied": 403, "timeout": 504,
                 "unknown_tool": 404, "malformed_output": 502, "internal": 500}


def run_tool(c: Any, p: Principal, name: str, args: dict[str, Any]) -> Any:
    """Invoke a registered tool on behalf of an API caller (same checks + audit as the agent)."""
    from app.tools.registry import ToolContext

    res = c.registry.invoke(name, args, ToolContext(principal=p, services=c, request_id=request_id_var.get()))
    if not res.ok:
        raise HTTPException(_ERROR_STATUS.get(res.error_type or "internal", 500), res.error or "tool error")
    return res.data
