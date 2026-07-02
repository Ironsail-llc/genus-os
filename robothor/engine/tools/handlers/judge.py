"""Tool handler for the goal-judge (self-improvement Phase 1).

Exposes ``judge_run`` — grade an agent's recent runs against real outcome
signals and write ``agent_reviews`` rows (reviewer_type='judge'). Hosted on the
evening-winddown agent ~5 min before buddy_refresh so the day's achievement
score is judge-backed. Inert unless ``ROBOTHOR_JUDGE_ENABLED=1``.
"""

from __future__ import annotations

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


@_handler("judge_run")
async def _judge_run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Judge recent runs for one agent (or the caller's agent) and persist verdicts."""
    from robothor.engine.judge import DEFAULT_MAX_RUNS, DEFAULT_WINDOW_HOURS, run_judgment_pass

    agent_id = (args.get("agent_id") or "").strip()
    if not agent_id:
        return {"error": "agent_id is required"}

    try:
        window_hours = int(args.get("window_hours", DEFAULT_WINDOW_HOURS))
        max_runs = int(args.get("max_runs", DEFAULT_MAX_RUNS))
    except (TypeError, ValueError):
        return {"error": "window_hours and max_runs must be integers"}

    tenant_id = getattr(ctx, "tenant_id", None) or _default_tenant()
    return await run_judgment_pass(
        agent_id,
        window_hours=window_hours,
        max_runs=max_runs,
        tenant_id=tenant_id,
    )


def _default_tenant() -> str:
    from robothor.constants import DEFAULT_TENANT

    return DEFAULT_TENANT
