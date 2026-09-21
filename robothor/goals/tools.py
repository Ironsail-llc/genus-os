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
    {
        "create_pursuit_goal",
        "get_pursuit_goal",
        "list_pursuit_goals",
        "report_pursuit_goal",
        "update_pursuit_goal",
    }
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
            "Read goal criteria, current version, evidence, tasks and execution history for further work. "
            "For a final single-goal chat status reply, use report_pursuit_goal instead. "
            "execution_enabled reports whether automatic pursuit is enabled for this tenant; "
            "do not promise automatic progress when false. A waiting goal may wake on its "
            "scheduled review or a matching event/linked-task change; paused goals do not wake. "
            "wake_conditions explicitly lists those alternatives, conditional on execution_enabled. "
            "These are registered triggers, not evidence that a trigger has fired. Events older "
            "than events_registered_at are ignored; enabling execution alone does not make a "
            "future review due. task_events accepts events carrying that task_id. "
            "Waking is not permission to run: parent controls, budgets and recovery still apply. "
            "Omit goal_id only within that goal's execution.",
            {"type": "object", "properties": {"goal_id": {"type": "string"}}},
        ),
        "list_pursuit_goals": (
            "List the current tenant's operator goals, including paused and waiting goals. "
            "Use this to discover goals or compare their progress: objectives, criteria, evidence, versions and task "
            "summaries are included. Once a single goal is selected, use report_pursuit_goal for its final "
            "chat status reply. Read an individual goal only when additional detail is needed "
            "(full task list, history, child goals, runs or registered wake conditions). "
            "Includes exact linked-task counts by status and up to three task titles per status; "
            "truncated previews are not the full task list. Task counts do not prove goal completion. "
            "execution_enabled reports whether automatic pursuit is enabled; goal status "
            "and wake conditions still govern each goal.",
            {"type": "object", "properties": {}},
        ),
        "report_pursuit_goal": (
            "Finish this chat reply with a factual report of one explicitly selected goal. "
            "Prefer this for a single-goal status question or to confirm a requested goal control. "
            "Use only when that report answers the user's entire request; finish other requested "
            "work first. This reads current goal, task and child state; it does not execute work "
            "or pause/resume anything. Call alone, after any requested controls. The host publishes "
            "the final report without another model reply only for a recognized standalone request. "
            "Otherwise report_prepared is false and the returned goal data supplies facts for your complete reply. "
            "Use ordinary reads and conversation "
            "for additional questions, multiple goals or requests this report cannot answer.",
            {
                "type": "object",
                "properties": {"goal_id": {"type": "string"}},
                "required": ["goal_id"],
                "additionalProperties": False,
            },
        ),
        "update_pursuit_goal": (
            "Record progress, criterion evidence, a wait, blocker, assessment or completion. "
            "Use the current version from get_pursuit_goal or list_pursuit_goals. Evidence needs a reference and "
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
    goal = await asyncio.to_thread(store.get, ctx.tenant_id, goal_id)
    wait = goal.get("wait") or {}
    conditions = None
    if goal["status"] == "waiting":
        conditions = {
            "scheduled_review_at": goal["ready_at"],
            "events_registered_at": wait.get("registered_at"),
            "task_events": {"task_id": wait["task_id"]} if wait.get("task_id") else None,
            "linked_task_changes": [str(task["id"]) for task in goal["tasks"]],
            "matching_event": {"type": wait["event_type"], "match": wait.get("event_match", {})}
            if wait.get("event_type")
            else None,
        }
    return {
        "goal": goal,
        "execution_enabled": await asyncio.to_thread(store.enabled, ctx.tenant_id),
        "wake_conditions": conditions,
    }


async def list_goals(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    check_context(ctx)
    return {
        "goals": await asyncio.to_thread(store.list_goals, ctx.tenant_id, task_summary=True),
        "execution_enabled": await asyncio.to_thread(store.enabled, ctx.tenant_id),
    }


async def report_goal(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from uuid import UUID

    from robothor.goals.presentation import render_goal_progress
    from robothor.goals.report_channel import publish_report, require_report_context

    check_context(ctx)
    channel = require_report_context(ctx)
    if set(args) != {"goal_id"} or not isinstance(args["goal_id"], str):
        raise ValueError("An explicit goal_id is required for the final report")
    goal_id = str(UUID(args["goal_id"]))
    goal = await asyncio.to_thread(store.get, ctx.tenant_id, goal_id)
    enabled = await asyncio.to_thread(store.enabled, ctx.tenant_id)
    report = render_goal_progress(goal, execution_enabled=enabled)
    if channel.allowed_goal_statuses and goal["status"] not in channel.allowed_goal_statuses:
        channel.finalizes = False
    publish_report(ctx, report)
    result = {"goal": goal, "execution_enabled": enabled, "report_prepared": channel.finalizes}
    return result


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
    if (
        current
        and goal_id == current.goal_id
        and (result["status"] not in {"queued", "running"} or change.action == "block")
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
        map(guarded, [create_goal, get_goal, list_goals, report_goal, update_goal]),
        strict=True,
    )
)
