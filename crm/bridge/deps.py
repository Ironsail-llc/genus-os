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
        return auth.tenant_id
    return getattr(request.state, "tenant_id", DEFAULT_TENANT)
