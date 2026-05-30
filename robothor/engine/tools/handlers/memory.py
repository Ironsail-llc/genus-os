"""Memory tool handlers — search, store, entity, blocks, stats."""

from __future__ import annotations

import asyncio
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


@_handler("search_memory")
async def _search_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.feature_flags import is_rip_enabled

    # RIP 15: route through the query-classed memory router instead of the
    # hard-coded full fan-out. Falls back below when the flag is off.
    if is_rip_enabled(15):
        return await _search_memory_routed(args, ctx)

    from robothor.engine.feature_flags import narrow_memory_search_enabled
    from robothor.memory.facts import search_facts
    from robothor.memory.outcomes import log_fact_access

    # R2: by default the tool fans out (entities + insights + episodes) on every
    # call — expensive for a narrow lookup. When MEMORY_NARROW_SEARCH is on, a
    # call defaults to facts-only and the caller opts into fan-out via args.
    fan_out = not narrow_memory_search_enabled()
    results = await search_facts(
        args.get("query", ""),
        limit=args.get("limit", 10),
        tenant_id=ctx.tenant_id,
        expand_entities=bool(args.get("expand_entities", fan_out)),
        include_insights=bool(args.get("include_insights", fan_out)),
        include_episodes=bool(args.get("include_episodes", fan_out)),
    )

    # Log fact access for outcome attribution (best-effort).
    run_id = getattr(ctx, "run_id", None)
    agent_id = getattr(ctx, "agent_id", None)
    if run_id:
        fact_ids = [
            r["id"]
            for r in results
            if r.get("source") in (None, "fact", "entity_expansion") and r.get("id")
        ]
        if fact_ids:
            await asyncio.to_thread(log_fact_access, str(run_id), fact_ids, agent_id, ctx.tenant_id)

    return {
        "results": [
            {
                "fact": r.get("fact_text") or r.get("insight_text") or "",
                "category": r.get("category", "")
                if isinstance(r.get("category"), str)
                else (r.get("categories") or [None])[0] or "",
                "confidence": r.get("confidence", 0),
                "similarity": round(r.get("similarity", 0), 4),
                "source": r.get("source", "fact"),
            }
            for r in results
        ]
    }


