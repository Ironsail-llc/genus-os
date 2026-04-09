"""Claude Managed Agents integration — standalone execution backend.

This package provides a second execution lane for running agents on
Anthropic's hosted Managed Agents infrastructure.  It is strictly additive:
nothing in the existing engine is modified.

Usage::

    from robothor.engine.managed_agents import run_on_managed_agents

    result = await run_on_managed_agents(
        agent_id="main",
        message="Analyze the quarterly report",
        model="claude-sonnet-4-6",
    )
    print(result.output_text)
"""

from robothor.engine.managed_agents.client import ManagedAgentsClient, get_ma_client
from robothor.engine.managed_agents.runner import run_on_managed_agents

__all__ = ["ManagedAgentsClient", "get_ma_client", "run_on_managed_agents"]
