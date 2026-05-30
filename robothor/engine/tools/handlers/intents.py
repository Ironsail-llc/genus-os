"""Intent-memory tool handlers — add / search / list / advance.

Gated by ``ROBOTHOR_RIP_14_ENABLED``. Agent-created intents are always
``stated`` (the operator asked for them via the agent); the agent cannot
mint ``inferred`` intents — those come from the nightly pass and require
HMAC confirmation.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from robothor.engine.feature_flags import is_rip_enabled

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}

_RIP = 14
_DISABLED = {"error": "intent memory disabled (set ROBOTHOR_RIP_14_ENABLED=1)"}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


@_handler("intent_add")
async def _intent_add(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not is_rip_enabled(_RIP):
        return dict(_DISABLED)
    from robothor.memory.intents import upsert_intent

    try:
        intent_id = await upsert_intent(
            args.get("title", ""),
            args.get("description", ""),
            horizon=args.get("horizon", "ongoing"),
            priority=int(args.get("priority", 3)),
            source="stated",
            tenant_id=ctx.tenant_id,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"id": intent_id, "title": args.get("title", "")}


@_handler("intent_search")
async def _intent_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not is_rip_enabled(_RIP):
        return dict(_DISABLED)
    from robothor.memory.intents import search_intents

    results = await search_intents(
        args.get("query", ""),
        limit=args.get("limit", 5),
        status=args.get("status", "active") or None,
        tenant_id=ctx.tenant_id,
    )
    return {"intents": results}


@_handler("intent_list")
async def _intent_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not is_rip_enabled(_RIP):
        return dict(_DISABLED)
    from robothor.memory.intents import list_active_intents

    return {
        "intents": await asyncio.to_thread(
            list_active_intents, limit=args.get("limit", 10), tenant_id=ctx.tenant_id
        )
    }


@_handler("intent_advance")
async def _intent_advance(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Record that you advanced an intent, optionally linking a session goal."""
    if not is_rip_enabled(_RIP):
        return dict(_DISABLED)
    from robothor.memory.intents import link_goal, mark_advanced

    intent_id = int(args.get("id", 0))
    goal_id = args.get("goal_id")
    if goal_id:
        ok = await asyncio.to_thread(link_goal, intent_id, int(goal_id), tenant_id=ctx.tenant_id)
    else:
        ok = await asyncio.to_thread(mark_advanced, intent_id, tenant_id=ctx.tenant_id)
    return {"ok": ok, "id": intent_id}
