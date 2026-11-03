"""What an agent can reach: the chat's Tools dialog reads.

Two read-only proxies to the engine, which is the only process holding the
tool registry and the MCP pool:

* ``GET /api/agents/{agent_id}/tools`` -> ``GET /api/admin/agents/{id}/tools``
* ``GET /api/mcp/servers`` -> ``GET /api/admin/mcp/servers``

Tenant-admin, first line, like the agent manifests: an agent and the MCP pool
belong to one tenant's engine, so both reads go to the ACTING tenant's engine
(``engine_request(..., tenant=get_tenant_id(request))``). A root admin who
switched into a child sees the child's tools, and a child's admin never sees
another tenant's. A rejected caller never reaches the engine.
The engine's answer is passed through with its status and never cached; it
already leaves out how each MCP server is reached.

Plain ``def`` (test_route_concurrency.py): the handler runs in the threadpool
and drives the one engine call to completion there.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from deps import get_tenant_id
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from routers._engine_client import engine_request
from routers._operator import require_tenant_admin

router = APIRouter(prefix="/api", tags=["agents"])

#: The engine's own rule for an agent id (``config.load_agent_config``). The id
#: reaches a proxied path, so anything else is refused here.
_AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _proxied(path: str, tenant: str) -> JSONResponse:
    status, body = asyncio.run(engine_request("GET", path, tenant=tenant))
    if status in (401, 403):
        # The engine refusing the BRIDGE's token or scope, not the caller:
        # passed through, the dialog would tell an operator their session
        # ended or that only an operator may look. The caller's own refusal
        # is ``require_tenant_admin``'s 403, which never reaches here.
        status, body = (
            502,
            {
                "error": (
                    f"the engine refused the bridge's credentials (HTTP {status}); "
                    "check the shared signing key"
                )
            },
        )
    return JSONResponse(status_code=status, content=body, headers={"Cache-Control": "no-store"})


@router.get("/agents/{agent_id}/tools")
def agent_tools(agent_id: str, request: Request) -> Any:
    """The agent's tools by source, and its manifest's allow and deny lists."""
    require_tenant_admin(request)
    if not _AGENT_ID.fullmatch(agent_id):
        raise HTTPException(status_code=422, detail="not an agent id")
    return _proxied(f"/api/admin/agents/{agent_id}/tools", get_tenant_id(request))


@router.get("/mcp/servers")
def mcp_servers(request: Request) -> Any:
    """Each MCP server's health and tool count."""
    require_tenant_admin(request)
    return _proxied("/api/admin/mcp/servers", get_tenant_id(request))
