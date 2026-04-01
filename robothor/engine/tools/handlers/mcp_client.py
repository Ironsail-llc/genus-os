"""MCP client tool handlers — call tools on external MCP servers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


@_handler("mcp_list_servers")
async def _mcp_list_servers(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List configured external MCP servers and their status."""
    from robothor.engine.mcp_client import get_mcp_client_pool

    pool = get_mcp_client_pool()
    return {"servers": pool.list_servers()}


@_handler("mcp_list_tools")
async def _mcp_list_tools(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List tools available on a specific MCP server."""
    from robothor.engine.mcp_client import get_mcp_client_pool

    server_name = args.get("server_name", "")
    if not server_name:
        return {"error": "server_name is required"}

    pool = get_mcp_client_pool()
    try:
        session = await pool.get_session(server_name)
        tools = await session.list_tools()
        return {"server": server_name, "tools": tools}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to list tools from '{server_name}': {e}"}


@_handler("mcp_call_tool")
async def _mcp_call_tool(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Call a tool on an external MCP server."""
    from robothor.engine.mcp_client import get_mcp_client_pool

    server_name = args.get("server_name", "")
    tool_name = args.get("tool_name", "")
    arguments = args.get("arguments", {})

    if not server_name or not tool_name:
        return {"error": "server_name and tool_name are required"}

    pool = get_mcp_client_pool()
    try:
        session = await pool.get_session(server_name)
        result = await session.call_tool(tool_name, arguments)
        return {"server": server_name, "tool": tool_name, "result": result}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"MCP tool call failed: {e}"}


@_handler("mcp_read_resource")
async def _mcp_read_resource(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Read a resource from an external MCP server."""
    from robothor.engine.mcp_client import get_mcp_client_pool

    server_name = args.get("server_name", "")
    uri = args.get("uri", "")

    if not server_name or not uri:
        return {"error": "server_name and uri are required"}

    pool = get_mcp_client_pool()
    try:
        session = await pool.get_session(server_name)
        result = await session.read_resource(uri)
        return {"server": server_name, "uri": uri, "result": result}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"MCP resource read failed: {e}"}
