"""Lifting a run's unfinished todo items back onto its CRM parent task.

Extracted from run_finalizer.py, which calls it at the end of a run (inline,
or in a worker thread under a running loop). Nothing here touches the runner:
it reads the run's todo list and writes to the CRM, so it takes everything it
needs as parameters.
"""

from __future__ import annotations

import logging
from typing import Any

from robothor.engine.sanitize import sanitize_log as _sanitize

logger = logging.getLogger(__name__)


def _escalate_unfinished_todos(
    todos: Any,
    parent_task_id: str | None,
    agent_id: str,
    tenant_id: str = "",
    agent_config: Any = None,
    run_id: str | None = None,
) -> bool:
    """Lift unfinished todo_write items back to the CRM parent task so the
    next heartbeat's planner picks up where this run left off.

    Closes the Stage-5 "full circle" gap: a run that exhausted iterations
    with items still pending or in_progress must propagate that work to
    the thread pool — silently dropping it is the prior failure mode.

    Behavior:
      - None/empty todo list → no-op, returns False.
      - No parent_task_id → no-op (list was standalone).
      - All items completed → no-op (run finished cleanly).
      - Otherwise: write set_next_action("Continue: <first_unfinished>")
        on the parent. If the parent isn't already tagged `thread`, add
        the tag and seed `objective` from the title if empty. Non-
        destructive — never touches status, owner, or other tags.

    Phase 3 addition: when ``agent_config.todo_list_enabled`` and
    ``agent_config.task_protocol`` are both true AND the
    ``ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED`` env is "1", unfinished
    items are ALSO promoted to real CRM subtasks (idempotent via content
    hash). The ``next_action`` write above stays — promotion is additive.
    """
    if todos is None:
        return False
    items = getattr(todos, "items", None) or []
    if not items:
        return False
    if not parent_task_id:
        return False

    unfinished = [it for it in items if getattr(it, "status", "") in ("pending", "in_progress")]
    if not unfinished:
        return False

    from robothor.constants import DEFAULT_TENANT
    from robothor.crm import dal

    effective_tenant = tenant_id or DEFAULT_TENANT

    try:
        parent = dal.get_task(parent_task_id, tenant_id=effective_tenant)
    except Exception as e:
        logger.warning("todo escalation: get_task failed for %s: %s", _sanitize(parent_task_id), e)
        return False
    if not parent:
        return False

    first = unfinished[0]
    next_action = f"Continue: {getattr(first, 'content', '').strip()}"[:500]

    dal.set_next_action(
        task_id=parent_task_id,
        next_action=next_action,
        agent=agent_id,
        by="runtime",
        tenant_id=effective_tenant,
    )

    existing_tags = parent.get("tags") or []
    if "thread" not in existing_tags:
        new_tags = list(existing_tags) + ["thread"]
        update_kwargs: dict[str, Any] = {
            "task_id": parent_task_id,
            "tags": new_tags,
            "changed_by": agent_id,
            "tenant_id": effective_tenant,
        }
        # Seed objective from title if empty — the planner needs something
        # to plan against on the next beat.
        if not (parent.get("objective") or "").strip():
            update_kwargs["objective"] = parent.get("title") or ""
        dal.update_task(**update_kwargs)
        # Refresh the local parent dict so the promotion step below sees the
        # freshly added `thread` tag.
        parent["tags"] = new_tags

    # Phase 3 — promote unfinished items to real subtasks (best-effort).
    if agent_config is not None:
        try:
            from robothor.engine.todo_promotion import promote_unfinished_items

            promote_unfinished_items(
                parent=parent,
                items=unfinished,
                agent_config=agent_config,
                agent_id=agent_id,
                run_id=run_id or "",
                tenant_id=effective_tenant,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("todo promotion error (non-fatal): %s", _sanitize(e))

    return True
