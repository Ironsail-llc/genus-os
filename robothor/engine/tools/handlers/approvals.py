"""Workflow approval tools — how a decision reaches the engine from a chat.

The operator answers in Telegram; the agent they are talking to relays the
decision here. Deliberately three narrow tools rather than one with a mode
flag: "approve" and "reject" should be impossible to confuse when a model is
choosing between them under a prompt that contains both words.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}


async def _handle_list_pending_approvals(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """What is waiting on a human right now."""
    import asyncio
    from datetime import UTC, datetime

    from robothor.engine.approvals import list_pending_approvals

    tenant_id = getattr(ctx, "tenant_id", "") or ""
    pending = await asyncio.to_thread(list_pending_approvals, tenant_id=tenant_id)

    now = datetime.now(UTC)
    return {
        "count": len(pending),
        "pending": [
            {
                "run_id": r.run_id,
                "workflow_id": r.workflow_id,
                "step_id": r.step_id,
                "prompt": r.prompt,
                "detail": r.detail,
                "hours_remaining": round((r.expires_at - now).total_seconds() / 3600, 1),
            }
            for r in pending
        ],
    }


async def _decide(args: dict[str, Any], ctx: ToolContext, decision_name: str) -> dict[str, Any]:
    import asyncio

    from robothor.engine.approvals import (
        ApprovalDecision,
        decide_approval,
        get_approval,
        list_pending_approvals,
    )

    run_id = (args.get("run_id") or "").strip()
    if not run_id:
        return {"error": "run_id is required"}
    tenant_id = getattr(ctx, "tenant_id", "") or ""
    step_id = (args.get("step_id") or "").strip()

    if not step_id:
        waiting = [
            r
            for r in await asyncio.to_thread(list_pending_approvals, tenant_id=tenant_id)
            if r.run_id == run_id
        ]
        if not waiting:
            return {"error": f"Run {run_id} has nothing awaiting approval"}
        if len(waiting) > 1:
            # Guessing which gate the operator meant is exactly the kind of
            # helpfulness that approves the wrong thing.
            return {
                "error": f"{len(waiting)} steps are waiting on this run — pass step_id",
                "waiting_steps": [{"step_id": r.step_id, "prompt": r.prompt} for r in waiting],
            }
        step_id = waiting[0].step_id

    decision = (
        ApprovalDecision.APPROVED if decision_name == "approve" else ApprovalDecision.REJECTED
    )
    decided_by = f"agent:{getattr(ctx, 'agent_id', '') or 'unknown'}"
    settled = await asyncio.to_thread(
        decide_approval,
        run_id,
        step_id,
        decision,
        decided_by=decided_by,
        note=args.get("note", "") or "",
        tenant_id=tenant_id,
    )

    if not settled:
        existing = await asyncio.to_thread(get_approval, run_id, step_id, tenant_id=tenant_id)
        return {
            "settled": False,
            "status": existing.status if existing else "unknown",
            "message": "Already decided — nothing changed.",
        }

    # The run resumes on the next watchdog tick (≤1 min). Not resuming inline
    # keeps a long workflow from running inside a tool call and blowing the
    # agent's own budget.
    return {
        "settled": True,
        "run_id": run_id,
        "step_id": step_id,
        "decision": decision.value,
        "message": f"{decision.value.capitalize()} — the workflow resumes within a minute.",
    }


async def _handle_approve_workflow_step(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Approve a waiting workflow step."""
    return await _decide(args, ctx, "approve")


async def _handle_reject_workflow_step(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Reject a waiting workflow step."""
    return await _decide(args, ctx, "reject")


HANDLERS["list_pending_approvals"] = _handle_list_pending_approvals
HANDLERS["approve_workflow_step"] = _handle_approve_workflow_step
HANDLERS["reject_workflow_step"] = _handle_reject_workflow_step
