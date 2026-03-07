"""Memory tool handlers — search, store, entity, blocks, stats."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


@_handler("search_memory")
async def _search_memory(args: dict, ctx: ToolContext) -> dict:
    from robothor.memory.facts import search_facts

    results = await search_facts(args.get("query", ""), limit=args.get("limit", 10))
    return {
        "results": [
            {
                "fact": r["fact_text"],
                "category": r["category"],
                "confidence": r["confidence"],
                "similarity": round(r.get("similarity", 0), 4),
            }
            for r in results
        ]
    }


@_handler("store_memory")
async def _store_memory(args: dict, ctx: ToolContext) -> dict:
    from robothor.memory.facts import extract_facts, store_fact

    content = args.get("content", "")
    content_type = args.get("content_type", "conversation")
    facts = await extract_facts(content)
    if facts:
        stored_ids = [await store_fact(f, content, content_type) for f in facts]
        return {"id": stored_ids[0], "facts_stored": len(stored_ids)}
    fact = {"fact_text": content, "category": "personal", "entities": [], "confidence": 0.5}
    fact_id = await store_fact(fact, content, content_type)
    return {"id": fact_id, "facts_stored": 1}


@_handler("get_entity")
async def _get_entity(args: dict, ctx: ToolContext) -> dict:
    from robothor.memory.entities import get_entity

    try:
        result = await get_entity(args.get("name", ""))
        return result or {"name": args.get("name", ""), "found": False}
    except Exception:
        return {"name": args.get("name", ""), "found": False}


@_handler("get_stats")
async def _get_stats(args: dict, ctx: ToolContext) -> dict:
    from robothor.memory.facts import get_memory_stats

    return await asyncio.to_thread(get_memory_stats)


@_handler("memory_block_read")
async def _memory_block_read(args: dict, ctx: ToolContext) -> dict:
    from robothor.memory.blocks import read_block

    return await asyncio.to_thread(read_block, args.get("block_name", ""))


@_handler("memory_block_write")
async def _memory_block_write(args: dict, ctx: ToolContext) -> dict:
    from robothor.memory.blocks import write_block

    return await asyncio.to_thread(write_block, args.get("block_name", ""), args.get("content", ""))


@_handler("memory_block_list")
async def _memory_block_list(args: dict, ctx: ToolContext) -> dict:
    from robothor.memory.blocks import list_blocks

    return await asyncio.to_thread(list_blocks)


@_handler("append_to_block")
async def _append_to_block(args: dict, ctx: ToolContext) -> dict:
    from robothor.crm.dal import append_to_block

    ok = await asyncio.to_thread(
        append_to_block,
        block_name=args.get("block_name", ""),
        entry=args.get("entry", ""),
        max_entries=args.get("maxEntries", 20),
    )
    return {"success": ok, "block_name": args.get("block_name", "")}
