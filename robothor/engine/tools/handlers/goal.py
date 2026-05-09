"""Session-goal tool handlers.

Backed by ``robothor.engine.session_goal``; goals are stored as crm_task rows
with the ``session_goal`` tag. Owner-only scoping is enforced at injection
time, but every agent can call these tools to read or evolve its own goal
(scoped via ``agent_id`` in the tool args / context).
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


def _target_agent(args: dict[str, Any], ctx: ToolContext) -> str:
    return str(args.get("agent_id") or ctx.agent_id or "").strip()


@_handler("create_goal")
async def _create_goal(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create a long-running session goal if one is not already active."""
    from robothor.engine.session_goal import create_active_goal, summarize_goal

    objective = str(args.get("objective") or "").strip()
    if not objective:
        return {"error": "objective is required"}

    criteria = args.get("success_criteria") or args.get("criteria") or None
    if criteria is not None and not isinstance(criteria, list):
        return {"error": "success_criteria must be an array when provided"}

    try:
        goal = create_active_goal(
            tenant_id=ctx.tenant_id,
            objective=objective,
            criteria=[str(c) for c in criteria] if criteria else None,
            agent_id=_target_agent(args, ctx),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"goal": summarize_goal(goal, workspace=ctx.workspace or None)}


@_handler("get_goal")
async def _get_goal(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Return the active session goal for the current or requested agent."""
    from robothor.engine.session_goal import get_active_goal, summarize_goal

    goal = get_active_goal(
        tenant_id=ctx.tenant_id,
        agent_id=_target_agent(args, ctx),
    )
    if goal is None:
        return {"goal": None, "status": "none"}
    return {"goal": summarize_goal(goal, workspace=ctx.workspace or None)}


@_handler("update_goal")
async def _update_goal(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Record evidence (with typed kind) or mark a goal complete.

    Evidence kinds and required references:
      - test_run: ``pytest:passed:N`` / ``pytest:failed:N`` or a UUID
      - commit:   git SHA (≥7 hex chars) — validated via ``git cat-file -e``
      - ci_run:   ``https://`` URL
      - note:     free-form (does not satisfy completion guard)
    """
    from robothor.engine.session_goal import add_evidence, complete_goal, summarize_goal

    status = str(args.get("status") or "").strip().lower()
    agent_id = _target_agent(args, ctx)

    try:
        if status == "complete":
            note = str(args.get("completion_note") or args.get("note") or "").strip()
            if not note:
                return {"error": "completion_note is required when status is complete"}
            goal = complete_goal(
                tenant_id=ctx.tenant_id,
                note=note,
                agent_id=agent_id,
                workspace=ctx.workspace or None,
            )
            return {"goal": summarize_goal(goal, workspace=ctx.workspace or None)}

        kind = str(args.get("kind") or args.get("evidence_kind") or "").strip()
        summary = str(args.get("summary") or args.get("evidence_summary") or "").strip()
        reference = str(args.get("reference") or "").strip()
        if not kind or not summary:
            return {
                "error": ("provide kind and summary, or set status='complete' with completion_note")
            }
        if kind not in ("test_run", "commit", "ci_run", "note"):
            return {"error": ("kind must be one of: test_run, commit, ci_run, note")}
        goal = add_evidence(
            tenant_id=ctx.tenant_id,
            kind=kind,
            summary=summary,
            reference=reference,
            agent_id=agent_id,
            workspace=ctx.workspace or None,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"goal": summarize_goal(goal, workspace=ctx.workspace or None)}
