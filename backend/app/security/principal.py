"""Principals and role-based authorization."""
from __future__ import annotations

from dataclasses import dataclass

ROLE_RANK = {"analyst": 1, "admin": 2}


class PermissionDenied(PermissionError):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: str
    via: str = "api"  # api | mcp | system

    def has(self, required_role: str) -> bool:
        return ROLE_RANK.get(self.role, 0) >= ROLE_RANK.get(required_role, 99)

    def require(self, required_role: str) -> None:
        if not self.has(required_role):
            raise PermissionDenied(f"role '{self.role}' lacks '{required_role}' permission")


SYSTEM = Principal(user_id="system", role="admin", via="system")
