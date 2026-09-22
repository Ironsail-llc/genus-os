"""Known unfinished worker items prevent automatic task closure."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robothor.engine.models import AgentRun
    from robothor.engine.session import AgentSession


def capture_pending_items(run: AgentRun, session: AgentSession | None) -> None:
    if session is None:
        return
    todos = getattr(session, "todo_list", None)
    run.pending_task_items = [
        item.content.strip()
        for item in (getattr(todos, "items", None) or [])
        if item.status in {"pending", "in_progress"}
    ]


def keep_pending_task_open(run: AgentRun) -> bool:
    """Persist the next step; a completed turn is not completed task work."""
    if not run.pending_task_items or not run.task_id:
        return False
    from robothor.constants import DEFAULT_TENANT
    from robothor.crm import dal

    dal.set_next_action(
        task_id=run.task_id,
        next_action=("Continue: " + run.pending_task_items[0])[:500],
        agent=run.agent_id,
        by="runtime",
        tenant_id=run.tenant_id or DEFAULT_TENANT,
    )
    return True
