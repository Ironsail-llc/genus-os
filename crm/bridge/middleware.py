"""Bridge middleware — RBAC, correlation IDs, tenant isolation, error formatting."""

from __future__ import annotations

import logging
import os
import uuid
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from robothor.audit.logger import log_event

if TYPE_CHECKING:
    from fastapi import Request
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.responses import Response

    from robothor.auth.deps import AuthContext
from robothor.events.capabilities import check_endpoint_access, load_capabilities

# Load the agent capabilities manifest once at import time
load_capabilities()

logger = logging.getLogger(__name__)


def _log_unauthorized(request: Request, reason: str) -> None:
    """One WARNING per 401 that names the caller, so a rejected local client
    can be attributed from the journal (production showed repeated loopback
    401s with no way to tell which client held the stale token).

    Identity only — User-Agent and remote address. Never the credential.
    """
    client = request.client
    remote_addr = client.host if client is not None else "-"
    logger.warning(
        "401 %s %s from %s ua=%r: %s",
        request.method,
        request.url.path,
        remote_addr,
        request.headers.get("user-agent", "-"),
        reason,
    )


class TenantMiddleware(BaseHTTPMiddleware):
    """Derive the tenant from verified claims (or explicit loopback dev mode)."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        from robothor.constants import DEFAULT_TENANT

        auth = getattr(request.state, "auth", None)
        requested_tenant = request.headers.get("x-tenant-id")
        if auth is not None:
            tenant_id = auth.tenant_id
            if requested_tenant and requested_tenant != tenant_id:
                log_event(
                    "auth.denied",
                    "tenant header does not match verified token",
                    actor=auth.actor_id,
                    details={"method": request.method, "path": request.url.path},
                    status="denied",
                )
                return JSONResponse(status_code=403, content={"error": "tenant not authorized"})
        elif _legacy_headers_allowed():
            tenant_id = requested_tenant or DEFAULT_TENANT
        else:
            # Only public endpoints can reach this branch in secure mode.
            tenant_id = DEFAULT_TENANT

        request.state.tenant_id = tenant_id
        response = await call_next(request)
        if auth is not None or _legacy_headers_allowed():
            response.headers["X-Tenant-Id"] = tenant_id
        return response


class RBACMiddleware(BaseHTTPMiddleware):
    """Check capabilities for a verified service-agent identity.

    The legacy header is consulted only in explicit insecure development mode.
    A signed service identity must name a configured agent; unknown agents are
    denied even when an old capability manifest's default policy is ``allow``.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        from robothor.events.capabilities import list_agents

        auth = getattr(request.state, "auth", None)
        requested_agent = request.headers.get("x-agent-id")
        agent_id: str | None = None

        if auth is not None:
            request.state.actor_id = auth.actor_id
            agent_id = auth.agent_id if auth.is_service else None
            if requested_agent and requested_agent != agent_id:
                return _deny(
                    request,
                    "agent header does not match verified token",
                    actor=auth.actor_id,
                    public_error="agent identity not authorized",
                )
            if auth.is_service and agent_id not in set(list_agents()):
                return _deny(
                    request,
                    "verified token names an unknown agent",
                    actor=auth.actor_id,
                    public_error="agent not authorized",
                )
        elif _legacy_headers_allowed():
            agent_id = requested_agent
            request.state.actor_id = requested_agent

        request.state.agent_id = agent_id
        if agent_id:
            method = request.method
            path = request.url.path
            if not check_endpoint_access(agent_id, method, path):
                return _deny(
                    request,
                    f"Agent '{agent_id}' denied {method} {path}",
                    actor=agent_id,
                    public_error=f"Agent '{agent_id}' not authorized for {method} {path}",
                )
        return await call_next(request)


class AuthMiddleware(BaseHTTPMiddleware):
    """Verify a bridge-issued session/service token and set request.state.auth.

    Replaces trusted ``X-Agent-Id``/``X-Tenant-Id`` derivation with a verified,
    audience-bound identity. Authentication is on by default. Legacy behavior
    exists only under ``GENUS_INSECURE_DEV_MODE=true`` on a loopback bind.

    - valid token  → ``request.state.auth = AuthContext`` (tenant comes from the token).
    - missing token on a private route → 401.
    - valid token without route scope/role → 403.
    - only probe and token bootstrap/rotation routes are public.
    """

    _PUBLIC_PATHS = frozenset(
        {
            "/live",
            "/liveness",
            "/ready",
            "/api/auth/sso",
            "/api/auth/refresh",
            "/api/auth/logout",
        }
    )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        from robothor.auth.deps import token_from_request, verify_token
        from robothor.auth.tokens import TokenError

        request.state.auth = None
        token = token_from_request(request)
        if token:
            try:
                request.state.auth = verify_token(token, expected_audience="genus-bridge")
            except TokenError as e:
                # Always reject a supplied invalid credential, including on a
                # public route. Never expose PyJWT's distinguishing error text.
                log_event(
                    "auth.denied",
                    "invalid bridge token",
                    details={
                        "reason": str(e),
                        "method": request.method,
                        "path": request.url.path,
                    },
                    status="denied",
                )
                _log_unauthorized(request, "invalid or expired token")
                return JSONResponse(status_code=401, content={"error": "invalid or expired token"})

        path = request.url.path
        if _auth_enforced() and request.state.auth is None and path not in self._PUBLIC_PATHS:
            log_event(
                "auth.denied",
                f"Unauthenticated {request.method} {path}",
                details={"method": request.method, "path": path},
                status="denied",
            )
            _log_unauthorized(request, "authentication required")
            return JSONResponse(status_code=401, content={"error": "authentication required"})

        auth = request.state.auth
        if auth is not None:
            denial = _authorization_denial(auth, request.method, path)
            if denial:
                return _deny(request, denial, actor=auth.actor_id, public_error=denial)

        return await call_next(request)


