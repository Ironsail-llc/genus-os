"""Verified identity for a request.

``AuthContext`` is what the bridge derives from a *verified* token, replacing the
unverified ``X-Agent-Id`` / ``X-Tenant-Id`` headers. The core (``verify_token``,
``bearer_from_header``) is framework-agnostic; ``get_current_user`` is a FastAPI
dependency that lazy-imports FastAPI so this module stays importable by the CLI
and the engine.
"""

from __future__ import annotations

from dataclasses import dataclass

from robothor.auth.tokens import TokenError, decode_token


@dataclass(frozen=True)
class AuthContext:
    """A verified caller identity (from a decoded access token)."""

    user_id: str
    tenant_id: str
    role: str
    typ: str  # "user" (human session) | "service" (engine/agent → bridge)

    @property
    def is_service(self) -> bool:
        return self.typ == "service"


def verify_token(token: str) -> AuthContext:
    """Decode + verify a token into an ``AuthContext``. Raises ``TokenError``."""
    claims = decode_token(token)
    sub = claims.get("sub")
    tid = claims.get("tid")
    if not sub or not tid:
        raise TokenError("token missing sub/tid")
    return AuthContext(
        user_id=str(sub),
        tenant_id=str(tid),
        role=str(claims.get("role") or ""),
        typ=str(claims.get("typ") or "user"),
    )


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
        raise HTTPException(status_code=401, detail=f"invalid token: {e}") from e
