"""API Bearer Token Authentication, Scope Authorization, and Startup Security Guards (P1-1a)."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.config import ApiSettings

SCOPE_READ = "read"
SCOPE_CONTROL = "control"
SCOPE_APPROVE = "approve"
SCOPE_INTERNAL = "internal"
SCOPE_ADMIN = "admin"

ALL_SCOPES = {SCOPE_READ, SCOPE_CONTROL, SCOPE_APPROVE, SCOPE_INTERNAL, SCOPE_ADMIN}


def hash_api_key(raw_key: str) -> str:
    """Compute SHA-256 hex digest of an API key. Plaintext keys are never stored in DB."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key(prefix: str = "aoc") -> tuple[str, str, str]:
    """Generate a new random API key and return (plaintext, key_prefix, key_hash)."""
    token = f"{prefix}_{secrets.token_urlsafe(32)}"
    return token, token[:12], hash_api_key(token)


def is_loopback_host(host: str) -> bool:
    """Return True only if ``host`` resolves statically to a loopback interface."""
    cleaned = (host or "").strip().lower()
    if cleaned in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return False


def validate_api_security_config(api_cfg: ApiSettings, has_db_credentials: bool = False) -> None:
    """Refuse to start if bound to a non-loopback address without active authentication."""
    if is_loopback_host(api_cfg.host):
        return

    if not api_cfg.auth_enabled:
        raise RuntimeError(
            f"Refusing to start API on non-loopback host '{api_cfg.host}' with auth_enabled=False"
        )

    admin_secret = api_cfg.admin_key.get_secret_value().strip() if api_cfg.admin_key else ""
    if not admin_secret and not has_db_credentials:
        raise RuntimeError(
            f"Refusing to start API on non-loopback host '{api_cfg.host}' without "
            "AOC_API__ADMIN_KEY or active api_credentials in PostgreSQL"
        )


class Principal(BaseModel):
    name: str
    scopes: set[str] = Field(default_factory=set)
    source: str = "db_credential"
    credential_id: str | None = None

    def has_scope(self, required: str) -> bool:
        return SCOPE_ADMIN in self.scopes or required in self.scopes


def _extract_bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def _ensure_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def authenticate_request(request: Request) -> Principal:
    """Authenticate an incoming HTTP request using Bearer token."""
    rt = request.app.state.runtime
    api_cfg: ApiSettings = rt.settings.api

    if not api_cfg.auth_enabled:
        return Principal(name="local-auth-disabled", scopes={SCOPE_ADMIN}, source="auth_disabled")

    token = _extract_bearer_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 1. Environment bootstrap admin key
    admin_secret = api_cfg.admin_key.get_secret_value().strip() if api_cfg.admin_key else ""
    if admin_secret and hmac.compare_digest(token, admin_secret):
        return Principal(name="env-admin", scopes={SCOPE_ADMIN}, source="env_admin")

    # 2. Dedicated internal metrics/embeddings token
    internal_secret = api_cfg.internal_token.get_secret_value().strip() if api_cfg.internal_token else ""
    if internal_secret and hmac.compare_digest(token, internal_secret):
        return Principal(name="env-internal", scopes={SCOPE_INTERNAL}, source="env_internal")

    # 3. Database hashed api_credentials lookup
    key_hash = hash_api_key(token)
    cred = await rt.repo.get_api_credential_by_hash(key_hash)
    if cred is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if cred.get("revoked"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )
    expires_at = cred.get("expires_at")
    if expires_at is not None and _ensure_utc(expires_at) <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )

    await rt.repo.touch_api_credential(cred["id"])
    return Principal(
        name=str(cred["name"]),
        scopes=set(cred.get("scopes") or []),
        source="db_credential",
        credential_id=str(cred["id"]),
    )


def require_scope(scope: str) -> Callable[..., Any]:
    """FastAPI dependency enforcing Bearer auth + required permission scope."""

    async def _dependency(request: Request) -> Principal:
        principal = await authenticate_request(request)
        if not principal.has_scope(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient scope: requires '{scope}'",
            )
        return principal

    return _dependency
