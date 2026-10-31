"""Pure helpers over a :class:`PlanState`, shared by webchat and Telegram plan mode.

Moved out of ``chat.py`` (decomposition ratchet) when the webchat attachment
intake added its call site there. ``chat`` re-exports all three, so
``from robothor.engine.chat import _plan_is_expired`` keeps working.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from robothor.engine.models import PLAN_TTL_SECONDS, PlanState

__all__ = ["_extract_plan_text", "_plan_is_expired", "_plan_to_dict"]


def _plan_is_expired(plan: PlanState) -> bool:
    """Check if a pending plan has exceeded its TTL."""
    if not plan.created_at:
        return True
    try:
        created = datetime.fromisoformat(plan.created_at)
        elapsed = (datetime.now(UTC) - created).total_seconds()
        return elapsed > PLAN_TTL_SECONDS
    except (ValueError, TypeError):
        return True


def _extract_plan_text(output: str) -> str:
    """Extract plan text from agent output, stripping the [PLAN_READY] marker."""
    if not output or output.startswith("[PLAN_FAILED]"):
        return ""
    marker = "[PLAN_READY]"
    idx = output.find(marker)
    if idx != -1:
        return output[:idx].strip()
    return output.strip()


def _plan_to_dict(plan: PlanState) -> dict[str, Any]:
    """Serialize PlanState for JSON responses."""
    return {
        "plan_id": plan.plan_id,
        "plan_text": plan.plan_text,
        "original_message": plan.original_message,
        "status": plan.status,
        "created_at": plan.created_at,
        "exploration_run_id": plan.exploration_run_id,
        "rejection_feedback": plan.rejection_feedback,
        "revision_count": plan.revision_count,
        "revision_history": plan.revision_history,
        "execution_run_id": plan.execution_run_id,
        "approval_request_id": plan.approval_request_id,
        "deep_plan": plan.deep_plan,
        "plan_hash": plan.plan_hash,
        "task_context": plan.task_context,
    }
