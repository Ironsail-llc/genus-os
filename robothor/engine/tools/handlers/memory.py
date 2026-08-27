"""Memory tool handlers — search, store, entity, blocks, stats."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}

# Memory-mutating tools — refused when ctx.is_benchmark. Mirrors the
# gws and crm mutating-tool guards: benchmark fixture text stored as memory_facts or
# agent blocks is read back by later real runs as established fact
# (the "recurring pattern" self-reinforcement). record_resolution is
# included because it retires real open items/alerts. Reads
# (search_memory, memory_block_read, get_entity, …) stay allowed.
_MEMORY_MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "store_memory",
        "append_to_block",
        "memory_block_write",
        "record_resolution",
    }
)


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in _MEMORY_MUTATING_TOOLS:

            async def gated(
                args: dict[str, Any],
                ctx: ToolContext,
                _fn: Callable[..., Any] = fn,
                _name: str = name,
            ) -> dict[str, Any]:
                if ctx.is_benchmark:
                    return {
                        "error": f"benchmark sandbox: {_name} writes are disabled",
                        "guard": "is_benchmark",
                    }
                return cast("dict[str, Any]", await _fn(args, ctx))

            HANDLERS[name] = gated
        else:
            HANDLERS[name] = fn
        return fn

    return decorator


logger = logging.getLogger(__name__)

#: A provider that hangs must not hang recall.
PROVIDER_TIMEOUT = 5.0


async def merge_plugin_memory(query: str, builtin: list[str], *, limit: int = 10) -> list[str]:
    """Built-in recall, then anything installed providers contribute.

    Order is the contract. Built-in rows keep their positions and contributed
    rows are appended, so a package can add to what the operator remembers and
    can never displace it — memory is the subsystem this platform is furthest
    ahead on, and a takeover there would cost more than the feature is worth.

    Every contributed row is prefixed with its provider so the agent can tell
    an outside claim from its own memory. A provider that raises, hangs or
    returns something that is not a list of strings is dropped: recall must
    survive a bad third-party package, not fail with it.
    """
    try:
        from robothor.plugins import load_plugins

        loaded = load_plugins(reserved_names=set())
        providers = loaded.memory or {}
    except Exception as exc:  # noqa: BLE001 - recall must not depend on plugins
        logger.warning("Memory providers unavailable: %s", exc)
        return list(builtin)

    if not providers:
        return list(builtin)

    merged = list(builtin)
    for name, spec in providers.items():
        search = spec.get("search") if isinstance(spec, dict) else None
        if not callable(search):
            logger.warning("Memory provider %r skipped: no callable 'search'", name)
            continue
        try:
            rows = await asyncio.wait_for(search(query, limit), timeout=PROVIDER_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - one provider, not all recall
            logger.warning("Memory provider %r failed: %s", name, exc)
            continue
        if not isinstance(rows, list):
            logger.warning("Memory provider %r skipped: search did not return a list", name)
            continue
        merged.extend(f"[{name}] {row}" for row in rows if isinstance(row, str) and row)
    return merged


@_handler("search_memory")
async def _search_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Search memory, applying identity scoping regardless of which path runs.

    Scoping is computed *above* the RIP 15 branch deliberately. It previously
    lived inside the fallback body, so enabling the router silently skipped it:
    the routed path never read ctx.identity and never passed a scope, meaning
    promoting ROBOTHOR_DATA_SCOPING to enforce would have filtered nothing
    through the primary memory read tool. Hoisting it makes the shape of the
    code enforce the invariant rather than a reviewer's memory.
    """
    from robothor.engine.feature_flags import data_scoping_mode, is_rip_enabled
    from robothor.identity.scope import (
        log_would_drop,
        observe_scope,
        rows_dropped_by_scope,
        scope_for_query,
    )
    from robothor.memory.outcomes import log_fact_access

    # Task 5 (Unified Identity Context) — "own data + shared" row scoping.
    # off/observe never touch the query; enforce restricts it to the caller's
    # own person_id (+ org-general rows) for non-privileged identities.
    identity = getattr(ctx, "identity", None)
    _scoping_mode = data_scoping_mode()
    _query_scope = scope_for_query(_scoping_mode, identity)

    if is_rip_enabled(15):
        results, query_class = await _recall_routed(args, ctx, scope=_query_scope)
    else:
        results, query_class = await _recall_fallback(args, ctx, scope=_query_scope)

    # Observe runs against the raw rows, which still carry person_id — the
    # formatted output deliberately does not expose it.
    _observe_scope = observe_scope(_scoping_mode, identity)
    if _observe_scope:
        log_would_drop(
            tool_name="search_memory",
            user_id=ctx.user_id,
            scope=_observe_scope,
            dropped=rows_dropped_by_scope(results, _observe_scope),
            table="memory_facts",
        )

    # Log fact access for outcome attribution (best-effort).
    run_id = getattr(ctx, "run_id", None)
    if run_id:
        fact_ids = [
            r["id"]
            for r in results
            if r.get("source") in (None, "fact", "entity_expansion") and r.get("id")
        ]
        if fact_ids:
            await asyncio.to_thread(
                log_fact_access,
                str(run_id),
                fact_ids,
                getattr(ctx, "agent_id", None),
                ctx.tenant_id,
            )

    formatted = _format_results(results, query_class)

    # Anything installed memory providers contribute, appended after the
    # instance's own rows and attributed. Providers augment recall; they can
    # never displace it — see merge_plugin_memory.
    _facts = [r["fact"] for r in formatted["results"] if r.get("fact")]
    _merged = await merge_plugin_memory(
        str(args.get("query") or ""), _facts, limit=int(args.get("limit") or 10)
    )
    for extra in _merged[len(_facts) :]:
        formatted["results"].append(
            {
                "id": None,
                "fact": extra,
                "category": "",
                "source": "plugin",
                "confidence": 0,
                "similarity": 0,
                "score": 0,
            }
        )
    return formatted


