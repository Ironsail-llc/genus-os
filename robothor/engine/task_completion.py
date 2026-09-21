"""Known unfinished worker items prevent automatic task closure."""


def capture_pending_items(run, session):
    if session is None:
        return
    todos = getattr(session, "todo_list", None)
    run.pending_task_items = [
        item.content.strip()
        for item in (getattr(todos, "items", None) or [])
        if item.status in {"pending", "in_progress"}
    ]


def keep_pending_task_open(run):
    """Persist the next step; a completed turn is not completed task work."""
    if not run.pending_task_items:
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
