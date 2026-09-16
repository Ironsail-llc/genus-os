"""Deferred / searchable tool meta-tools (Rip 16 / G4 — tools-as-code).

When an agent's toolset is deferred, the model is advertised only a small CORE
set plus three meta-tools defined here:

- ``tool_search(query)`` — find allowed tools not currently in the toolset.
- ``tool_describe(name)`` — fetch one tool's full parameter schema.
- ``tool_call(name, arguments)`` — invoke a discovered tool.

Security: ``tool_describe`` and ``tool_call`` check the requested name against
``_allowed_names()`` — the agent's allow-set published by the runner via
``set_deferred_allowed`` (the ``_deferred_allowed`` ContextVar), NOT the
``set_tool_whitelist`` path. A ``tool_call`` to a tool outside the agent's
allow-list is refused here, before ``registry.execute`` is ever reached — no
privilege escalation. The searchable set is likewise scoped to that allow-set,
so the model only discovers tools it may actually run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from robothor.engine.tools.constants import TOOLSEARCH_TOOLS
from robothor.engine.tools.dispatch import get_agent_toolset, get_deferred_allowed

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


def _searchable_names() -> tuple[list[str], bool]:
    """``(names, deferred)`` — what this run may search, and which set it is.

    On a deferred run that is the allow-set the agent reaches through
    ``tool_call``. On any other run it is the agent's own advertised toolset,
    which the runner publishes for exactly this reason: ``tool_search`` used to
    answer "only available on deferred runs" and teach the agent nothing about
    what it does have. Searching the tools it already holds is a smaller answer
    but a true one, and the agent stops guessing.
    """
    deferred = _allowed_names()
    if deferred:
        return deferred, True
    toolset = get_agent_toolset()
    if not toolset:
        return [], False
    return sorted(n for n in toolset if n not in TOOLSEARCH_TOOLS), False


def _closest(name: str, candidates: list[str], limit: int = 3) -> list[str]:
    """The nearest allowed names to one the agent got wrong.

    A model that has read instruction text naming ``gws_gmail_draft`` or
    ``gws_calendar_update`` — neither of which this platform registers — gets
    "unknown tool" and no way forward. Ranked by the same scorer the search
    uses, on the wrong name read as a query, so "gws_gmail_draft" reaches the
    Gmail family rather than whatever sorts nearest alphabetically.
    """
    from robothor.engine.tools.registry import get_registry

    query = name.replace("_", " ")
    hits = get_registry().search_tools(candidates, query, limit=limit)
    return [hit["name"] for hit in hits if hit["name"] != name][:limit]


async def _tool_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.tools.registry import get_registry

    query = str(args.get("query", "")).strip()
    if not query:
        return {"error": "query is required"}
    try:
        limit = int(args.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10

    names, deferred = _searchable_names()
    registry = get_registry()
    note = registry.absent_capability_note(query)

    if not names:
        # Not an error: a run that published no toolset is a harness detail,
        # and there is nothing the agent can do about it either way.
        out: dict[str, Any] = {
            "results": [],
            "count": 0,
            "hint": "this run published no toolset, so there is nothing to search",
        }
        if note:
            out["note"] = note
        return out

    results = registry.search_tools(names, query, limit=limit)
    for hit in results:
        hit["in_toolset"] = not deferred
    out = {
        "results": results,
        "count": len(results),
        "hint": (
            "Use tool_describe(name=...) for the full schema, then "
            "tool_call(name=..., arguments=...)."
            if deferred
            else "These are already in your toolset — call them directly, not via tool_call."
        ),
    }
    if note:
        out["note"] = note
    return out


async def _tool_describe(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.tools.registry import get_registry

    name = str(args.get("name", "")).strip()
    if not name:
        return {"error": "name is required"}
    if name in TOOLSEARCH_TOOLS:
        return {"error": f"{name} is a meta-tool and cannot be described/called"}
    candidates, _deferred = _searchable_names()
    if name not in candidates:
        return {
            "error": f"tool '{name}' is not in your allow-list",
            "did_you_mean": _closest(name, candidates),
        }
    schema = get_registry().get_schema(name)
    if not schema:
        return {
            "error": f"unknown tool: {name}",
            "did_you_mean": _closest(name, candidates),
        }
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
        # The suggestion is computed from the SAME set the check just refused,
        # so it can never name a tool the agent may not call.
        return {
            "error": f"tool '{name}' is not in your allow-list",
            "did_you_mean": _closest(name, _allowed_names()),
        }
    tool_args = args.get("arguments", {})
    if not isinstance(tool_args, dict):
        return {"error": "arguments must be an object"}

    # The out-of-allow-list check above (against the _deferred_allowed set) is
    # what prevents escalation; we never reach registry.execute for a denied
    # tool. tool_call grants no reach beyond the agent's own allow-list.
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
