"""Installed plugins, from the Helm.

Every answer here is the engine's. A plugin is an object in THAT process: what
loaded, what was refused and why, and which discovery generation the caches are
serving cannot be asked from outside it. So this router is three things and
nothing else — a gate, a proxy, and an audit trail.

The gate is the half worth arguing about. ``disable`` takes a capability out of
service on the next reload, and ``reload`` re-imports third-party code into a
running daemon; a non-operator reaching either would be changing what the
appliance executes. So every route's first statement is ``require_operator``,
and the tenant dependency refuses a second tenant outright: one engine runs one
set of plugins, and another tenant's operator disabling one would be acting on
somebody else's instance. The same shape ``agent_manifests`` and
``channel_access`` are built on.

Nothing here reads the lockfile, and nothing here decides anything. A second
implementation of "which plugins are installed" living in the bridge would
answer from a different process's view of site-packages — which is how a
dashboard comes to disagree with the engine it is describing.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from deps import get_tenant_id
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from routers._audit import audited
from routers._engine_client import engine_request
from routers._operator import require_operator

logger = logging.getLogger(__name__)


def _require_primary_tenant(tenant_id: str = Depends(get_tenant_id)) -> None:
    """Plugins are appliance-global, so deny secondary tenants.

    Symmetric with ``agent_manifests`` and ``channel_access``: one engine runs
    one set of plugins, and a second tenant's operator disabling one would be
    taking a capability out of somebody else's instance.
    """
    from robothor.constants import DEFAULT_TENANT

    if tenant_id != DEFAULT_TENANT:
        raise HTTPException(
            status_code=403,
            detail="appliance administration not authorized for tenant",
        )


router = APIRouter(
    prefix="/api/plugins",
    tags=["plugins"],
    dependencies=[Depends(_require_primary_tenant)],
)

#: What a distribution name may look like. PyPI names are letters and digits
#: separated by ``.-_``; the value reaches a proxied URL path and an audit
#: ``action``, so anything else is a 422 here rather than a request the engine
#: has to decide about.
_DIST_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _proxied(status: int, body: Any) -> JSONResponse:
    """The engine's answer, with its status code. Never manufactured here."""
    return JSONResponse(status_code=status, content=body)


def _name(value: str) -> str:
    if not _DIST_NAME.match(value or ""):
        raise HTTPException(status_code=422, detail="not a distribution name")
    return value


@router.get("")
async def list_plugins(request: Request) -> JSONResponse:
    """What is installed, what loaded, and what the lockfile records.

    Not audited: a listing is not an act, and an audit row per page load would
    bury the disable that matters.
    """
    require_operator(request)
    status, body = await engine_request("GET", "/api/admin/plugins")
    return _proxied(status, body)


@router.post("/sync")
async def sync_plugins(request: Request) -> JSONResponse:
    """Record the installed distributions in the lockfile.

    The action a fresh install opens with: until a sync has run, nothing is
    recorded, the lockfile governs nothing, and every enable/disable is a 404 —
    so without this route the Plugins page was read-only until somebody got a
    shell on the box.

    No ``force``. The engine refuses when the existing file holds intent it
    cannot read, and an operator discarding recorded disables should be reading
    the doctor's line about what it costs first; that escape stays on the CLI.

    Declared before ``/{name}/...`` so the literal path is matched as itself.
    """
    require_operator(request)
    status, body = await engine_request("POST", "/api/admin/plugins/sync")
    audited(
        request,
        "plugin.sync",
        action="sync",
        status="ok" if status < 400 else "error",
    )
    return _proxied(status, body)


@router.post("/reload")
async def reload_plugins(request: Request) -> JSONResponse:
    """Re-discover plugins in the running engine, without a restart.

    Declared before ``/{name}/...`` so the literal path is matched as itself.
    """
    require_operator(request)
    status, body = await engine_request("POST", "/api/admin/plugins/reload")
    audited(
        request,
        "plugin.reload",
        action="reload",
        status="ok" if status < 400 else "error",
    )
    return _proxied(status, body)


@router.post("/{name}/enable")
async def enable_plugin(name: str, request: Request) -> JSONResponse:
    """Let a recorded plugin load again on the next reload."""
    require_operator(request)
    plugin = _name(name)
    status, body = await engine_request("POST", f"/api/admin/plugins/{plugin}/enable")
    audited(
        request,
        "plugin.enable",
        action=plugin,
        plugin=plugin,
        status="ok" if status < 400 else "error",
    )
    return _proxied(status, body)


@router.post("/{name}/disable")
async def disable_plugin(name: str, request: Request) -> JSONResponse:
    """Stop a plugin being imported at all, from the next reload on."""
    require_operator(request)
    plugin = _name(name)
    status, body = await engine_request("POST", f"/api/admin/plugins/{plugin}/disable")
    audited(
        request,
        "plugin.disable",
        action=plugin,
        plugin=plugin,
        status="ok" if status < 400 else "error",
    )
    return _proxied(status, body)