def _auth_enforced() -> bool:
    from robothor.auth.runtime import auth_required

    return auth_required(bind_host=_bridge_bind_host())


def _legacy_headers_allowed() -> bool:
    from robothor.auth.runtime import legacy_headers_allowed

    return legacy_headers_allowed(bind_host=_bridge_bind_host())


def _bridge_bind_host() -> str:
    return os.environ.get("ROBOTHOR_BRIDGE_HOST", "127.0.0.1")


def _authorization_denial(auth: AuthContext, method: str, path: str) -> str | None:
    """Return a stable public denial reason for a verified identity."""
    if path.startswith("/api/auth/"):
        return None

    required_scope = (
        "bridge:read" if method.upper() in {"GET", "HEAD", "OPTIONS"} else "bridge:write"
    )
    if not auth.has_scope(required_scope):
        return "insufficient scope"

    if path.startswith("/api/tenants"):
        if auth.is_service:
            if not auth.has_scope("tenant:admin"):
                return "tenant administration not authorized"
        elif auth.role not in {"owner", "admin"}:
            return "role not authorized"

    if path.startswith("/api/audit") or path.startswith("/api/telemetry"):
        if auth.is_service:
            if not auth.has_scope("audit:read"):
                return "audit access not authorized"
        elif auth.role not in {"owner", "admin", "auditor"}:
            return "role not authorized"

    # The generic bridge scopes above grant access to ordinary tenant data.
    # Sensitive credentials, appliance-global state, and authority-changing
    # operations require a narrower capability.  Owner/admin sessions retain
    # their operator access; every other human or service caller must carry the
    # explicit scope named here.  This prevents a default ``member`` token (or
    # a generic ``bridge:*`` service token) from silently becoming an
    # administrator.
    if path.startswith("/api/vault/"):
        return _privileged_access_denial(auth, "vault:read", "vault access not authorized")

    integration_scope = _integration_scope(method, path)
    if integration_scope:
        return _privileged_access_denial(
            auth,
            integration_scope,
            "integration access not authorized",
        )

    if path == "/api/installed-agents" or path.startswith("/api/installed-agents/"):
        from robothor.constants import DEFAULT_TENANT

        # Installation state is appliance-global, not tenant-local.  Until it
        # is represented as a tenant-scoped resource, only the primary tenant
        # may administer or inspect it.
        if auth.tenant_id != DEFAULT_TENANT:
            return "appliance administration not authorized for tenant"
        scope = "agent:read" if method.upper() in {"GET", "HEAD", "OPTIONS"} else "agent:admin"
        return _privileged_access_denial(auth, scope, "agent administration not authorized")

    memory_scope = _memory_admin_scope(method, path)
    if memory_scope:
        denial = _privileged_access_denial(
            auth,
            memory_scope,
            "memory administration not authorized",
        )
        if denial:
            return denial

        # The current ingestion and pipeline-trigger implementations are
        # appliance-local and do not accept a tenant boundary.  Fail closed
        # for every non-primary tenant instead of writing or executing against
        # another tenant's state.
        if path == "/api/memory/store" or path.startswith("/api/memory/pipeline/trigger/"):
            from robothor.constants import DEFAULT_TENANT

            if auth.tenant_id != DEFAULT_TENANT:
                return "memory operation not authorized for tenant"

    if method.upper() == "POST" and path.startswith("/api/tasks/"):
        action = path.rsplit("/", 1)[-1]
        if action in {"approve", "reject", "answer"}:
            return _privileged_access_denial(
                auth,
                "task:approve",
                "task approval not authorized",
            )

    return None


def _privileged_access_denial(
    auth: AuthContext,
    scope: str,
    public_error: str,
    *,
    operator_roles: frozenset[str] = frozenset({"owner", "admin"}),
) -> str | None:
    """Require an explicit capability or a trusted human operator role."""
    if auth.has_scope(scope):
        return None
    if not auth.is_service and auth.role in operator_roles:
        return None
    return public_error


def _integration_scope(method: str, path: str) -> str | None:
    """Return the narrow scope for legacy integration routes, if any."""
    if path == "/resolve-contact" or path == "/log-interaction":
        return "integration:write"
    if path.startswith("/timeline/"):
        return "integration:read"
    return None


def _memory_admin_scope(method: str, path: str) -> str | None:
    """Classify memory endpoints that expose or mutate administrative state."""
    verb = method.upper()
    if path == "/api/memory/store":
        return "memory:write"
    if path == "/api/memory/stats" or path.startswith("/api/memory/pipeline/"):
        return "memory:admin"
    if path == "/api/memory/blocks" or path.startswith("/api/memory/blocks/"):
        return "memory:read" if verb in {"GET", "HEAD", "OPTIONS"} else "memory:write"
    return None


def _deny(
    request: Request,
    reason: str,
    *,
    actor: str,
    public_error: str,
) -> JSONResponse:
    log_event(
        "auth.denied",
        reason,
        actor=actor,
        details={"method": request.method, "path": request.url.path},
        status="denied",
    )
    return JSONResponse(status_code=403, content={"error": public_error})


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Attach a unique X-Correlation-Id to every request/response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        correlation_id = request.headers.get("x-correlation-id") or uuid.uuid4().hex
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = correlation_id
        return response
