"""Bridge middleware — RBAC, correlation IDs, error formatting."""

from __future__ import annotations

import uuid

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from robothor.audit.logger import log_event
from robothor.events.capabilities import check_endpoint_access, load_capabilities

# Load the agent capabilities manifest once at import time
load_capabilities()


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


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Attach a unique X-Correlation-Id to every request/response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        correlation_id = request.headers.get("x-correlation-id") or uuid.uuid4().hex
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = correlation_id
        return response
