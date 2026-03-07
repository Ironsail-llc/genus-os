"""Observability tool handlers — agent runs, schedules, stats."""

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


@_handler("list_agent_runs")
async def _list_agent_runs(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.tracking import list_runs

    runs = await asyncio.to_thread(
        list_runs,
        agent_id=args.get("agent_id"),
        status=args.get("status"),
        limit=args.get("limit", 20),
        tenant_id=ctx.tenant_id,
    )
    return {
        "runs": [
            {
                "id": r["id"],
                "agent_id": r["agent_id"],
                "status": r["status"],
                "trigger_type": r.get("trigger_type"),
                "model_used": r.get("model_used"),
                "duration_ms": r.get("duration_ms"),
                "input_tokens": r.get("input_tokens"),
                "output_tokens": r.get("output_tokens"),
                "total_cost_usd": float(r["total_cost_usd"]) if r.get("total_cost_usd") else None,
                "started_at": str(r["started_at"]) if r.get("started_at") else None,
                "completed_at": str(r["completed_at"]) if r.get("completed_at") else None,
                "error_message": r.get("error_message"),
            }
            for r in runs
        ],
        "count": len(runs),
    }


@_handler("get_agent_run")
async def _get_agent_run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.tracking import get_run, list_steps

    run = await asyncio.to_thread(get_run, args["run_id"])
    if not run:
        return {"error": "Run not found"}
    steps = await asyncio.to_thread(list_steps, args["run_id"])
    return {
        "run": {
            "id": run["id"],
            "agent_id": run["agent_id"],
            "status": run["status"],
            "trigger_type": run.get("trigger_type"),
            "trigger_detail": run.get("trigger_detail"),
            "model_used": run.get("model_used"),
            "models_attempted": run.get("models_attempted"),
            "duration_ms": run.get("duration_ms"),
            "input_tokens": run.get("input_tokens"),
            "output_tokens": run.get("output_tokens"),
            "total_cost_usd": float(run["total_cost_usd"]) if run.get("total_cost_usd") else None,
            "started_at": str(run["started_at"]) if run.get("started_at") else None,
            "completed_at": str(run["completed_at"]) if run.get("completed_at") else None,
            "error_message": run.get("error_message"),
            "delivery_status": run.get("delivery_status"),
        },
        "steps": [
            {
                "step_number": s["step_number"],
                "step_type": s["step_type"],
                "tool_name": s.get("tool_name"),
                "duration_ms": s.get("duration_ms"),
                "error_message": s.get("error_message"),
            }
            for s in steps
        ],
        "step_count": len(steps),
    }


@_handler("list_agent_schedules")
async def _list_agent_schedules(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.tracking import list_schedules

    schedules = await asyncio.to_thread(
        list_schedules,
        enabled_only=args.get("enabled_only", True),
        tenant_id=ctx.tenant_id,
    )
    return {
        "schedules": [
            {
                "agent_id": s["agent_id"],
                "enabled": s["enabled"],
                "cron_expr": s.get("cron_expr"),
                "timezone": s.get("timezone"),
                "timeout_seconds": s.get("timeout_seconds"),
                "model_primary": s.get("model_primary"),
                "last_run_at": str(s["last_run_at"]) if s.get("last_run_at") else None,
                "last_status": s.get("last_status"),
                "last_duration_ms": s.get("last_duration_ms"),
                "next_run_at": str(s["next_run_at"]) if s.get("next_run_at") else None,
                "consecutive_errors": s.get("consecutive_errors", 0),
            }
            for s in schedules
        ],
        "count": len(schedules),
    }


@_handler("get_agent_stats")
async def _get_agent_stats(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.tracking import get_agent_stats as _get_agent_stats

    stats = await asyncio.to_thread(
        _get_agent_stats,
        agent_id=args["agent_id"],
        hours=args.get("hours", 24),
        tenant_id=ctx.tenant_id,
    )
    return {
        "agent_id": args["agent_id"],
        "hours": args.get("hours", 24),
        "total_runs": stats.get("total_runs", 0),
        "completed": stats.get("completed", 0),
        "failed": stats.get("failed", 0),
        "timeouts": stats.get("timeouts", 0),
        "avg_duration_ms": round(float(stats["avg_duration_ms"]))
        if stats.get("avg_duration_ms")
        else None,
        "total_input_tokens": stats.get("total_input_tokens"),
        "total_output_tokens": stats.get("total_output_tokens"),
        "total_cost_usd": float(stats["total_cost_usd"]) if stats.get("total_cost_usd") else None,
    }
