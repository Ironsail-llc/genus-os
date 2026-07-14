"""Bridge dependency injection — shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Request  # noqa: TC002 — FastAPI needs runtime import for DI


def get_tenant_id(request: Request) -> str:
    """Tenant for the request.

    Prefers the VERIFIED tenant from a bridge-issued token (``request.state.auth``,
    set by AuthMiddleware) over the unverified ``X-Tenant-Id`` header (kept as the
    legacy/service fallback). Falls back to DEFAULT_TENANT.
    """
    from robothor.constants import DEFAULT_TENANT

    auth = getattr(request.state, "auth", None)
    if auth is not None and getattr(auth, "tenant_id", None):
        return str(auth.tenant_id)
    return str(getattr(request.state, "tenant_id", DEFAULT_TENANT))


def get_actor_id(request: Request) -> str | None:
    """Return the verified user/service actor, or a loopback-only legacy agent."""
    auth = getattr(request.state, "auth", None)
    if auth is not None:
        return str(auth.actor_id)
    actor_id = getattr(request.state, "actor_id", None)
    return str(actor_id) if actor_id else None


def get_agent_id(request: Request) -> str | None:
    """Return a verified service agent ID (legacy header only in insecure dev)."""
    auth = getattr(request.state, "auth", None)
    if auth is not None:
        return str(auth.agent_id) if auth.is_service and auth.agent_id else None
    agent_id = getattr(request.state, "agent_id", None)
    return str(agent_id) if agent_id else None
