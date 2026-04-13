"""Sub-agent spawn tool handlers."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from robothor.engine.models import SpawnContext

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner
    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}

# ─── Runner reference (for spawn_agent tool) ─────────────────────────
_runner_ref: AgentRunner | None = None
_engine_config: EngineConfig | None = None


def set_runner(runner: AgentRunner, engine_config: EngineConfig | None = None) -> None:
    """Register the runner instance (called by daemon on startup)."""
    global _runner_ref, _engine_config
    _runner_ref = runner
    _engine_config = engine_config


def get_runner() -> AgentRunner | None:
    """Get the registered runner instance."""
    return _runner_ref


# ─── Spawn context (async-safe via contextvars) ──────────────────────
_current_spawn_context: ContextVar[SpawnContext | None] = ContextVar(
    "_current_spawn_context", default=None
)

# ─── Concurrency semaphore for sub-agent spawns ──────────────────────
_spawn_semaphore: asyncio.Semaphore | None = None
_spawn_semaphore_size: int = 0
DEFAULT_MAX_CONCURRENT_SPAWNS = 10


def _get_spawn_semaphore() -> asyncio.Semaphore:
    """Get or create the spawn concurrency semaphore.

    Uses the engine config's max_concurrent_spawns if available,
    otherwise falls back to DEFAULT_MAX_CONCURRENT_SPAWNS.
    """
    global _spawn_semaphore, _spawn_semaphore_size
    target = (
        _engine_config.max_concurrent_spawns if _engine_config else DEFAULT_MAX_CONCURRENT_SPAWNS
    )
    if _spawn_semaphore is None or _spawn_semaphore_size != target:
        _spawn_semaphore = asyncio.Semaphore(target)
        _spawn_semaphore_size = target
    return _spawn_semaphore


async def _handle_spawn_agent(
    args: dict[str, Any],
    ctx: ToolContext | None = None,
    *,
    agent_id: str = "",
) -> dict[str, Any]:
    """Spawn a single child agent and wait for its result."""
    from robothor.engine.config import load_agent_config
    from robothor.engine.models import DeliveryMode, TriggerType

    # Support both ToolContext and direct agent_id kwarg
    if ctx and not agent_id:
        agent_id = ctx.agent_id

    runner = get_runner()
    if runner is None:
        return {"error": "Runner not available — spawn_agent requires a running engine"}

    spawn_ctx = _current_spawn_context.get()
    if spawn_ctx is None:
        return {"error": "No spawn context — spawn_agent can only be called during an agent run"}

    child_agent_id = args.get("agent_id", "")
    message = args.get("message", "")
    if not child_agent_id or not message:
        return {"error": "agent_id and message are required"}

    # Depth check
    child_depth = spawn_ctx.nesting_depth + 1
    if child_depth > spawn_ctx.max_nesting_depth:
        return {
            "error": (
                f"Max nesting depth exceeded: depth {child_depth} > limit {spawn_ctx.max_nesting_depth}. "
                "Handle this task directly instead of spawning."
            )
        }

    # Load child agent config
    child_config = load_agent_config(child_agent_id, runner.config.manifest_dir)
    if child_config is None:
        return {"error": f"Agent config not found: {child_agent_id}"}

    # Apply tools_override if provided
    tools_override = args.get("tools_override")
    if tools_override and isinstance(tools_override, list):
        child_config.tools_allowed = tools_override

    # Apply max_iterations override (never increase beyond parent's sub_agent_max_iterations)
    child_max_iters = child_config.max_iterations
    requested_iters = args.get("max_iterations")
    if requested_iters is not None:
        child_max_iters = min(child_max_iters, int(requested_iters))
    child_max_iters = min(child_max_iters, 30)
    child_config.max_iterations = child_max_iters

    # Apply timeout override
    requested_timeout = args.get("timeout_seconds")
    if requested_timeout is not None:
        child_config.timeout_seconds = min(child_config.timeout_seconds, int(requested_timeout))

    # Force delivery to NONE — sub-agents never message the owner
    child_config.delivery_mode = DeliveryMode.NONE

    # Disable spawning on child unless explicitly configured
    if child_depth >= spawn_ctx.max_nesting_depth:
        child_config.can_spawn_agents = False

    # Build child SpawnContext
    child_spawn_ctx = SpawnContext(
        parent_run_id=spawn_ctx.parent_run_id,
        parent_agent_id=agent_id,
        correlation_id=spawn_ctx.correlation_id,
        nesting_depth=child_depth,
        max_nesting_depth=spawn_ctx.max_nesting_depth,
        max_spawn_batch=spawn_ctx.max_spawn_batch,
        remaining_token_budget=spawn_ctx.remaining_token_budget,
        remaining_cost_budget_usd=spawn_ctx.remaining_cost_budget_usd,
        parent_trace_id=spawn_ctx.parent_trace_id,
        parent_span_id=spawn_ctx.parent_span_id,
    )

    # Namespaced dedup key — includes message hash so the same agent can be
    # spawned multiple times with different messages (wide research pattern)
    msg_hash = hashlib.md5(message.encode()).hexdigest()[:8]
    dedup_key = f"sub:{spawn_ctx.parent_run_id}:{child_agent_id}:{msg_hash}"
    from robothor.engine.dedup import release, try_acquire

    if not await try_acquire(dedup_key):
        return {
            "error": f"Agent {child_agent_id} with this exact message is already running as a sub-agent"
        }

    start_time = time.monotonic()
    try:
        sem = _get_spawn_semaphore()
        async with sem:
            run = await runner.execute(
                agent_id=child_agent_id,
                message=message,
                trigger_type=TriggerType.SUB_AGENT,
                trigger_detail=f"spawned_by:{agent_id}",
                correlation_id=spawn_ctx.correlation_id,
                agent_config=child_config,
                spawn_context=child_spawn_ctx,
                user_id=ctx.user_id if ctx else "",
                user_role=ctx.user_role if ctx else "",
                tenant_id=ctx.tenant_id if ctx else "",
            )
    finally:
        await release(dedup_key)

    elapsed_ms = int((time.monotonic() - start_time) * 1000)

    # Deduct child's usage from parent's remaining budget
    if spawn_ctx.remaining_token_budget > 0:
        spawn_ctx.remaining_token_budget = max(
            0, spawn_ctx.remaining_token_budget - run.input_tokens - run.output_tokens
        )
    if spawn_ctx.remaining_cost_budget_usd > 0:
        spawn_ctx.remaining_cost_budget_usd = max(
            0.0, spawn_ctx.remaining_cost_budget_usd - run.total_cost_usd
        )

    result: dict[str, Any] = {
        "agent_id": child_agent_id,
        "run_id": run.id,
        "status": run.status.value,
        "output_text": run.output_text or "",
        "duration_ms": elapsed_ms,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "total_cost_usd": run.total_cost_usd,
        "steps": len(run.steps),
    }
    if run.error_message:
        result["error"] = run.error_message

    return result


async def _handle_spawn_agents(
    args: dict[str, Any],
    ctx: ToolContext | None = None,
    *,
    agent_id: str = "",
) -> dict[str, Any]:
    """Spawn multiple agents in parallel and wait for all results."""
    if ctx and not agent_id:
        agent_id = ctx.agent_id

    agents_list = args.get("agents", [])
    if not agents_list:
        return {"error": "agents list is required and must not be empty"}

    # Batch limit: per-agent config > engine config > default 10
    spawn_ctx = _current_spawn_context.get()
    max_batch = DEFAULT_MAX_CONCURRENT_SPAWNS
    if _engine_config:
        max_batch = _engine_config.max_spawn_batch
    # Per-agent override (if caller's agent config specifies it)
    if spawn_ctx and spawn_ctx.max_spawn_batch > 0:
        max_batch = spawn_ctx.max_spawn_batch

    if len(agents_list) > max_batch:
        return {"error": f"Max {max_batch} parallel sub-agents allowed, got {len(agents_list)}"}

    coros = []
    for spec in agents_list:
        spawn_args = {
            "agent_id": spec.get("agent_id", ""),
            "message": spec.get("message", ""),
        }
        if "tools_override" in spec:
            spawn_args["tools_override"] = spec["tools_override"]
        coros.append(_handle_spawn_agent(spawn_args, agent_id=agent_id))

    raw_results = await asyncio.gather(*coros, return_exceptions=True)

    results = []
    completed = 0
    failed = 0

    for i, r in enumerate(raw_results):
        if isinstance(r, BaseException):
            failed += 1
            results.append(
                {
                    "agent_id": agents_list[i].get("agent_id", "unknown"),
                    "status": "failed",
                    "error": str(r),
                }
            )
        elif isinstance(r, dict) and r.get("error"):
            failed += 1
            results.append(r)
        else:
            completed += 1
            if isinstance(r, dict):
                results.append(r)
            else:
                results.append({"status": "completed", "result": str(r)})

    return {
        "results": results,
        "total": len(agents_list),
        "completed": completed,
        "failed": failed,
    }


# Register handlers — wrap to extract agent_id from ctx
async def _spawn_agent_handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _handle_spawn_agent(args, ctx, agent_id=ctx.agent_id)


async def _spawn_agents_handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _handle_spawn_agents(args, ctx, agent_id=ctx.agent_id)


HANDLERS["spawn_agent"] = _spawn_agent_handler
HANDLERS["spawn_agents"] = _spawn_agents_handler
