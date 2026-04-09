"""Tool execution router — dispatches tool calls to handler modules."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from robothor.constants import DEFAULT_TENANT

if TYPE_CHECKING:
    from robothor.config import Config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolContext:
    """Context passed to every tool handler."""

    agent_id: str = ""
    tenant_id: str = field(default_factory=lambda: DEFAULT_TENANT)
    workspace: str = ""


def get_db() -> Any:
    """Standard DB connection for tool handlers.

    Usage::

        with get_db() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            conn.commit()
    """
    from robothor.db.connection import get_connection

    return get_connection()


def _cfg() -> Config:
    """Lazy config access (not module-level to avoid import-time side effects)."""
    from robothor.config import get_config

    return get_config()


def _collect_handlers() -> dict[str, Any]:
    """Collect all HANDLERS dicts from handler modules."""
    from robothor.engine.tools.handlers import (  # noqa: E501
        apollo,
        benchmark,
        browser,
        crm,
        desktop,
        devops_metrics,
        experiment,
        federation,
        filesystem,
        git,
        github_api,
        gws,
        identity,
        jira,
        mcp_client,
        memory,
        messaging,
        observability,
        pdf,
        pf,
        reasoning,
        reports,
        skills,
        spawn,
        timing,
        todolist,
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
        apollo,
        filesystem,
        crm,
        browser,
        desktop,
        experiment,
        benchmark,
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
        jira,
        github_api,
        devops_metrics,
        identity,
        reports,
        mcp_client,
        timing,
        todolist,
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


def _audit_tool_call(
    tool_name: str,
    agent_id: str,
    tenant_id: str,
    *,
    status: str = "ok",
    error: str | None = None,
) -> None:
    """Record a tool invocation in the audit log (non-blocking, never raises)."""
    try:
        from robothor.audit.logger import log_event

        details: dict[str, Any] = {"tenant_id": tenant_id}
        if error:
            details["error"] = error[:500]
        log_event(
            event_type="agent.tool_call",
            action=tool_name,
            category="agent",
            actor=agent_id or "unknown",
            details=details,
            status=status,
        )
    except Exception:
        pass


async def _execute_tool(
    name: str,
    args: dict[str, Any],
    *,
    agent_id: str = "",
    tenant_id: str = "",
    workspace: str = "",
) -> dict[str, Any]:
    """Route tool call to the correct handler.

    Checks adapter-provided tools first (dynamic MCP servers), then falls
    through to hardcoded engine handlers.
    """
    from robothor.engine.tools import get_registry

    route = get_registry().get_adapter_route(name)
    if route:
        from robothor.engine.mcp_client import get_mcp_client_pool

        try:
            pool = get_mcp_client_pool()
            session = await pool.get_session(route)
            result: dict[str, Any] = await session.call_tool(name, args)
            _audit_tool_call(name, agent_id, tenant_id)
            return result
        except Exception as e:
            logger.error("Adapter tool %s (server=%s) failed: %s", name, route, e)
            _audit_tool_call(name, agent_id, tenant_id, status="error", error=str(e))
            return {"error": f"Adapter tool '{name}' failed: {e}"}

    ctx = ToolContext(agent_id=agent_id, tenant_id=tenant_id, workspace=workspace)
    handlers = _get_handlers()
    handler = handlers.get(name)
    if handler is None:
        return {"error": f"Unknown tool: {name}"}

    result = cast("dict[str, Any]", await handler(args, ctx))
    if isinstance(result, dict) and "error" in result:
        _audit_tool_call(name, agent_id, tenant_id, status="error", error=result["error"])
    else:
        _audit_tool_call(name, agent_id, tenant_id)
    return result
