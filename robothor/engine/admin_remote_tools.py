"""The target engine's remote-tool routes: what a parent tenant's agent may run here.

Three routes under ``/api/admin`` (so ``engine:control``, by prefix, and a token
for this engine's own tenant, both enforced by ``engine/auth.py``):

* ``GET  /api/admin/remote-tools`` -- the declared tools, the plugins and
  their doctor checks.
* ``GET  /api/admin/remote-tools/{tool}`` -- one tool's declaration and schema.
* ``POST /api/admin/remote-tools/call`` -- run one.

The bridge is the only caller: it forwards a parent's call after its own
checks, and this engine checks again (``robothor.engine.remote_tools``). Any
other identity, a person's token included, is refused.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import Request  # noqa: TC002 - FastAPI resolves the annotation at runtime

from robothor.tenancy import remote_contract as rc

if TYPE_CHECKING:
    from fastapi import FastAPI

    from robothor.auth.deps import AuthContext

logger = logging.getLogger(__name__)


def is_bridge(auth: AuthContext) -> bool:
    """Whether the caller is the bridge's own service identity."""
    return bool(auth.is_service and auth.user_id == rc.BRIDGE_SERVICE_ID)


def register(app: FastAPI) -> None:
    """Mount the three remote-tool routes on the engine app."""
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    from robothor.engine import remote_tools
    from robothor.engine.auth import request_context

    router = APIRouter(prefix="/api/admin", tags=["admin"])

    def refused(status: int, code: str) -> JSONResponse:
        return JSONResponse(remote_tools.refusal_body(code), status_code=status)

    def not_the_bridge(request: Request) -> JSONResponse | None:
        if is_bridge(request_context(request)):
            return None
        return refused(403, rc.ERR_CALLER_FORBIDDEN)

    @router.get("/remote-tools")
    async def list_remote_tools(request: Request) -> Any:
        """What this tenant offers its parent's agents."""
        denied = not_the_bridge(request)
        if denied is not None:
            return denied
        try:
            return await remote_tools.catalogue(request_context(request).tenant_id)
        except remote_tools.Refused as exc:
            return JSONResponse(exc.body(), status_code=exc.status)

    @router.get("/remote-tools/{tool}")
    async def describe_remote_tool(tool: str, request: Request) -> Any:
        """One remote tool's declaration and parameters."""
        denied = not_the_bridge(request)
        if denied is not None:
            return denied
        try:
            return await remote_tools.describe(tool)
        except remote_tools.Refused as exc:
            return JSONResponse(exc.body(), status_code=exc.status)

    @router.post("/remote-tools/call")
    async def call_remote_tool(request: Request) -> Any:
        """Run one remote tool for a parent tenant's agent."""
        auth = request_context(request)
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = None
        if not is_bridge(auth):
            # Audited too: a call that reached here from anyone else is a fact.
            fields = body if isinstance(body, dict) else {}
            remote_tools.audit(
                tenant_id=auth.tenant_id,
                home_tenant=str(fields.get("home_tenant") or "")[:128],
                user_id="",
                agent_id=str(fields.get("agent_id") or "")[:64],
                tool=str(fields.get("tool") or "")[:128],
                access=str(fields.get("access") or "")[:16],
                outcome=rc.ERR_CALLER_FORBIDDEN,
                dry_run=fields.get("dry_run") is True,
                request_id=None,
                args=fields.get("args"),
            )
            return refused(403, rc.ERR_CALLER_FORBIDDEN)
        if not isinstance(body, dict):
            return refused(422, rc.ERR_BAD_ARGUMENTS)
        try:
            status, payload = await remote_tools.execute(auth.tenant_id, body)
        except Exception:
            logger.exception("remote tool call failed")
            return JSONResponse(
                {"error": "internal_error", "message": "the call failed", "attestation": None},
                status_code=500,
            )
        return JSONResponse(payload, status_code=status)

    app.include_router(router)
