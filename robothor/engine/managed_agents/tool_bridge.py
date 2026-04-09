"""Convert engine tool schemas to Managed Agents custom-tool format.

Reads from the existing ``ToolRegistry`` (read-only) and produces the
tool list required by the ``/v1/agents`` endpoint.

Built-in MA tools (bash, read, write, etc.) are optionally enabled via
``agent_toolset_20260401`` when sandboxed execution is desired.  Engine
tools that overlap with the built-in set are excluded from the custom
list to avoid duplicates.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.models import AgentConfig
    from robothor.engine.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Engine tool names that map to MA built-in tools.
# When the sandbox is enabled these run inside the MA cloud container.
_BUILTIN_OVERLAP: frozenset[str] = frozenset(
    {
        "exec",  # → MA bash
        "read_file",  # → MA read
        "write_file",  # → MA write
        "list_directory",  # → MA glob
        "web_fetch",  # → MA web_fetch
        "web_search",  # → MA web_search
    }
)


def engine_schema_to_ma_custom(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert one OpenAI-format tool schema to MA custom-tool format.

    Input::

        {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}

    Output::

        {"type": "custom", "name": "...", "description": "...", "input_schema": {...}}
    """
    func = schema.get("function", schema)
    return {
        "type": "custom",
        "name": func["name"],
        "description": func.get("description", ""),
        "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
    }


def build_ma_tools_for_agent(
    registry: ToolRegistry,
    agent_config: AgentConfig,
    *,
    enable_builtin_sandbox: bool = False,
) -> list[dict[str, Any]]:
    """Build the full MA tools list for an agent manifest.

    Parameters
    ----------
    registry
        The engine's existing tool registry (read-only — nothing is mutated).
    agent_config
        The engine ``AgentConfig`` for the target agent.
    enable_builtin_sandbox
        When *True*, include ``agent_toolset_20260401`` (MA built-in bash,
        read, write, edit, glob, grep, web_fetch, web_search) and omit
        overlapping engine tools from the custom-tool list.

    Returns
    -------
    list[dict]
        Tool definitions in MA API format, ready for ``create_agent()``.
    """
    # Read schemas from registry without mutating it
    schemas = registry.build_for_agent(agent_config)

    tools: list[dict[str, Any]] = []
    skip_names: set[str] = set()

    if enable_builtin_sandbox:
        tools.append({"type": "agent_toolset_20260401"})
        skip_names = set(_BUILTIN_OVERLAP)

    for schema in schemas:
        func = schema.get("function", schema)
        name = func.get("name", "")
        if not name:
            continue
        if name in skip_names:
            continue
        try:
            tools.append(engine_schema_to_ma_custom(schema))
        except (KeyError, TypeError):
            logger.warning("Skipping malformed tool schema: %s", name)

    return tools


def build_ma_tools_from_names(
    registry: ToolRegistry,
    tool_names: list[str],
    *,
    enable_builtin_sandbox: bool = False,
) -> list[dict[str, Any]]:
    """Build MA tools from an explicit list of tool names.

    Useful when calling ``run_on_managed_agents()`` with a specific
    subset of tools rather than a full ``AgentConfig``.
    """
    all_schemas: dict[str, dict[str, Any]] = {}
    for schema in registry._schemas.values():
        func = schema.get("function", schema)
        name = func.get("name", "")
        if name:
            all_schemas[name] = schema

    tools: list[dict[str, Any]] = []
    skip_names: set[str] = set()

    if enable_builtin_sandbox:
        tools.append({"type": "agent_toolset_20260401"})
        skip_names = set(_BUILTIN_OVERLAP)

    for name in tool_names:
        if name in skip_names:
            continue
        tool_schema = all_schemas.get(name)
        if tool_schema:
            tools.append(engine_schema_to_ma_custom(tool_schema))
        else:
            logger.warning("Tool %r not found in registry, skipping", name)

    return tools
