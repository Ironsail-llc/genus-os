"""
Planning phase — optional pre-execution LLM call to produce a structured plan.

When enabled, runs before the main tool loop using a cheap model. The plan is
injected as context so the agent has a roadmap. Non-fatal: if planning fails,
execution proceeds normally.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import litellm

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

DEFAULT_PLAN: dict[str, Any] = {
    "difficulty": "moderate",
    "estimated_steps": 5,
    "plan": [],
    "risks": [],
    "success_criteria": "",
}


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
