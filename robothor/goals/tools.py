"""Tool schemas and handlers for operator goal pursuit."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext


from robothor.goals import store
from robothor.goals.model import CreateGoal, GoalUpdate
from robothor.goals.runtime import binding

Handler = Callable[[dict[str, Any], "ToolContext"], Awaitable[dict[str, Any]]]

TOOL_NAMES = frozenset(
    {"create_pursuit_goal", "get_pursuit_goal", "list_pursuit_goals", "update_pursuit_goal"}
)


def schemas() -> dict[str, Any]:
    specs = {
        "create_pursuit_goal": (
            "Create a persistent goal ONLY when the user explicitly requests a goal, or to execute "
            "an actionable child of an authorized long-term goal. Main agent owns pursuit. "
            "Short goals loop until achieved or blocked; long goals coordinate tasks and waits.",
            CreateGoal.model_json_schema(),
        ),
        "get_pursuit_goal": (
            "Read goal criteria, current version, evidence, tasks and execution history. "
            "Omit goal_id only within that goal's execution.",
            {"type": "object", "properties": {"goal_id": {"type": "string"}}},
        ),
        "list_pursuit_goals": (
            "List the current tenant's operator goals, including paused and waiting goals.",
            {"type": "object", "properties": {}},
        ),
        "update_pursuit_goal": (
            "Record progress, criterion evidence, a wait, blocker, assessment or completion. "
            "Use the current version from get_pursuit_goal. Evidence needs a reference and "
            "a truthful satisfied assessment. Ongoing goals use assess. Pause/cancel only "
            "when asked; resume/steer/approve require an operator. Never weaken criteria.",
            GoalUpdate.model_json_schema(),
        ),
    }
    specs["update_pursuit_goal"][1]["properties"]["goal_id"] = {"type": "string"}
    return {
        name: {
            "type": "function",
            "function": {"name": name, "description": desc, "parameters": params},
        }
        for name, (desc, params) in specs.items()
    }


def check_context(ctx: ToolContext) -> None:
    if ctx.is_benchmark:
        raise ValueError("goal pursuit is unavailable in benchmarks")
    if ctx.agent_id != "main":
        raise ValueError("the main agent coordinates operator goals")
    if ctx.identity is not None and ctx.user_role not in {"owner", "admin"}:
        raise ValueError("operator role required for goals")
    current = binding.get()
    if current and current.run_id != ctx.run_id:
        raise ValueError("a delegated run cannot control its parent's goal")


async def create_goal(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    check_context(ctx)
    return {
        "goal": await asyncio.to_thread(
            store.create, ctx.tenant_id, CreateGoal(**args), ctx.user_id or ctx.agent_id
        ),
        "execution_enabled": await asyncio.to_thread(store.enabled, ctx.tenant_id),
    }


async def get_goal(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    check_context(ctx)
    current = binding.get()
    goal_id = args.get("goal_id") or (current.goal_id if current else None)
    if not goal_id:
        raise ValueError("goal_id is required outside goal execution")
    return {"goal": await asyncio.to_thread(store.get, ctx.tenant_id, goal_id)}


async def list_goals(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    check_context(ctx)
    return {"goals": await asyncio.to_thread(store.list_goals, ctx.tenant_id)}


async def update_goal(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    check_context(ctx)
    args = dict(args)
    current = binding.get()
    goal_id = args.pop("goal_id", None) or (current.goal_id if current else None)
    if not goal_id:
        raise ValueError("goal_id is required outside goal execution")
    change = GoalUpdate(**args)
    current = binding.get()
    if (
        current
        and goal_id == current.goal_id
        and not await asyncio.to_thread(store.heartbeat, ctx.tenant_id, goal_id, current.attempt)
    ):
        raise ValueError("goal execution no longer holds its lease")
    result = await asyncio.to_thread(
        store.update,
        ctx.tenant_id,
        goal_id,
        change,
        ctx.user_id or ctx.agent_id,
        operator=ctx.user_role in {"owner", "admin"},
    )
    if current and goal_id == current.goal_id and (
        result["status"] not in {"queued", "running"} or change.action == "block"
    ):
        current.yield_requested = True
    return {"goal": result}


def guarded(handler: Handler) -> Handler:
    async def call(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        try:
            return await handler(args, ctx)
        except ValueError as exc:
            return {"error": str(exc)}

    return call


HANDLERS = dict(
    zip(
        sorted(TOOL_NAMES),
        map(guarded, [create_goal, get_goal, list_goals, update_goal]),
        strict=True,
    )
)
