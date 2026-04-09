"""Bridge dependency injection — shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Request  # noqa: TC002 — FastAPI needs runtime import for DI


def get_tenant_id(request: Request) -> str:
    """Extract tenant ID from request state (set by TenantMiddleware).

    Falls back to DEFAULT_TENANT if not set.
    """
    from robothor.constants import DEFAULT_TENANT

    return getattr(request.state, "tenant_id", DEFAULT_TENANT)
