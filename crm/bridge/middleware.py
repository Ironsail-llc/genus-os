"""Bridge middleware — RBAC, correlation IDs, tenant isolation, error formatting."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from robothor.audit.logger import log_event

if TYPE_CHECKING:
    from fastapi import Request
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.responses import Response
from robothor.events.capabilities import check_endpoint_access, load_capabilities

# Load the agent capabilities manifest once at import time
load_capabilities()


class TenantMiddleware(BaseHTTPMiddleware):
    """Extract X-Tenant-Id header and set request.state.tenant_id.

    Defaults to ROBOTHOR_DEFAULT_TENANT env var (or 'default') when the header is absent.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        from robothor.constants import DEFAULT_TENANT

        tenant_id = request.headers.get("x-tenant-id", DEFAULT_TENANT)
        request.state.tenant_id = tenant_id
        response = await call_next(request)
        response.headers["X-Tenant-Id"] = tenant_id
        return response


class RBACMiddleware(BaseHTTPMiddleware):
    """Check agent capabilities via X-Agent-Id header.

    Missing header -> full access (backward compatible).
    Known agent -> check endpoint access, deny with 403 if unauthorized.
    Unknown agent -> default policy (allow).
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        agent_id = request.headers.get("x-agent-id")
        if agent_id:
            method = request.method
            path = request.url.path
            if not check_endpoint_access(agent_id, method, path):
                log_event(
                    "auth.denied",
                    f"Agent '{agent_id}' denied {method} {path}",
                    actor=agent_id,
                    details={"method": method, "path": path},
                    status="denied",
                )
                return JSONResponse(
                    status_code=403,
                    content={"error": f"Agent '{agent_id}' not authorized for {method} {path}"},
                )
        return await call_next(request)


class AuthMiddleware(BaseHTTPMiddleware):
    """Verify a bridge-issued session/service token and set request.state.auth.

    Replaces the trusted ``X-Agent-Id``/``X-Tenant-Id`` derivation with a VERIFIED
    identity. Behaviour is gated by ``GENUS_AUTH_ENFORCE`` (default off) so this
    ships dark — Phase A only attaches identity when a valid token is present and
    never blocks; Phase B flips enforcement on for user routes.

    - valid token  → ``request.state.auth = AuthContext`` (tenant comes from the token).
    - no/invalid token + enforce off → pass through (legacy X-Agent-Id path stays live).
    - no/invalid token + enforce on  → 401, except public routes (/api/auth/*, /health, docs).
    """

    _PUBLIC_PREFIXES = ("/api/auth/", "/health", "/docs", "/openapi.json", "/redoc")

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        from robothor.auth.deps import token_from_request, verify_token
        from robothor.auth.tokens import TokenError

        request.state.auth = None
        token = token_from_request(request)
        if token:
            try:
                request.state.auth = verify_token(token)
            except TokenError as e:
                if _auth_enforced():
                    return JSONResponse(status_code=401, content={"error": f"invalid token: {e}"})

        if _auth_enforced() and request.state.auth is None:
            path = request.url.path
            if not any(path.startswith(p) for p in self._PUBLIC_PREFIXES):
                log_event(
                    "auth.denied",
                    f"Unauthenticated {request.method} {path}",
                    details={"method": request.method, "path": path},
                    status="denied",
                )
                return JSONResponse(status_code=401, content={"error": "authentication required"})

        return await call_next(request)


def _auth_enforced() -> bool:
    import os

    return os.environ.get("GENUS_AUTH_ENFORCE", "").lower() in ("1", "true", "yes")


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Attach a unique X-Correlation-Id to every request/response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        correlation_id = request.headers.get("x-correlation-id") or uuid.uuid4().hex
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = correlation_id
        return response
