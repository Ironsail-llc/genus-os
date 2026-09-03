"""What tools a run gets, and how plan mode wraps its prompt.

Extracted from `execute`, which is 1,132 lines. Two things happen here that
are easy to get subtly wrong and were covered by no test:

* **The plan-mode sandwich.** Constraints go BEFORE the identity prompt and the
  reminder AFTER, so plan rules are not buried in the middle of SOUL.md's
  directives. Appending both at the end — the obvious simplification — puts the
  rules where a long identity prompt drowns them.
* **Adapter loading is non-fatal.** An external MCP server that is down must
  cost the agent that server's tools, not the whole run. `v2.mcp_servers` was
  dead code until it was wired in here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from robothor.engine.sanitize import sanitize_log as _sanitize

logger = logging.getLogger(__name__)


@dataclass
class PreparedToolset:
    tool_schemas: list[dict[str, Any]]
    tool_names: list[str]
    system_prompt: str


async def prepare_toolset(
    registry: Any,
    agent_config: Any,
    *,
    agent_id: str,
    system_prompt: str,
    readonly_mode: bool,
    deep_plan: bool,
) -> PreparedToolset:
    """Load this agent's adapters, then pick and wrap its toolset."""
    await _load_adapters(registry, agent_config, agent_id)

    if not readonly_mode:
        return PreparedToolset(
            tool_schemas=registry.build_for_agent(agent_config),
            tool_names=registry.get_tool_names(agent_config),
            system_prompt=system_prompt,
        )

    from robothor.engine.prompts import (
        DEEP_PLAN_PREAMBLE,
        DEEP_PLAN_SUFFIX,
        PLAN_MODE_PREAMBLE,
        PLAN_MODE_SUFFIX,
    )

    tool_schemas = registry.build_readonly_for_agent(agent_config)
    tool_names = registry.get_readonly_tool_names(agent_config)

    if deep_plan:
        wrapped = DEEP_PLAN_PREAMBLE + system_prompt + DEEP_PLAN_SUFFIX
    else:
        # Name the tools the agent actually has: a plan written against tools
        # it cannot call is a plan that fails at execution time.
        tool_list = ", ".join(f"`{t}`" for t in sorted(tool_names)) if tool_names else "(none)"
        preamble = PLAN_MODE_PREAMBLE.replace("{tool_names_placeholder}", tool_list)
        wrapped = preamble + system_prompt + PLAN_MODE_SUFFIX

    return PreparedToolset(tool_schemas=tool_schemas, tool_names=tool_names, system_prompt=wrapped)


async def _load_adapters(registry: Any, agent_config: Any, agent_id: str) -> None:
    """Best-effort. A dead MCP server costs its own tools and nothing else."""
    try:
        from robothor.engine.adapters import get_adapters_for_agent
        from robothor.engine.mcp_client import configure_mcp_servers, register_adapter

        if agent_config.mcp_servers:
            configure_mcp_servers(agent_config.mcp_servers)

        adapters = get_adapters_for_agent(agent_id)
        for adapter in adapters:
            register_adapter(adapter)
        if adapters:
            await registry.register_adapter_tools(adapters)
    except Exception as e:  # noqa: BLE001
        logger.warning("Adapter loading failed (non-fatal): %s", _sanitize(e))
