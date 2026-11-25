"""Memory tool handlers — search, store, entity, blocks, stats."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

from robothor.constants import SANDBOX_DENIAL_PREFIX

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _hidden_blocks() -> frozenset[str]:
    from robothor.tenancy.watch import SNAPSHOT_BLOCK

    return frozenset({SNAPSHOT_BLOCK})


#: Memory blocks that hold ENGINE state, not an agent's memory: no agent-facing
#: surface reads, writes, appends to or lists them (these handlers, deep
#: reasoning's block reader in ``rlm_tool.py``, the warm-up's memory-block
#: section). ``org_watch_snapshot`` names child companies and decides what the
#: owner is told next, so only ``robothor.tenancy.watch`` touches it.
AGENT_HIDDEN_BLOCKS = _hidden_blocks()

#: What an agent is told when it names one.
HIDDEN_BLOCK_ERROR = "That memory block holds engine state; agents cannot read or change it."


def is_hidden_block(name: Any) -> bool:
    """Whether ``name`` is one of :data:`AGENT_HIDDEN_BLOCKS`, however it is spelled."""
    return str(name or "").strip().lower() in AGENT_HIDDEN_BLOCKS


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
        # Procedures and breadcrumbs are durable agent state read back by the
        # next real run's warmup, exactly like a block. They were on the
        # harness deny-list and nowhere else until 2026-09-12, and that list is
        # computed from the graded agent's `tools_allowed` — which is empty for
        # an agent that restricts nothing, leaving the deny-list empty too.
        "record_procedure",
        "report_procedure_outcome",
        "leave_breadcrumb",
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
                        "error": f"{SANDBOX_DENIAL_PREFIX} {_name} writes are disabled",
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


#: Result sources whose ``id`` is a ``memory_facts`` id (an insight's or an
#: episode's is not).
_FACT_SOURCES = (None, "fact", "entity_expansion")


def _schedule_access_recording(results: list[dict[str, Any]], tenant_id: str) -> None:
    """Count the returned facts as used, without the search waiting for it.

    The UPDATE runs in a worker thread under a tracked background task (the
    same shape as conversation ingest), so neither this request nor the event
    loop waits on it. Any failure is logged, never raised: the caller is a
    retrieval.
    """
    from robothor.memory.outcomes import record_fact_access

    fact_ids = [r["id"] for r in results if r.get("source") in _FACT_SOURCES and r.get("id")]
    if not fact_ids:
        return

    async def _record() -> None:
        try:
            await asyncio.to_thread(record_fact_access, tenant_id, fact_ids=fact_ids)
        except Exception as exc:  # noqa: BLE001 - best-effort accounting
            logger.warning("search_memory: recording fact access failed: %s", exc)

    coro = _record()
    try:
        from robothor.engine.task_registry import get_task_registry

        get_task_registry().spawn(coro, name="memory:record-access")
    except Exception as exc:  # noqa: BLE001 - best-effort accounting
        coro.close()
        logger.warning("search_memory: could not schedule fact access recording: %s", exc)


@_handler("search_memory")
async def _search_memory(
    args: dict[str, Any], ctx: ToolContext, *, record_access: bool = True
) -> dict[str, Any]:
    """Search memory, applying identity scoping regardless of which path runs.

    Facts returned count as used (``access_count``/``last_accessed``, the decay
    and prune inputs) unless ``record_access`` is False — for a caller that
    must have no side effects. The recording never delays or fails the search.

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
    # Conversation segments are scoped on EVERY call, whatever the rollout mode:
    # verbatim conversation is more sensitive than an extracted fact. The reader
    # (their person and their user id) sees the conversations they own and the
    # workspaces they belong to; nothing else.
    reader_person_id: str | None = getattr(identity, "person_id", None) or None
    reader_user_id: str | None = getattr(ctx, "user_id", "") or None
    room_ids = await _reader_room_ids(ctx)

    recall_path = _recall_routed if is_rip_enabled(15) else _recall_fallback
    results, query_class = await recall_path(
        args,
        ctx,
        scope=_query_scope,
        reader_person_id=reader_person_id,
        reader_user_id=reader_user_id,
        room_ids=room_ids,
    )

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
        fact_ids = [r["id"] for r in results if r.get("source") in _FACT_SOURCES and r.get("id")]
        if fact_ids:
            await asyncio.to_thread(
                log_fact_access,
                str(run_id),
                fact_ids,
                getattr(ctx, "agent_id", None),
                ctx.tenant_id,
            )

    if record_access:
        _schedule_access_recording(results, ctx.tenant_id)

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


