"""Tool Registry for the Agent Engine.

This package was decomposed from a single tools.py file.
All public symbols are re-exported here for backward compatibility.
"""

from __future__ import annotations

from typing import Any

# ── Core exports ──
from robothor.engine.tools.constants import (
    GIT_TOOLS,
    GWS_TOOLS,
    IMPETUS_TOOLS,
    PROTECTED_BRANCHES,
    READONLY_TOOLS,
    SPAWN_TOOLS,
)
from robothor.engine.tools.dispatch import _execute_tool

# ── Handler exports (used by tests and runner) ──
from robothor.engine.tools.handlers.gws import _handle_gws_tool, _run_gws
from robothor.engine.tools.handlers.pdf import _handle_analyze_pdf, _parse_page_range
from robothor.engine.tools.handlers.spawn import (
    MAX_CONCURRENT_SPAWNS,
    _current_spawn_context,
    _get_spawn_semaphore,
    _handle_spawn_agent,
    _handle_spawn_agents,
    get_runner,
    set_runner,
)
from robothor.engine.tools.registry import ToolRegistry, get_registry

__all__ = [
    # Constants
    "GIT_TOOLS",
    "GWS_TOOLS",
    "IMPETUS_TOOLS",
    "PROTECTED_BRANCHES",
    "READONLY_TOOLS",
    "SPAWN_TOOLS",
    # Registry
    "ToolRegistry",
    "get_registry",
    # Dispatch
    "_execute_tool",
    # Spawn
    "MAX_CONCURRENT_SPAWNS",
    "set_runner",
    "get_runner",
    "_current_spawn_context",
    "_get_spawn_semaphore",
    "_handle_spawn_agent",
    "_handle_spawn_agents",
    # GWS
    "_handle_gws_tool",
    "_run_gws",
    # PDF
    "_handle_analyze_pdf",
    "_parse_page_range",
]


# ── Backward-compat shim for _handle_sync_tool ──
# Tests import this to call individual sync tool handlers directly.
# Route through the async dispatch path.
def _handle_sync_tool(
    name: str,
    args: dict[str, Any],
    *,
    agent_id: str = "",
    tenant_id: str = "robothor-primary",
    workspace: str = "",
) -> dict[str, Any]:
    """Backward-compatible sync wrapper. Used by tests that call tool handlers directly."""
    import asyncio

    coro = _execute_tool(
        name,
        args,
        agent_id=agent_id,
        tenant_id=tenant_id,
        workspace=workspace,
    )
    # If we're already in an async context, use a new event loop in a thread
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already in async context (e.g. pytest-asyncio) — run in a new thread
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)
