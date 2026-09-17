"""Mid-run replanning: the plan was wrong, so write a new one.

Extracted from ``_run_loop`` by the rule ``run_finalizer.py``'s header states —
a cohesive cluster goes into its own module rather than onto a god-object — and
because the function ratchet asks for an extraction rather than a bigger
number when the loop learns something new.

It is a cohesive cluster by the usual test: everything here is about ONE
question, is the plan still the plan, and the loop's only involvement is
holding the two values that answer changes (``plan_result`` and the attempt
count). Nothing here reads the conversation except to append a revised plan to
it, and nothing else in the loop reads what it writes.

Nothing here may end a run. A replan that fails leaves the previous plan in
place and the loop carries on: an agent working from a stale plan is worse
than one working from a fresh one and better than one that stopped.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Any

from robothor.engine.sanitize import sanitize_log as _sanitize
from robothor.engine.session import ENGINE_CONTEXT_ROLE

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplanOutcome:
    """What the loop has to carry forward: the plan, and how many attempts."""

    plan_result: Any
    replan_count: int


async def maybe_replan(
    session: Any,
    agent_config: Any,
    *,
    plan_result: Any,
    scratchpad: Any,
    escalation: Any,
    models: list[str],
    replan_count: int,
    readonly_mode: bool,
    hook_registry: Any,
) -> ReplanOutcome:
    """Replace the run's plan if the evidence says it has stopped working.

    Returns the plan and count to carry forward, unchanged when no replan was
    due or the replan did not succeed.
    """
    unchanged = ReplanOutcome(plan_result=plan_result, replan_count=replan_count)
    if (
        not (plan_result and scratchpad and escalation and agent_config.planning_enabled)
        or readonly_mode
    ):
        return unchanged

    from robothor.engine.planner import should_replan

    budget_pct = 0.0
    if session.run.token_budget > 0:
        used = session.run.input_tokens + session.run.output_tokens
        budget_pct = used / session.run.token_budget
    if not should_replan(scratchpad, plan_result, escalation, replan_count, budget_pct):
        return unchanged

    from robothor.engine.planner import format_plan_context, replan

    new_plan = await replan(
        plan_result,
        scratchpad,
        models[0],
        fallback_models=models[1:],  # [1:2] missed the offline tier
    )
    if not (new_plan.success and new_plan.plan):
        return unchanged

    attempt = replan_count + 1
    scratchpad.set_plan(new_plan.plan)
    await _dispatch_replan_hook(hook_registry, agent_config, session, attempt)
    context = _format_context(format_plan_context, new_plan)
    if context:
        session.messages.append(
            {
                "role": ENGINE_CONTEXT_ROLE,
                "content": f"[REVISED PLAN — attempt {attempt}]\n{context}",
            }
        )
    return ReplanOutcome(plan_result=new_plan, replan_count=attempt)


def _format_context(formatter: Any, new_plan: Any) -> str:
    """Non-fatal: replan formatting must not abort the run."""
    try:
        return str(formatter(new_plan) or "")
    except Exception as e:  # noqa: BLE001 - the plan is live either way
        logger.warning(
            "Replan context formatting failed (non-fatal, continuing without "
            "revised plan context): %s",
            _sanitize(e),
        )
        return ""


async def _dispatch_replan_hook(
    hook_registry: Any, agent_config: Any, session: Any, attempt: int
) -> None:
    if not hook_registry:
        return
    from robothor.engine.hook_registry import HookContext, HookEvent

    with contextlib.suppress(Exception):
        await hook_registry.dispatch(
            HookEvent.REPLAN,
            HookContext(
                event=HookEvent.REPLAN,
                agent_id=agent_config.id,
                run_id=session.run_id,
                metadata={"replan_count": attempt},
            ),
        )
