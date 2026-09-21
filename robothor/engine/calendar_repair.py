"""Repair work is an explicit task; it never retries the user's calendar write."""

from __future__ import annotations

from typing import Any


def attach_repair_task(result: dict[str, Any], ctx: Any) -> None:
    from robothor.crm.dal import create_task

    try:
        repair = create_task(
            title="Reconcile blocked calendar operation",
            body=f"Operation {result['operation_id']}; run {ctx.run_id}. " + str(result["error"]),
            created_by_agent=ctx.agent_id,
            requires_human=True,
            tags=["calendar", "integration-repair"],
            tenant_id=ctx.tenant_id,
        )
        if isinstance(repair, str):
            result["repair_task_id"] = repair
        else:
            result["repair_task_error"] = "Repair task was not created; operation record retained"
    except Exception:
        result["repair_task_error"] = "Repair task could not be filed; operation record retained"