async def _reader_room_ids(ctx: ToolContext) -> list[str]:
    """The workspaces whose conversation the caller may recall.

    The room this run is in (a room turn's carrier), plus every room the
    calling user is a member of. Empty for a run with neither — no workspace
    conversation is then searched. A lookup failure is logged and narrows the
    result; it never widens it.
    """
    from robothor.rooms.run_context import current_room_run

    rooms: set[str] = set()
    tenant_id = getattr(ctx, "tenant_id", "")
    user_id = getattr(ctx, "user_id", "")
    run = current_room_run()
    if run is not None and run.tenant_id == tenant_id:
        rooms.add(str(run.room_id))
    if user_id and tenant_id:
        try:
            from robothor.rooms import dal as rooms_dal

            summaries = await asyncio.to_thread(rooms_dal.list_rooms_for_user, tenant_id, user_id)
            rooms.update(str(s.id) for s in summaries)
        except Exception as e:  # noqa: BLE001 - fewer rooms, never more
            logger.warning(
                "search_memory: workspace lookup failed (%s); workspace segments omitted",
                type(e).__name__,
            )
    return sorted(rooms)


async def _recall_fallback(
    args: dict[str, Any],
    ctx: ToolContext,
    *,
    scope: Any = None,
    reader_person_id: str | None = None,
    room_ids: list[str] | None = None,
    reader_user_id: str | None = None,
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
        reader_person_id=reader_person_id,
        reader_user_id=reader_user_id,
        room_ids=room_ids,
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

    A conversation segment (``kind: "segment"``) also says where it came from:
    its ``topic``, ``source_kind``, ``source_ref`` (the session), the turn
    range ``start_ref``..``end_ref`` (chat message ids, with ``part`` for a
    long turn split in pieces) and ``started_at``/``ended_at`` (ISO 8601).
    Facts keep exactly their shape.
    """
    return {
        "query_class": query_class,
        "results": [{**_format_row(r), **_segment_fields(r)} for r in rows],
    }


def _format_row(r: dict[str, Any]) -> dict[str, Any]:
    return {
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


def _segment_fields(r: dict[str, Any]) -> dict[str, Any]:
    if r.get("kind") != "segment":
        return {}

    def iso(value: Any) -> Any:
        return value.isoformat() if hasattr(value, "isoformat") else value

    return {
        "kind": "segment",
        "topic": r.get("topic"),
        "source_kind": r.get("source_kind"),
        "source_ref": r.get("source_ref"),
        "start_ref": r.get("start_ref"),
        "end_ref": r.get("end_ref"),
        "part": r.get("part", 0),
        "started_at": iso(r.get("started_at")),
        "ended_at": iso(r.get("ended_at")),
    }


async def _recall_routed(
    args: dict[str, Any],
    ctx: ToolContext,
    *,
    scope: Any = None,
    reader_person_id: str | None = None,
    room_ids: list[str] | None = None,
    reader_user_id: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """RIP 15 path — query-classed routing via robothor.memory.router."""
    from robothor.memory.router import recall

    out = await recall(
        args.get("query", ""),
        limit=args.get("limit", 10),
        tenant_id=ctx.tenant_id,
        scope=scope,
        reader_person_id=reader_person_id,
        reader_user_id=reader_user_id,
        room_ids=room_ids,
    )
    return out["results"], out["query_class"]


def _argument_error(
    tool: str, required: tuple[str, ...], missing: list[str], args: dict[str, Any]
) -> str:
    """An error the calling model can act on: what it lacked and what it sent."""
    received = ", ".join(sorted(args)) or "nothing"
    return (
        f"{tool} needs {' and '.join(required)} "
        f"(missing: {', '.join(missing)}; received: {received})"
    )


@_handler("store_memory")
async def _store_memory(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.memory.write_jobs import async_write_enabled, enqueue_write, process_write_job

    content = args.get("content", "")
    content_type = args.get("content_type", "conversation")
    if not str(content or "").strip():
        return {"error": _argument_error("store_memory", ("content",), ["content"], args)}

    if not async_write_enabled():
        return await store_memory_content(content, content_type, tenant_id=ctx.tenant_id)

    # Record the promise, then hand the work off. The row is written first on
    # purpose: a crash during extraction must still leave evidence that a write
    # was owed, because the caller has already been told it succeeded. The row
    # carries the write's origin and scope, so the replay stores it the same way.
    job_id = await enqueue_write(
        content,
        content_type=content_type,
        tenant_id=ctx.tenant_id,
        agent_id=getattr(ctx, "agent_id", None),
        run_id=getattr(ctx, "run_id", None),
        context=store_memory_context(ctx.tenant_id, content_type),
    )
    if job_id is None:
        # Refused at the boundary — a benchmark run writing outside the sandbox
        # tenant. Nothing was queued, so there is no job to poll and reporting
        # "queued" would promise a write that will never happen.
        return {
            "error": f"{SANDBOX_DENIAL_PREFIX} store_memory writes are disabled",
            "guard": "is_benchmark",
        }

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

# The whole call's budget, inside the same 120s wall. Conflict classification
# (one short model call, only for a fact with a similar existing one) gets what
# extraction left of it; past it, a fact is stored as new rather than lost.
REQUEST_PATH_BUDGET = 105.0


def store_memory_context(
    tenant_id: str, content_type: str, scope: tuple[str, str | None] | None = None
) -> Any:
    """The write context of a ``store_memory`` call.

    What the assistant writes to memory is agent-generated (external content it
    relays stays external). Shared within the tenant, as it always was: the
    tool never makes a fact private on its own. ``scope`` is for a caller that
    explicitly asks for a private write (none does yet).
    """
    from robothor.memory.write_path import WriteContext

    return WriteContext.for_source(
        tenant_id=tenant_id,
        source_channel="",
        content_type=content_type or "conversation",
        role="assistant",
        scope=scope,
    )


async def store_memory_content(
    content: str,
    content_type: str = "conversation",
    *,
    tenant_id: str = "",
    extract_timeout: float = REQUEST_PATH_EXTRACT_TIMEOUT,
    scope: tuple[str, str | None] | None = None,
) -> dict[str, Any]:
    """Extract facts from content and store them. One implementation, two callers.

    The engine handler and robothor/api/mcp.py both had their own copy of this
    logic. They had already diverged — the MCP copy passed no tenant, so every
    write through it landed in DEFAULT_TENANT without dedup — and any fix
    applied to one silently missed the other.

    Goes through the one write path (``robothor.memory.write_path``), like every
    other memory write: one extraction, then per fact the quality gate, conflict
    handling (so a contradiction of an operator-confirmed fact leaves that fact
    active), the fact with its origin and scope, its mentions and its relations
    with evidence. No model call is added beyond conflict classification, which
    runs only for a fact with a similar existing one and is cut off at
    ``REQUEST_PATH_BUDGET``. If extraction fails or finds nothing, the raw
    content is stored rather than dropped (``metadata.extraction =
    "fallback_raw"``), and a failure is reported.
    """
    import time

    from robothor.constants import DEFAULT_TENANT
    from robothor.memory import write_path

    if not (content or "").strip():
        return {"error": "content is required: there is nothing to store"}

    started = time.monotonic()
    context = store_memory_context(tenant_id or DEFAULT_TENANT, content_type, scope)
    result = await write_path.ingest_text(
        content,
        context,
        extract_timeout=extract_timeout,
        fallback_on_empty=True,
        conflict_check=True,
        deadline=started + REQUEST_PATH_BUDGET,
    )
    stored = list(result.get("fact_ids") or [])
    existing = list(result.get("existing_ids") or [])
    out: dict[str, Any] = {
        "id": (stored or existing or [None])[0],
        "facts_stored": len(stored),
    }
    for key in (
        "facts_skipped",
        "facts_kept_confirmed",
        "conflict_checks_timed_out",
        "relations_stored",
        "relations_private_deferred",
        "facts_failed",
        "graph_errors",
    ):
        if result.get(key):
            out[key] = result[key]
    if existing:
        out["already_known_ids"] = existing
    if result.get("extraction_failed"):
        out["extraction_failed"] = True
        out["extraction_error"] = result.get("extraction_error")
    if result.get("facts_failed"):
        reasons = "; ".join(result.get("errors") or []) or "unknown error"
        if not stored and not existing:
            # Nothing was kept: the call failed, and the agent must be told so.
            out["error"] = f"memory not stored: {reasons}"
        else:
            out["warning"] = f"{result['facts_failed']} fact(s) not stored: {reasons}"
    return out


@_handler("record_resolution")
async def _record_resolution(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Record that an open item/alert/decision is resolved and retire it."""
    from robothor.memory.resolution import record_resolution

    missing = [k for k in ("open_item", "outcome") if not str(args.get(k) or "").strip()]
    if missing:
        return {
            "error": _argument_error("record_resolution", ("open_item", "outcome"), missing, args)
        }

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

    if is_hidden_block(args.get("block_name")):
        return {"error": HIDDEN_BLOCK_ERROR}
    return await asyncio.to_thread(read_block, args.get("block_name", ""), tenant_id=ctx.tenant_id)


@_handler("memory_block_write")
async def _memory_block_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.memory.blocks import write_block

    if is_hidden_block(args.get("block_name")):
        return {"error": HIDDEN_BLOCK_ERROR}
    return await asyncio.to_thread(
        write_block,
        args.get("block_name", ""),
        args.get("content", ""),
        tenant_id=ctx.tenant_id,
    )


@_handler("memory_block_list")
async def _memory_block_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.memory.blocks import list_blocks

    listed: dict[str, Any] = await asyncio.to_thread(list_blocks, tenant_id=ctx.tenant_id)
    blocks = listed.get("blocks")
    if isinstance(blocks, list):
        listed["blocks"] = [b for b in blocks if not is_hidden_block(b.get("name"))]
    return listed


@_handler("get_knowledge_gaps")
async def _get_knowledge_gaps(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.memory.gap_analysis import analyze_knowledge_gaps

    return await analyze_knowledge_gaps(tenant_id=ctx.tenant_id)


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

    if is_hidden_block(args.get("block_name")):
        return {"error": HIDDEN_BLOCK_ERROR, "success": False}
    ok = await asyncio.to_thread(
        append_to_block,
        block_name=args.get("block_name", ""),
        entry=args.get("entry", ""),
        max_entries=args.get("maxEntries", 20),
        tenant_id=ctx.tenant_id,
    )
    return {"success": ok, "block_name": args.get("block_name", "")}
