"""Phase 3 — bridge from in-session TodoList items to CRM subtasks.

When a worker exits with unfinished `todo_write` items AND its agent
manifest opts in, this module turns each item into a real CRM subtask
under the parent thread. The Helm task board renders the queue, the
thread planner has discrete units to re-plan, and the operator sees
stalled work instead of it being buried in a free-text `next_action`.

Promotion is additive — the existing `next_action` hint stays. Three
guards keep this safe:

1. Env kill switch: ``ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED`` defaults
   to ``"0"``. Set to ``"1"`` per-instance after one production cycle.
2. Manifest opt-in: agent must have both ``todo_list_enabled`` AND
   ``task_protocol`` set in its YAML.
3. Tag-based cycle guard: a parent already carrying the
   ``promoted_todo`` tag cannot itself produce promotions. Combined with
   ``MAX_PROMOTIONS_PER_RUN``, recursion is bounded at one level.

Idempotency uses a sha256(parent_id + content) prefix as a body marker,
so repeated runs against the same parent + item content return the
existing subtask id instead of duplicating.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

from robothor.constants import DEFAULT_TENANT
from robothor.crm import dal

logger = logging.getLogger(__name__)

# Cap per run — bounds the blast radius if an agent writes a 20-item list.
MAX_PROMOTIONS_PER_RUN = 5

# Body line that carries the idempotency key. The 16-char prefix is
# unambiguous in practice and keeps task bodies readable.
_HASH_BODY_PREFIX = "todo_hash:"

# Tag that marks "this subtask was promoted from an unfinished todo
# item." Doubles as the cycle-guard signal: parents carrying this tag
# do not produce further promotions.
PROMOTED_TAG = "promoted_todo"


def compute_item_hash(parent_task_id: str, content: str) -> str:
    """sha256(parent_id + ":" + normalized content)[:16].

    Normalization lowercases and strips whitespace so trivial wording
    drift doesn't create duplicate subtasks. Different parents always
    produce different hashes because parent_id is part of the input.
    """
    normalized = content.strip().lower()
    key = f"{parent_task_id}:{normalized}".encode()
    return hashlib.sha256(key).hexdigest()[:16]


def should_promote(parent: dict[str, Any]) -> bool:
    """Cycle guard. False if the parent itself was the product of a promotion."""
    tags = parent.get("tags") or []
    return PROMOTED_TAG not in tags


def _find_existing(parent_task_id: str, content_hash: str, tenant_id: str) -> str | None:
    """Return an existing subtask id if one already carries this hash, else None."""
    try:
        existing = dal.find_task_by_dedup_key(
            key_name=_HASH_BODY_PREFIX.rstrip(":"),
            key_value=content_hash,
            include_recently_resolved=True,
            tenant_id=tenant_id,
        )
    except Exception as e:
        logger.debug("find_task_by_dedup_key failed: %s", e)
        return None
    if not existing:
        return None
    return str(existing.get("id")) if existing.get("id") else None


def promote_todo_to_subtask(
    parent: dict[str, Any],
    item: Any,
    agent_id: str,
    run_id: str,
    tenant_id: str = DEFAULT_TENANT,
) -> str | None:
    """Create a subtask from an unfinished todo item. Returns the subtask id,
    or the existing one for idempotent re-runs, or None on skip / error.

    Skips when:
      - `item.status` is `completed` (nothing to promote).
      - An existing subtask carrying the same hash already exists (idempotency).
      - `dal.create_task` returns a validation-error dict.
    """
    status = getattr(item, "status", "") or ""
    content = (getattr(item, "content", "") or "").strip()
    if not content:
        return None
    if status == "completed":
        return None

    parent_id = str(parent.get("id") or "")
    if not parent_id:
        logger.debug("promote_todo_to_subtask: parent has no id")
        return None

    content_hash = compute_item_hash(parent_id, content)

    existing_id = _find_existing(parent_id, content_hash, tenant_id)
    if existing_id:
        logger.debug(
            "todo_promotion: idempotent hit hash=%s parent=%s existing=%s",
            content_hash,
            parent_id,
            existing_id,
        )
        return existing_id

    title = content[:120]
    body = (
        f"Promoted from todo_write in run {run_id}\n"
        f"\n"
        f"{_HASH_BODY_PREFIX} {content_hash}\n"
        f"\n"
        f"{content}"
    )
    priority = parent.get("priority") or "normal"
    assigned = parent.get("assigned_to_agent") or agent_id
    tags = [PROMOTED_TAG]

    try:
        result = dal.create_task(
            title=title,
            body=body,
            status="TODO",
            assigned_to_agent=assigned,
            created_by_agent=agent_id,
            priority=priority,
            tags=tags,
            parent_task_id=parent_id,
            tenant_id=tenant_id,
        )
    except Exception as e:
        logger.warning("todo_promotion.create_task failed: %s", e)
        return None

    # create_task returns the id string on success; a {"error": ...} dict on
    # validation failure (Phase-1 contract).
    if isinstance(result, dict):
        logger.warning(
            "todo_promotion: create_task validation rejected promotion: %s",
            result.get("error"),
        )
        return None
    if not result:
        return None

    subtask_id = str(result)

    # Audit row carries metadata.kind=todo_promoted so observability +
    # the Phase-1 CHECK constraint accept it. The DAL helper opens its own
    # transaction and never raises — failure here can't undo the subtask
    # insert.
    dal.append_task_history(
        task_id=subtask_id,
        from_status=None,
        to_status="TODO",
        changed_by=agent_id,
        reason="Promoted from todo_write",
        metadata={
            "kind": "todo_promoted",
            "content_hash": content_hash,
            "from_run_id": run_id,
            "item_count": 1,
        },
        tenant_id=tenant_id,
    )

    logger.info(
        "todo_promotion.created parent=%s subtask=%s hash=%s",
        parent_id,
        subtask_id,
        content_hash,
        extra={
            "event": "todo_promotion.created",
            "parent_task_id": parent_id,
            "subtask_id": subtask_id,
            "content_hash": content_hash,
            "agent_id": agent_id,
            "run_id": run_id,
        },
    )
    return subtask_id


def promote_unfinished_items(
    parent: dict[str, Any],
    items: list[Any],
    agent_config: Any,
    agent_id: str,
    run_id: str,
    tenant_id: str = DEFAULT_TENANT,
) -> list[str]:
    """Runner-facing entry point. Returns the list of subtask ids created.

    Three short-circuits in order:
      1. Env kill switch ``ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED != "1"`` → no-op.
      2. Manifest opt-out: needs both ``todo_list_enabled`` AND ``task_protocol``.
      3. Cycle guard: parent already carries the ``promoted_todo`` tag.

    Then walks the items, skipping ``completed`` ones, capping at
    ``MAX_PROMOTIONS_PER_RUN``. Per-item failures are logged and skipped —
    one bad item never blocks the others.
    """
    if os.environ.get("ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED", "0") != "1":
        logger.debug(
            "todo_promotion.skipped parent=%s reason=env_disabled",
            parent.get("id"),
            extra={
                "event": "todo_promotion.skipped",
                "parent_task_id": parent.get("id"),
                "reason": "env_disabled",
            },
        )
        return []

    todo_enabled = bool(getattr(agent_config, "todo_list_enabled", False))
    task_protocol = bool(getattr(agent_config, "task_protocol", False))
    if not todo_enabled or not task_protocol:
        logger.debug(
            "todo_promotion.skipped parent=%s reason=manifest_opt_out",
            parent.get("id"),
            extra={
                "event": "todo_promotion.skipped",
                "parent_task_id": parent.get("id"),
                "reason": "manifest_opt_out",
            },
        )
        return []

    if not should_promote(parent):
        logger.debug(
            "todo_promotion.skipped parent=%s reason=cycle_guard",
            parent.get("id"),
            extra={
                "event": "todo_promotion.skipped",
                "parent_task_id": parent.get("id"),
                "reason": "cycle_guard",
            },
        )
        return []

    created: list[str] = []
    for item in items:
        if len(created) >= MAX_PROMOTIONS_PER_RUN:
            logger.debug(
                "todo_promotion.skipped parent=%s reason=cap_exceeded",
                parent.get("id"),
                extra={
                    "event": "todo_promotion.skipped",
                    "parent_task_id": parent.get("id"),
                    "reason": "cap_exceeded",
                },
            )
            break
        if getattr(item, "status", "") == "completed":
            continue
        try:
            subtask_id = promote_todo_to_subtask(
                parent=parent,
                item=item,
                agent_id=agent_id,
                run_id=run_id,
                tenant_id=tenant_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("todo_promotion: per-item failure (continuing): %s", e)
            continue
        if subtask_id:
            created.append(subtask_id)
    return created
