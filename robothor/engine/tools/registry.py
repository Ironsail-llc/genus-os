"""Tool Registry — schema filtering + execution for the Agent Engine."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from robothor.engine.tools.constants import SPAWN_TOOLS, TODO_TOOLS
from robothor.engine.tools.dispatch import _execute_tool
from robothor.engine.tools.schemas import get_engine_schemas

if TYPE_CHECKING:
    from robothor.engine.models import AgentConfig

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registry of available tools with schema filtering per agent."""

    def __init__(self) -> None:
        self._schemas: dict[str, dict[str, Any]] = {}
        self._adapter_routes: dict[str, str] = {}  # tool_name → adapter server name
        self._register_all()

    def _register_all(self) -> None:
        """Register all tool schemas."""
        from robothor.api.mcp import get_tool_definitions

        # MCP tools
        for defn in get_tool_definitions():
            name = defn["name"]
            self._schemas[name] = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": defn["description"],
                    "parameters": defn["inputSchema"],
                },
            }

        # Engine-specific tools
        self._schemas.update(get_engine_schemas())

    # Adapter connection failure cache: {adapter_name: (fail_time, backoff_seconds)}
    _adapter_failures: dict[str, tuple[float, float]] = {}

    async def register_adapter_tools(self, adapters: list[Any]) -> None:
        """Connect to adapter MCP servers, discover tools, register as first-class schemas.

        Resilience features:
        - 5s timeout per adapter connection (don't block agent startup)
        - Failed adapters cached with exponential backoff (5min initial, 30min max)
        - Failures logged once at WARNING, then suppressed until retry window
        """
        import asyncio
        import time

        from robothor.engine.mcp_client import get_mcp_client_pool

        pool = get_mcp_client_pool()
        for adapter in adapters:
            # Check failure cache — skip if in backoff window
            now = time.monotonic()
            if adapter.name in self._adapter_failures:
                fail_time, backoff = self._adapter_failures[adapter.name]
                if now - fail_time < backoff:
                    logger.debug(
                        "Adapter '%s': skipping (backoff %.0fs remaining)",
                        adapter.name,
                        backoff - (now - fail_time),
                    )
                    continue

            try:
                session = await asyncio.wait_for(pool.get_session(adapter.name), timeout=5.0)
                mcp_tools = await asyncio.wait_for(session.list_tools(), timeout=5.0)
                for tool in mcp_tools:
                    name = tool.get("name", "")
                    if not name:
                        continue
                    self._schemas[name] = {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": tool.get("description", ""),
                            "parameters": tool.get(
                                "inputSchema", {"type": "object", "properties": {}}
                            ),
                        },
                    }
                    self._adapter_routes[name] = adapter.name
                # Clear failure cache on success
                self._adapter_failures.pop(adapter.name, None)
                logger.info("Adapter '%s': discovered %d tools", adapter.name, len(mcp_tools))
            except Exception as e:
                # Exponential backoff: 300s (5min) -> 600s -> 1200s -> max 1800s (30min)
                _, prev_backoff = self._adapter_failures.get(adapter.name, (0, 150.0))
                new_backoff = min(prev_backoff * 2, 1800.0)
                self._adapter_failures[adapter.name] = (now, new_backoff)
                if prev_backoff <= 150.0:
                    # First failure or first retry — log at WARNING
                    logger.warning(
                        "Adapter '%s' unavailable (backoff %.0fs): %s",
                        adapter.name,
                        new_backoff,
                        e,
                    )
                else:
                    logger.debug(
                        "Adapter '%s' still unavailable, backoff %.0fs", adapter.name, new_backoff
                    )

    def get_adapter_route(self, tool_name: str) -> str | None:
        """Return the adapter server name for a tool, or None if not adapter-provided."""
        return self._adapter_routes.get(tool_name)

    def build_for_agent(self, config: AgentConfig) -> list[dict[str, Any]]:
        """Return filtered tool schemas for an agent based on allow/deny lists."""
        names = self._get_filtered_names(config)
        return [self._schemas[n] for n in names]

    def build_readonly_for_agent(self, config: AgentConfig) -> list[dict[str, Any]]:
        """Return only read-only tool schemas for plan mode."""
        from robothor.engine.tools.constants import READONLY_TOOLS

        full_names = set(self.get_tool_names(config))
        readonly_names = sorted(full_names & READONLY_TOOLS)
        return [self._schemas[n] for n in readonly_names if n in self._schemas]

    def get_readonly_tool_names(self, config: AgentConfig) -> list[str]:
        """Return read-only tool names for plan mode."""
        from robothor.engine.tools.constants import READONLY_TOOLS

        full_names = set(self.get_tool_names(config))
        return sorted(full_names & READONLY_TOOLS)

    def get_tool_names(self, config: AgentConfig) -> list[str]:
        """Return filtered tool names for an agent."""
        return self._get_filtered_names(config)

    def _get_filtered_names(self, config: AgentConfig) -> list[str]:
        if config.tools_allowed:
            names = [n for n in config.tools_allowed if n in self._schemas]
        else:
            names = list(self._schemas.keys())

        if config.tools_denied:
            # Support glob patterns (e.g. "mcp_*", "gws_*") in tools_denied
            has_globs = any(c in p for p in config.tools_denied for c in "*?[")
            if has_globs:
                from fnmatch import fnmatch

                names = [n for n in names if not any(fnmatch(n, p) for p in config.tools_denied)]
            else:
                denied = set(config.tools_denied)
                names = [n for n in names if n not in denied]

        # Exclude spawn tools unless agent has can_spawn_agents enabled
        if not config.can_spawn_agents:
            names = [n for n in names if n not in SPAWN_TOOLS]

        # Exclude todo list tools unless agent has todo_list_enabled
        if not config.todo_list_enabled:
            names = [n for n in names if n not in TODO_TOOLS]

        return names

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        agent_id: str = "",
        tenant_id: str = "",
        workspace: str = "",
        user_id: str = "",
        user_role: str = "",
        accessible_tenant_ids: tuple[str, ...] = (),
        timeout: int = 120,
    ) -> dict[str, Any]:
        """Execute a tool and return the result dict.

        Args:
            timeout: Per-tool timeout in seconds. 0 = unlimited.
        """
        _extra = {
            "user_id": user_id,
            "user_role": user_role,
            "accessible_tenant_ids": accessible_tenant_ids,
        }
        try:
            if timeout > 0:
                async with asyncio.timeout(timeout):
                    return await _execute_tool(
                        tool_name,
                        arguments,
                        agent_id=agent_id,
                        tenant_id=tenant_id,
                        workspace=workspace,
                        **_extra,
                    )
            else:
                return await _execute_tool(
                    tool_name,
                    arguments,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    workspace=workspace,
                    **_extra,
                )
        except TimeoutError:
            logger.warning("Tool %s timed out after %ds", tool_name, timeout)
            return {
                "error": f"Tool '{tool_name}' timed out after {timeout}s. "
                "Try a different approach or skip this step."
            }
        except Exception as e:
            logger.error("Tool %s failed: %s", tool_name, e, exc_info=True)
            return {"error": f"Tool execution failed: {e}"}


# Singleton
_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    """Get or create the singleton tool registry."""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry
