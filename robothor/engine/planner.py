"""
Planning phase — optional pre-execution LLM call to produce a structured plan.

When enabled, runs before the main tool loop using a cheap model. The plan is
injected as context so the agent has a roadmap. Non-fatal: if planning fails,
execution proceeds normally.

Dynamic replanning: when execution goes sideways (repeated failures, budget/progress
mismatch), the planner can produce a revised plan mid-run.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import litellm

if TYPE_CHECKING:
    from robothor.engine.escalation import EscalationManager
    from robothor.engine.scratchpad import Scratchpad

logger = logging.getLogger(__name__)

PLANNING_PROMPT = """Analyze this task and produce a JSON execution plan.

Task: {message}

Available tools: {tools}

Respond with ONLY valid JSON in this exact format:
{{
  "difficulty": "simple" or "moderate" or "complex",
  "estimated_steps": <integer>,
  "plan": [
    {{"step": 1, "action": "description", "tool": "tool_name_or_none"}}
  ],
  "risks": ["potential issue 1"],
  "success_criteria": "how to verify the task succeeded"
}}"""

REPLAN_PROMPT = """You need to revise your execution plan. The original plan hit problems.

Original plan:
{original_plan}

What succeeded:
{successes}

What failed:
{failures}

Current state: {progress_summary}

Produce a REVISED plan that works around the failures. Respond with ONLY valid JSON in this exact format:
{{
  "difficulty": "simple" or "moderate" or "complex",
  "estimated_steps": <integer>,
  "plan": [
    {{"step": 1, "action": "description", "tool": "tool_name_or_none"}}
  ],
  "risks": ["potential issue 1"],
  "success_criteria": "how to verify the task succeeded"
}}"""

DEFAULT_PLAN: dict[str, Any] = {
    "difficulty": "moderate",
    "estimated_steps": 5,
    "plan": [],
    "risks": [],
    "success_criteria": "",
}

MAX_REPLANS = 2


@dataclass
class PlanResult:
    """Result of the planning phase."""

    success: bool = False
    difficulty: str = "moderate"
    estimated_steps: int = 5
    plan: list[dict[str, Any]] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    success_criteria: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


async def generate_plan(
    message: str,
    tools: list[str],
    model: str,
    fallback_models: list[str] | None = None,
) -> PlanResult:
    """Generate an execution plan via a separate LLM call.

    Uses JSON mode, max 500 tokens, cheap model. Non-fatal — returns
    a default PlanResult on any failure.
    """
    prompt = PLANNING_PROMPT.format(
        message=message,
        tools=", ".join(tools[:30]),  # cap tool list for context
    )

    models = [model] + (fallback_models or [])
    models = [m for m in models if m]

    for m in models:
        try:
            response = await litellm.acompletion(
                model=m,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=500,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            if not content:
                continue

            data = json.loads(content)
            return PlanResult(
                success=True,
                difficulty=data.get("difficulty", "moderate"),
                estimated_steps=int(data.get("estimated_steps", 5)),
                plan=data.get("plan", []),
                risks=data.get("risks", []),
                success_criteria=data.get("success_criteria", ""),
                raw=data,
            )
        except Exception as e:
            logger.debug("Planning failed with model %s: %s", m, e)
            continue

    return PlanResult(success=False, error="All planning models failed")


def format_plan_context(plan: PlanResult) -> str:
    """Format a plan as context to inject before execution."""
    if not plan.success or not plan.plan:
        return ""

    lines = ["[EXECUTION PLAN]", f"Difficulty: {plan.difficulty}"]
    for step in plan.plan:
        tool = step.get("tool", "")
        tool_str = f" (tool: {tool})" if tool else ""
        lines.append(f"  {step.get('step', '?')}. {step.get('action', '?')}{tool_str}")
    if plan.risks:
        lines.append(f"Risks: {', '.join(plan.risks)}")
    if plan.success_criteria:
        lines.append(f"Success: {plan.success_criteria}")
    return "\n".join(lines)


# ─── Dynamic Replanning ──────────────────────────────────────────────


def should_replan(
    scratchpad: Scratchpad,
    plan: PlanResult,
    escalation: EscalationManager,
    replan_count: int,
    budget_pct_used: float = 0.0,
) -> bool:
    """Determine if mid-run replanning should trigger.

    Triggers if:
    - 3+ consecutive failures on the same plan step
    - >60% budget consumed with <30% plan progress
    - Escalation at CHANGE_STRATEGY threshold

    Blocked if replan_count >= MAX_REPLANS.
    """
    if replan_count >= MAX_REPLANS:
        return False

    if not plan.success or not plan.plan:
        return False

    # 3+ failed attempts on the current plan step
    if scratchpad.current_step_attempts >= 3:
        return True

    # Budget/progress mismatch
    if budget_pct_used > 0.6 and scratchpad.total_plan_steps > 0:
        progress_pct = scratchpad.steps_completed / scratchpad.total_plan_steps
        if progress_pct < 0.3:
            return True

    # Escalation hit change-strategy threshold
    return bool(escalation.at_change_strategy_threshold)


async def replan(
    original_plan: PlanResult,
    scratchpad: Scratchpad,
    model: str,
    fallback_models: list[str] | None = None,
) -> PlanResult:
    """Generate a revised plan based on execution progress and failures.

    Uses the same cheap model as initial planning. Non-fatal — returns
    the original plan if replanning fails.
    """
    # Build context from scratchpad
    successes = []
    failures = []
    for i, step in enumerate(original_plan.plan):
        if i in scratchpad._completed_steps:
            successes.append(f"Step {i + 1}: {step.get('action', '?')} — completed")
        elif scratchpad._step_attempts.get(i, 0) > 0:
            attempts = scratchpad._step_attempts[i]
            failures.append(f"Step {i + 1}: {step.get('action', '?')} — failed {attempts} time(s)")

    original_text = format_plan_context(original_plan)

    prompt = REPLAN_PROMPT.format(
        original_plan=original_text,
        successes="\n".join(successes) if successes else "None yet",
        failures="\n".join(failures) if failures else "None",
        progress_summary=(
            f"{scratchpad.steps_completed}/{scratchpad.total_plan_steps} steps done, "
            f"{scratchpad._errors} errors total"
        ),
    )

    models = [model] + (fallback_models or [])
    models = [m for m in models if m]

    for m in models:
        try:
            response = await litellm.acompletion(
                model=m,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=500,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            if not content:
                continue

            data = json.loads(content)
            return PlanResult(
                success=True,
                difficulty=data.get("difficulty", original_plan.difficulty),
                estimated_steps=int(data.get("estimated_steps", 5)),
                plan=data.get("plan", []),
                risks=data.get("risks", []),
                success_criteria=data.get("success_criteria", ""),
                raw=data,
            )
        except Exception as e:
            logger.debug("Replanning failed with model %s: %s", m, e)
            continue

    logger.warning("Replanning failed — continuing with original plan")
    return original_plan