async def _search_memory_routed(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """RIP 15 path — query-classed routing via robothor.memory.router."""
    from robothor.memory.outcomes import log_fact_access
    from robothor.memory.router import recall

    out = await recall(
        args.get("query", ""),
        limit=args.get("limit", 10),
        tenant_id=ctx.tenant_id,
    )
    results = out["results"]

    # Outcome attribution: only fact-sourced rows feed the blame loop.
    run_id = getattr(ctx, "run_id", None)
    agent_id = getattr(ctx, "agent_id", None)
    if run_id:
        fact_ids = [r["id"] for r in results if r.get("source") == "fact" and r.get("id")]
        if fact_ids:
            await asyncio.to_thread(log_fact_access, str(run_id), fact_ids, agent_id, ctx.tenant_id)

    return {
        "query_class": out["query_class"],
        "results": [
            {
                "fact": r.get("text", ""),
                "category": r.get("category", ""),
                "source": r.get("source", "fact"),
                "score": round(r.get("score", 0) or 0, 4),
            }
            for r in results
        ],
    }


@_handler("store_memory")
async def _store_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.memory.facts import extract_facts, store_fact

    content = args.get("content", "")
    content_type = args.get("content_type", "conversation")
    facts = await extract_facts(content)
    if facts:
        stored_ids = [
            await store_fact(f, content, content_type, tenant_id=ctx.tenant_id) for f in facts
        ]
        return {"id": stored_ids[0], "facts_stored": len(stored_ids)}
    fact = {"fact_text": content, "category": "personal", "entities": [], "confidence": 0.5}
    fact_id = await store_fact(fact, content, content_type, tenant_id=ctx.tenant_id)
    return {"id": fact_id, "facts_stored": 1}


@_handler("get_entity")
async def _get_entity(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.memory.entities import get_entity

    try:
        result = await get_entity(args.get("name", ""), tenant_id=ctx.tenant_id)
        return result or {"name": args.get("name", ""), "found": False}
    except Exception:
        return {"name": args.get("name", ""), "found": False}


@_handler("get_stats")
async def _get_stats(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.memory.facts import get_memory_stats

    return await asyncio.to_thread(get_memory_stats, tenant_id=ctx.tenant_id)


@_handler("memory_block_read")
async def _memory_block_read(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.memory.blocks import read_block

    return await asyncio.to_thread(read_block, args.get("block_name", ""), tenant_id=ctx.tenant_id)


@_handler("memory_block_write")
async def _memory_block_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.memory.blocks import write_block

    return await asyncio.to_thread(
        write_block,
        args.get("block_name", ""),
        args.get("content", ""),
        tenant_id=ctx.tenant_id,
    )


@_handler("memory_block_list")
async def _memory_block_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.memory.blocks import list_blocks

    return await asyncio.to_thread(list_blocks, tenant_id=ctx.tenant_id)


@_handler("get_knowledge_gaps")
async def _get_knowledge_gaps(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.memory.gap_analysis import analyze_knowledge_gaps

    return await analyze_knowledge_gaps()


@_handler("record_procedure")
async def _record_procedure(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Save a reusable procedure (steps, prerequisites, tags)."""
    from robothor.memory.procedures import record_procedure

    proc_id = await record_procedure(
        name=args.get("name", ""),
        steps=list(args.get("steps") or []),
        description=args.get("description", ""),
        prerequisites=list(args.get("prerequisites") or []),
        applicable_tags=list(args.get("tags") or []),
        created_by_agent=getattr(ctx, "agent_id", "unknown"),
        tenant_id=ctx.tenant_id,
    )
    return {"id": proc_id, "name": args.get("name", "")}


@_handler("find_procedure")
async def _find_procedure(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Find procedures applicable to a task (semantic + optional tag filter)."""
    from robothor.memory.procedures import find_applicable_procedures

    results = await find_applicable_procedures(
        task_description=args.get("task", ""),
        tags=list(args.get("tags") or []) or None,
        limit=args.get("limit", 3),
        tenant_id=ctx.tenant_id,
    )
    return {
        "procedures": [
            {
                "id": r["id"],
                "name": r["name"],
                "description": r["description"],
                "steps": r["steps"],
                "prerequisites": r["prerequisites"],
                "tags": r["applicable_tags"],
                "success_count": r["success_count"],
                "failure_count": r["failure_count"],
                "confidence": r["confidence"],
                "similarity": round(r.get("similarity", 0), 4),
            }
            for r in results
        ]
    }


@_handler("report_procedure_outcome")
async def _report_procedure_outcome(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Record success/failure of a procedure you just applied."""
    from robothor.memory.procedures import report_procedure_outcome

    return await report_procedure_outcome(
        procedure_id=int(args.get("procedure_id", 0)),
        success=bool(args.get("success", False)),
        notes=args.get("notes", ""),
        tenant_id=ctx.tenant_id,
    )


@_handler("leave_breadcrumb")
async def _leave_breadcrumb(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Persist mid-task state so the next run picks up where you left off.

    `content` may be a short note string or a structured dict — both are
    accepted. The next run this agent performs will see the latest 5
    breadcrumbs in its warmup context.
    """
    from robothor.memory.breadcrumbs import leave_breadcrumb

    content = args.get("content", "")
    agent_id = getattr(ctx, "agent_id", "unknown")
    run_id = getattr(ctx, "run_id", None)
    bc_id = await asyncio.to_thread(
        leave_breadcrumb,
        agent_id,
        content,
        str(run_id) if run_id else None,
        args.get("ttl_days", 7),
        ctx.tenant_id,
    )
    return {"breadcrumb_id": bc_id, "agent_id": agent_id}


@_handler("append_to_block")
async def _append_to_block(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.crm.dal import append_to_block

    ok = await asyncio.to_thread(
        append_to_block,
        block_name=args.get("block_name", ""),
        entry=args.get("entry", ""),
        max_entries=args.get("maxEntries", 20),
        tenant_id=ctx.tenant_id,
    )
    return {"success": ok, "block_name": args.get("block_name", "")}
