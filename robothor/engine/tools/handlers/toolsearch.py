"""Deferred / searchable tool meta-tools (Rip 16 / G4 — tools-as-code).

When an agent's toolset is deferred, the model is advertised only a small CORE
set plus three meta-tools defined here:

- ``tool_search(query)`` — find allowed tools not currently in the toolset.
- ``tool_describe(name)`` — fetch one tool's full parameter schema.
- ``tool_call(name, arguments)`` — invoke a discovered tool.

Security: ``tool_call`` forwards to ``registry.execute``, which enforces the
per-task whitelist the runner installs for deferred runs. So a ``tool_call`` to
a tool outside the agent's allow-list is refused by the same gate that guards
direct calls — no privilege escalation. The searchable set is likewise scoped
to that whitelist, so the model only discovers tools it may actually run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from robothor.engine.tools.constants import TOOLSEARCH_TOOLS
from robothor.engine.tools.dispatch import get_deferred_allowed

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _allowed_names() -> list[str]:
    """The agent's allowed tools for this deferred run (from the deferred
    allow-set the runner installs), excluding the meta-tools themselves."""
    allowed = get_deferred_allowed()
    if not allowed:
        return []
    return sorted(n for n in allowed if n not in TOOLSEARCH_TOOLS)


async def _tool_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.tools.registry import get_registry

    query = str(args.get("query", "")).strip()
    if not query:
        return {"error": "query is required"}
    try:
        limit = int(args.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10

    names = _allowed_names()
    if not names:
        return {
            "error": "tool_search is only available on deferred runs",
            "results": [],
        }
    results = get_registry().search_tools(names, query, limit=limit)
    return {
        "results": results,
        "count": len(results),
        "hint": "Use tool_describe(name=...) for the full schema, then tool_call(name=..., arguments=...).",
    }


async def _tool_describe(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.tools.registry import get_registry

    name = str(args.get("name", "")).strip()
    if not name:
        return {"error": "name is required"}
    if name in TOOLSEARCH_TOOLS:
        return {"error": f"{name} is a meta-tool and cannot be described/called"}
    if name not in _allowed_names():
        return {"error": f"tool '{name}' is not in your allow-list"}
    schema = get_registry().get_schema(name)
    if not schema:
        return {"error": f"unknown tool: {name}"}
    fn = schema.get("function", {})
    return {
        "name": name,
        "description": fn.get("description", ""),
        "parameters": fn.get("parameters", {}),
    }


async def _tool_call(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.tools.registry import get_registry

    name = str(args.get("name", "")).strip()
    if not name:
        return {"error": "name is required"}
    if name in TOOLSEARCH_TOOLS:
        return {"error": f"{name} cannot invoke itself or another meta-tool"}
    if name not in _allowed_names():
        # Defense in depth: deferral shrinks the advertised schema list, so the
        # agent's tools_denied / allow-list is enforced HERE for indirect calls.
        return {"error": f"tool '{name}' is not in your allow-list"}
    tool_args = args.get("arguments", {})
    if not isinstance(tool_args, dict):
        return {"error": "arguments must be an object"}

    # Forward to the registry. The per-task whitelist (installed by the runner
    # for deferred runs) is re-checked inside _execute_tool, so a call to a
    # denied/out-of-allow-list tool is refused here exactly as a direct call
    # would be — tool_call grants no extra reach.
    return await get_registry().execute(
        name,
        tool_args,
        agent_id=ctx.agent_id,
        run_id=ctx.run_id,
        tenant_id=ctx.tenant_id,
        workspace=ctx.workspace,
        user_id=ctx.user_id,
        user_role=ctx.user_role,
        accessible_tenant_ids=ctx.accessible_tenant_ids,
        task_author_override=ctx.task_author_override,
        is_benchmark=ctx.is_benchmark,
    )


HANDLERS["tool_search"] = _tool_search
HANDLERS["tool_describe"] = _tool_describe
HANDLERS["tool_call"] = _tool_call
