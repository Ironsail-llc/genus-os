"""What an agent can reach -- the engine half of the chat's Tools dialog.

Two reads under ``/api/admin`` (so ``engine:control``, by prefix, like every
other admin route):

* ``GET /api/admin/agents/{agent_id}/tools`` -- the names the registry would
  offer that agent, grouped by where each one comes from.
* ``GET /api/admin/mcp/servers`` -- the MCP pool's servers, their health and how
  many tools each contributed.

Answered here because both facts exist only in this process: the registry is
built from the built-ins, the loaded plugins and each MCP server's live
``tools/list``, and the pool holds the sessions.

The grouping uses the sources the engine already records. An MCP tool carries
its server in the registry's adapter routes; a built-in is a name in
:func:`~robothor.engine.tools.registry.builtin_schema_names` (opt-in when it
is in ``OPT_IN_TOOLS``); a plugin tool is attributed to the loaded distribution
whose manifest declares it. A plugin tool no loaded manifest declares is
reported under ``unattributed`` rather than guessed at.

``command`` and ``url`` (and anything else the pool knows about reaching a
server) are deployment detail: only the fields in ``_SHOWN`` leave this module.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: The plugin group for a tool no loaded distribution's manifest declares.
UNATTRIBUTED = "unattributed"

#: Manifest kinds that name tools: ``genus.tools`` handlers and
#: ``genus.schemas`` schemas (see ``robothor.plugins.loader._GROUPS``).
_TOOL_KINDS = ("handlers", "schemas")

#: The fields of ``McpClientPool.list_servers()`` a caller may see. An
#: allow-list, so a field the pool grows later (an env block, headers) stays
#: in this process until someone decides it is safe to show.
_SHOWN = ("name", "transport", "running", "initialized", "protocol")


def _manifest_dir() -> Path:
    from robothor.engine.admin_providers import _manifest_dir as engine_manifest_dir

    return engine_manifest_dir()


def _plugin_owners() -> dict[str, str]:
    """Tool name -> the loaded distribution whose manifest declares it."""
    from robothor.plugins import inventory as plugin_inventory

    owners: dict[str, str] = {}
    try:
        statuses = plugin_inventory.inventory()
    except Exception as e:  # noqa: BLE001 - a plugin must not break this read
        logger.warning("Plugin inventory unavailable for tool grouping: %s", e)
        return owners
    for status in statuses:
        if status.state != "loaded" or not status.manifest:
            continue
        declared = status.manifest.get("declared") or {}
        for kind in _TOOL_KINDS:
            for name in declared.get(kind) or []:
                owners.setdefault(str(name), status.name)
    return owners


def group_tools(names: list[str], registry: Any, owners: dict[str, str]) -> dict[str, Any]:
    """Sort *names* into core, plugins, mcp and opt_in, each list sorted.

    *owners* is :func:`_plugin_owners`, read off the loop by the caller.
    """
    from robothor.engine.tools.constants import OPT_IN_TOOLS
    from robothor.engine.tools.registry import builtin_schema_names

    builtins = builtin_schema_names()
    core: list[str] = []
    opt_in: list[str] = []
    plugins: dict[str, list[str]] = {}
    mcp: dict[str, list[str]] = {}
    for name in names:
        server = registry.get_adapter_route(name)
        if server:
            mcp.setdefault(server, []).append(name)
        elif name in builtins:
            (opt_in if name in OPT_IN_TOOLS else core).append(name)
        else:
            plugins.setdefault(owners.get(name, UNATTRIBUTED), []).append(name)
    return {
        "core": sorted(core),
        "plugins": {k: sorted(v) for k, v in sorted(plugins.items())},
        "mcp": {k: sorted(v) for k, v in sorted(mcp.items())},
        "opt_in": sorted(opt_in),
    }


def _load(agent_id: str) -> tuple[Any, str, dict[str, str]]:
    """The blocking half: the manifest and the plugin manifests. Off the loop."""
    from robothor.engine.config import load_agent_config_or_reason

    config, reason = load_agent_config_or_reason(agent_id, _manifest_dir(), "admin_tools")
    return config, reason, (_plugin_owners() if config is not None else {})


async def agent_tools(agent_id: str) -> tuple[int, dict[str, Any]]:
    """``(status, body)`` for one agent.

    The registry is read ON the loop, as every run reads it. Off it,
    ``_get_filtered_names`` would spend its warn-once flag for an agent with an
    unresolved tool while the escalation it pairs with, which needs a running
    loop, did nothing -- so the operator would never be told. It would also
    iterate ``_schemas`` while the loop may be changing it.
    """
    from robothor.engine.tools import registry as tool_registry

    config, reason, owners = await asyncio.to_thread(_load, agent_id)
    if config is None:
        status = 404 if reason.startswith("Agent config not found") else 422
        return status, {"error": reason}
    registry = tool_registry.get_registry()
    # The run path refreshes before it builds; so does this, so the dialog
    # right after a plugin install shows what the next run will get.
    registry._refresh_plugin_schemas_if_stale()
    names = list(registry.get_tool_names(config))
    return 200, {
        "agent": config.id,
        "groups": group_tools(names, registry, owners),
        "allowed": list(config.tools_allowed or []),
        "denied": list(config.tools_denied or []),
        "count": len(names),
    }


def mcp_servers() -> list[dict[str, Any]]:
    """The pool's servers, without ``command`` or ``url``, with a tool count."""
    from robothor.engine.mcp_client import get_mcp_client_pool
    from robothor.engine.tools import registry as tool_registry

    registry = tool_registry.get_registry()
    counts: dict[str, int] = {}
    for name in registry.adapter_tool_names():
        route = registry.get_adapter_route(name)
        if route:
            counts[route] = counts.get(route, 0) + 1
    servers: list[dict[str, Any]] = []
    for server in get_mcp_client_pool().list_servers():
        row = {k: server[k] for k in _SHOWN if k in server}
        row["tool_count"] = counts.get(str(server.get("name")), 0)
        servers.append(row)
    return servers


def register(app: FastAPI) -> None:
    """Mount the agent-tools and MCP-server reads on the engine app."""
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/api/admin", tags=["admin"])

    @router.get("/agents/{agent_id}/tools")
    async def get_agent_tools(agent_id: str) -> Any:
        """The agent's tools by source, and its manifest's allow and deny lists."""
        status, body = await agent_tools(agent_id)
        if status != 200:
            return JSONResponse(body, status_code=status)
        return body

    @router.get("/mcp/servers")
    async def get_mcp_servers() -> dict[str, Any]:
        """Each MCP server's health and tool count. Never how to reach it."""
        return {"servers": mcp_servers()}

    app.include_router(router)