async def _recall_fallback(
    args: dict[str, Any], ctx: ToolContext, *, scope: Any = None
) -> tuple[list[dict[str, Any]], str]:
    """Direct search_facts path — returns raw rows plus a query-class label."""
    from robothor.engine.feature_flags import narrow_memory_search_enabled
    from robothor.memory.facts import search_facts
    from robothor.memory.router import classify_query

    # R2: by default the tool fans out (entities + insights + episodes) on every
    # call — expensive for a narrow lookup. When MEMORY_NARROW_SEARCH is on, a
    # call defaults to facts-only and the caller opts into fan-out via args.
    fan_out = not narrow_memory_search_enabled()
    query = args.get("query", "")

    results = await search_facts(
        query,
        limit=args.get("limit", 10),
        tenant_id=ctx.tenant_id,
        expand_entities=bool(args.get("expand_entities", fan_out)),
        include_insights=bool(args.get("include_insights", fan_out)),
        include_episodes=bool(args.get("include_episodes", fan_out)),
        scope=scope,
    )
    # classify_query is a pure regex with no I/O, so the label is available on
    # this path too — the output shape stays identical across the flag.
    return results, classify_query(query)


def _format_results(rows: list[dict[str, Any]], query_class: str) -> dict[str, Any]:
    """One output shape for both read paths.

    The routed and fallback paths used to emit different keys — the fallback
    gave `confidence`/`similarity`, the routed one gave `score` and a
    `query_class`, and neither gave `id`. That meant flipping RIP 15 changed
    the tool's contract as a side effect of changing its retrieval, so a revert
    would bundle a schema change into a behaviour change.

    This emits the superset on every call. `id` is included because outcome
    attribution already depends on it and because a caller cannot reason about
    row identity — scoping, dedup, citation — without it.
    """
    return {
        "query_class": query_class,
        "results": [
            {
                "id": r.get("id"),
                "fact": r.get("fact_text") or r.get("insight_text") or r.get("text") or "",
                "category": r.get("category", "")
                if isinstance(r.get("category"), str)
                else (r.get("categories") or [None])[0] or "",
                "source": r.get("source") or "fact",
                "confidence": r.get("confidence", 0),
                "similarity": round(r.get("similarity") or 0, 4),
                "score": round(r.get("score") or r.get("rrf_score") or 0, 4),
            }
            for r in rows
        ],
    }


async def _recall_routed(
    args: dict[str, Any], ctx: ToolContext, *, scope: Any = None
) -> tuple[list[dict[str, Any]], str]:
    """RIP 15 path — query-classed routing via robothor.memory.router."""
    from robothor.memory.router import recall

    out = await recall(
        args.get("query", ""),
        limit=args.get("limit", 10),
        tenant_id=ctx.tenant_id,
        scope=scope,
    )
    return out["results"], out["query_class"]


