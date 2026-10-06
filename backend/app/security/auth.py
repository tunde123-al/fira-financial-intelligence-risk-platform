"""Authentication: password hashing (scrypt) and JWT access tokens (HS256).

Secrets come only from the environment: JWT_SECRET must be set (>= 32 chars) in
production, and bootstrap user passwords are read from
BOOTSTRAP_ADMIN_PASSWORD / BOOTSTRAP_ANALYST_PASSWORD on first start.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import uuid
from datetime import timedelta
from typing import Any

import jwt

from app.data.store import utcnow
from app.security.principal import ROLE_RANK, Principal

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
_EPHEMERAL_SECRET = secrets.token_urlsafe(48)


class AuthenticationError(Exception):
    pass


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p),
                            dklen=len(base64.b64decode(dk_b64)))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def jwt_secret(settings: Any) -> str:
    if settings.jwt_secret:
        if settings.is_production and len(settings.jwt_secret) < 32:
            raise RuntimeError("JWT_SECRET must be at least 32 characters in production")
        return settings.jwt_secret
    if settings.is_production:
        raise RuntimeError("JWT_SECRET must be set in production")
    # development only: random per-process secret (tokens die on restart)
    return _EPHEMERAL_SECRET


def create_token(settings: Any, user_id: str, username: str, role: str) -> dict[str, Any]:
    now = utcnow()
    exp = now + timedelta(minutes=settings.jwt_ttl_minutes)
    token = jwt.encode({"sub": user_id, "name": username, "role": role, "iat": int(now.timestamp()),
                        "exp": int(exp.timestamp()), "jti": uuid.uuid4().hex, "iss": "fira"},
                       jwt_secret(settings), algorithm="HS256")
    return {"access_token": token, "token_type": "bearer", "expires_at": exp.isoformat(), "role": role}


def decode_claims(settings: Any, token: str) -> dict[str, Any]:
    try:
        claims = jwt.decode(token, jwt_secret(settings), algorithms=["HS256"], issuer="fira",
                            options={"require": ["exp", "sub", "role", "iat"]})
    except jwt.PyJWTError as e:
        raise AuthenticationError(f"invalid token: {type(e).__name__}") from None
    if claims.get("role") not in ROLE_RANK:
        raise AuthenticationError("invalid role")
    return dict(claims)


def decode_token(settings: Any, token: str) -> Principal:
    claims = decode_claims(settings, token)
    return Principal(user_id=str(claims["sub"]), role=str(claims["role"]), via="api")


def authenticate(store: Any, username: str, password: str) -> dict[str, Any]:
    user = store.get_user(username)
    # constant-ish time: always run a hash verification
    stored = user["password_hash"] if user else "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    ok = verify_password(password, stored)
    if not user or not ok or not user.get("active", True):
        raise AuthenticationError("invalid credentials")
    return user


def bootstrap_users(store: Any, settings: Any) -> list[str]:
    """Create the initial admin/analyst users from environment passwords (idempotent)."""
    created = []
    for username, role, pw in (("admin", "admin", settings.bootstrap_admin_password),
                               ("analyst", "analyst", settings.bootstrap_analyst_password)):
        if pw and store.get_user(username) is None:
            store.upsert_user({"user_id": f"U-{username}", "username": username, "password_hash": hash_password(pw),
                               "role": role, "active": True})
            created.append(username)
    return created
