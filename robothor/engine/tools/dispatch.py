"""Tool execution router — dispatches tool calls to handler modules."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from robothor.config import Config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolContext:
    """Context passed to every tool handler."""

    agent_id: str = ""
    tenant_id: str = "robothor-primary"
    workspace: str = ""


def _cfg() -> Config:
    """Lazy config access (not module-level to avoid import-time side effects)."""
    from robothor.config import get_config

    return get_config()


def _collect_handlers() -> dict[str, Any]:
    """Collect all HANDLERS dicts from handler modules."""
    from robothor.engine.tools.handlers import (
        browser,
        crm,
        desktop,
        experiment,
        federation,
        filesystem,
        git,
        gws,
        mcp_client,
        memory,
        messaging,
        observability,
        pdf,
        pf,
        reasoning,
        skills,
        spawn,
        vault,
        vision,
        voice,
        web,
    )

    all_handlers: dict[str, Any] = {}
    for mod in [
        memory,
        vision,
        web,
        filesystem,
        crm,
        browser,
        desktop,
        experiment,
        git,
        gws,
        vault,
        observability,
        voice,
        spawn,
        pdf,
        reasoning,
        federation,
        pf,
        messaging,
        skills,
        mcp_client,
    ]:
        all_handlers.update(mod.HANDLERS)
    return all_handlers


# Lazily initialized handler map
_handler_map: dict[str, Any] | None = None


def _get_handlers() -> dict[str, Any]:
    global _handler_map
    if _handler_map is None:
        _handler_map = _collect_handlers()
    return _handler_map


async def _execute_tool(
    name: str,
    args: dict[str, Any],
    *,
    agent_id: str = "",
    tenant_id: str = "robothor-primary",
    workspace: str = "",
) -> dict[str, Any]:
    """Route tool call to the correct handler.

    Checks adapter-provided tools first (dynamic MCP servers), then falls
    through to hardcoded engine handlers.
    """
    # Check if this tool is provided by a business adapter
    from robothor.engine.tools import get_registry

    route = get_registry().get_adapter_route(name)
    if route:
        from robothor.engine.mcp_client import get_mcp_client_pool

        try:
            pool = get_mcp_client_pool()
            session = await pool.get_session(route)
            return await session.call_tool(name, args)
        except Exception as e:
            logger.error("Adapter tool %s (server=%s) failed: %s", name, route, e)
            return {"error": f"Adapter tool '{name}' failed: {e}"}

    ctx = ToolContext(agent_id=agent_id, tenant_id=tenant_id, workspace=workspace)
    handlers = _get_handlers()
    handler = handlers.get(name)
    if handler is None:
        return {"error": f"Unknown tool: {name}"}
    return cast("dict[str, Any]", await handler(args, ctx))