@_handler("store_memory")
async def _store_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.memory.write_jobs import async_write_enabled, enqueue_write, process_write_job

    content = args.get("content", "")
    content_type = args.get("content_type", "conversation")

    if not async_write_enabled():
        return await store_memory_content(content, content_type, tenant_id=ctx.tenant_id)

    # Record the promise, then hand the work off. The row is written first on
    # purpose: a crash during extraction must still leave evidence that a write
    # was owed, because the caller has already been told it succeeded.
    job_id = await enqueue_write(
        content,
        content_type=content_type,
        tenant_id=ctx.tenant_id,
        agent_id=getattr(ctx, "agent_id", None),
        run_id=getattr(ctx, "run_id", None),
    )

    from robothor.engine.task_registry import get_task_registry

    get_task_registry().spawn(process_write_job(job_id), name=f"memory-write:{job_id}")

    # facts_stored is deliberately absent rather than guessed. It cannot be
    # known synchronously, and reporting a number here would guarantee the model
    # narrates a fabricated count.
    return {
        "status": "queued",
        "job_id": job_id,
        "content_chars": len(content),
        "note": (
            "Facts are extracted asynchronously and are typically searchable "
            "within a minute. Read-after-write is not immediate; use "
            "memory_write_status with this job_id if confirmation matters."
        ),
    }


@_handler("memory_write_status")
async def _memory_write_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Check a deferred memory write.

    Kept out of the default tool set on purpose — adding it everywhere would
    inflate every agent's schema for a case most runs never need. Agents that
    genuinely require write confirmation opt in via their manifest.
    """
    from robothor.memory.write_jobs import job_status

    job_id = args.get("job_id")
    if job_id is None:
        return {"error": "job_id is required"}
    status = await job_status(int(job_id))
    return status or {"error": f"no such memory write job: {job_id}"}


# Budget for extraction when a caller is waiting. The tool wall is 120s
# (runner.py:2471); leaving headroom means a slow extraction returns a result
# instead of being killed at the wall, which is what 15 of 121 calls did.
REQUEST_PATH_EXTRACT_TIMEOUT = 70.0


async def store_memory_content(
    content: str,
    content_type: str = "conversation",
    *,
    tenant_id: str = "",
    extract_timeout: float = REQUEST_PATH_EXTRACT_TIMEOUT,
) -> dict[str, Any]:
    """Extract facts from content and store them. One implementation, two callers.

    The engine handler and robothor/api/mcp.py both had their own copy of this
    logic. They had already diverged — the MCP copy passed no tenant, so every
    write through it landed in DEFAULT_TENANT without dedup — and any fix
    applied to one silently missed the other.

    Uses store_facts_batch rather than a per-fact loop. Note this is *not* the
    latency win it looks like: measured warm, extraction is ~23s and embedding
    ~0.12s per fact, so batching saves fractions of a second. It is here because
    it makes the write atomic — the old loop committed each fact on its own
    connection, so a mid-loop failure left a partial write behind while the
    caller was told the call had failed.
    """
    from robothor.memory.facts import extract_facts, store_facts_batch

    facts = await extract_facts(content, timeout=extract_timeout)
    if not facts:
        # No facts, or extraction timed out. Storing the raw content keeps the
        # information rather than dropping it; the metadata tag lets dechurn and
        # any later reprocessing find these rows.
        facts = [
            {
                "fact_text": content,
                "category": "personal",
                "entities": [],
                "confidence": 0.5,
                "metadata": {"extraction": "fallback_raw"},
            }
        ]

    stored_ids = await store_facts_batch(facts, content, content_type, tenant_id=tenant_id)
    return {
        "id": stored_ids[0] if stored_ids else None,
        "facts_stored": len(stored_ids),
    }


@_handler("record_resolution")
async def _record_resolution(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Record that an open item/alert/decision is resolved and retire it."""
    from robothor.memory.resolution import record_resolution

    return await record_resolution(
        open_item=args.get("open_item", ""),
        outcome=args.get("outcome", ""),
        confirmed_by=args.get("confirmed_by", ""),
        tenant_id=ctx.tenant_id,
        agent_id=getattr(ctx, "agent_id", "unknown"),
    )


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
