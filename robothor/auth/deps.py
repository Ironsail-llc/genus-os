"""Verified identity for a request.

``AuthContext`` is what the bridge derives from a *verified* token, replacing the
unverified ``X-Agent-Id`` / ``X-Tenant-Id`` headers. The core (``verify_token``,
``bearer_from_header``) is framework-agnostic; ``get_current_user`` is a FastAPI
dependency that lazy-imports FastAPI so this module stays importable by the CLI
and the engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

from robothor.auth.tokens import TokenError, decode_token

_HUMAN_ROLES = frozenset({"owner", "admin", "member", "user", "viewer", "auditor"})


@dataclass(frozen=True)
class AuthContext:
    """A verified caller identity (from a decoded access token)."""

    user_id: str
    tenant_id: str
    role: str
    typ: str  # "user" (human session) | "service" (engine/agent → bridge)
    audience: str = ""
    scopes: frozenset[str] = field(default_factory=frozenset)
    agent_id: str | None = None
    token_id: str = ""

    @property
    def is_service(self) -> bool:
        return self.typ == "service"

    @property
    def actor_id(self) -> str:
        """Stable audit actor: service agent when present, otherwise the user."""
        return self.agent_id or self.user_id

    def has_scope(self, required: str) -> bool:
        """Return whether a concrete scope is granted, including namespace wildcards."""
        if "*" in self.scopes or required in self.scopes:
            return True
        namespace, separator, _ = required.partition(":")
        return bool(separator and f"{namespace}:*" in self.scopes)


def verify_token(token: str, *, expected_audience: str = "genus-bridge") -> AuthContext:
    """Decode + verify a token into an ``AuthContext``. Raises ``TokenError``."""
    claims = decode_token(token, expected_audience=expected_audience)
    sub = claims.get("sub")
    tid = claims.get("tid")
    tenant = claims.get("tenant")
    typ = claims.get("typ")
    type_alias = claims.get("type")
    role = claims.get("role")
    audience = claims.get("aud")
    token_id = claims.get("jti")
    if not sub or not tid or not role or not audience or not token_id:
        raise TokenError("token missing sub/tid")
    if tenant is not None and str(tenant) != str(tid):
        raise TokenError("conflicting tenant claims")
    if typ not in ("user", "service"):
        raise TokenError("unsupported token type")
    if type_alias is not None and type_alias != typ:
        raise TokenError("conflicting token type claims")

    scopes = _parse_scopes(claims.get("scope"))
    if not scopes:
        raise TokenError("token missing scopes")

    agent_id = claims.get("agent_id")
    if typ == "service" and not agent_id:
        raise TokenError("service token missing agent_id")
    if typ == "service" and role != "service":
        raise TokenError("service token has invalid role")
    if typ == "user" and agent_id is not None:
        raise TokenError("user token cannot carry agent_id")
    if typ == "user" and role not in _HUMAN_ROLES:
        raise TokenError("user token has invalid role")
    if not isinstance(audience, str):
        raise TokenError("token audience must be a string")

    return AuthContext(
        user_id=str(sub),
        tenant_id=str(tid),
        role=str(role),
        typ=str(typ),
        audience=str(audience),
        scopes=scopes,
        agent_id=str(agent_id) if agent_id is not None else None,
        token_id=str(token_id),
    )


def _parse_scopes(raw: object) -> frozenset[str]:
    if isinstance(raw, str):
        return frozenset(part for part in raw.split() if part)
    if isinstance(raw, (list, tuple)) and all(isinstance(item, str) for item in raw):
        return frozenset(item.strip() for item in raw if item.strip())
    raise TokenError("invalid token scopes")


def require_access(
    *, scopes: Iterable[str] = (), roles: Iterable[str] = ()
) -> Callable[[object], AuthContext]:
    """Build a FastAPI dependency enforcing verified scopes and/or user roles."""
    required_scopes = tuple(scopes)
    permitted_roles = frozenset(roles)

    def dependency(request: object) -> AuthContext:
        from fastapi import HTTPException

        ctx = get_current_user(request)
        if required_scopes and not all(ctx.has_scope(scope) for scope in required_scopes):
            raise HTTPException(status_code=403, detail="insufficient scope")
        if permitted_roles and (ctx.is_service or ctx.role not in permitted_roles):
            raise HTTPException(status_code=403, detail="role not authorized")
        return ctx

    return dependency


def bearer_from_header(authorization: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header value."""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


# Cookie name the dashboard sets for the access token (httpOnly).
SESSION_COOKIE = "gos_session"


def token_from_request(request: object) -> str | None:
    """Pull the access token from the Authorization header or the session cookie."""
    headers = getattr(request, "headers", {})
    token = bearer_from_header(headers.get("authorization") if headers else None)
    if token:
        return token
    cookies = getattr(request, "cookies", {}) or {}
    return cookies.get(SESSION_COOKIE)


def get_current_user(request: object) -> AuthContext:
    """FastAPI dependency — yield the verified ``AuthContext`` or 401.

    Use in routers that need the caller's identity. Middleware handles coarse
    gating; this gives handlers the typed context.
    """
    from fastapi import HTTPException  # lazy: keep this module fastapi-optional

    token = token_from_request(request)
    if not token:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        return verify_token(token)
    except TokenError as e:
        # Generic message only — the raw verification detail would be a token
        # oracle for unauthenticated callers (the middleware audit-logs the reason).
        raise HTTPException(status_code=401, detail="invalid or expired token") from e
