"""Member approval: the one place the operator-only Inbox gate widens.

A non-operator (tenant role ``member`` or ``user``, a person, not a service)
may list and decide a proposed action (``operation_actions``) only when ALL
of these hold, each read from the database at the moment of the decision:

0. the caller is, in the database now, an active ``member`` or ``user``
   account of the tenant (not just per their token);
1. the action was proposed for the caller (``proposed_for_user_id``), by a
   run rather than a plugin (``proposed_by`` is NULL), and is not of a kind
   only an operator decides (``OPERATOR_ONLY_KIND_PATTERNS``);
2. the run that proposed it (``proposed_by_run_id``) is the caller's own
   interactive webchat turn on that workspace's session: its ``agent_runs``
   row has ``trigger_type='webchat'``, ``trigger_detail`` exactly
   ``webchat:<the workspace's session key>`` and ``user_id`` the caller;
3. the caller can post in that workspace now: a current member (shared or
   assigned), the workspace not archived (``access.require(..., "post")``);
4. the workspace is a task workspace and its task (``rooms.task_id``, linked
   in ``room_tasks``, not deleted, not resolved or done) has the caller as its
   human assignee (``crm_tasks.assignee_id``). A member cannot give themselves
   a task workspace's own task (``tasks.accept_task`` refuses a ``subject``
   link); they become an assignee by accepting a suggestion, or someone
   assigns them.

Anything else is ``NotPermitted``, and stays with the tenant's operators. The
check runs once before the decision and again inside it, under the action's
row lock (``Operations.decide(member_check=...)``), so access removed in
between is honoured. The verdict is recorded as ``member:<user id>`` in the
ledger's own audit; what executes is bound to the approved payload hash
exactly as for an operator's approval.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from robothor.operations.store import NotPermitted, Operations
from robothor.rooms import access
from robothor.rooms.errors import RoomError
from robothor.rooms.keys import canonical_uuid

if TYPE_CHECKING:
    from robothor.rooms.access import Actor

logger = logging.getLogger(__name__)

__all__ = [
    "MEMBER_ROLES",
    "OPERATOR_ONLY_KIND_PATTERNS",
    "decide_member_action",
    "list_member_actions",
    "member_actor_id",
]

#: The tenant roles the member path is for. Owners and admins are operators.
MEMBER_ROLES = frozenset({"member", "user"})
#: Action kinds (SQL ``LIKE`` patterns) that only an operator may decide,
#: whoever proposed them: the sales pipeline accepts only an ``operator:``
#: verdict on what it sends and follows up (``robothor/sales/gmail_sync.py``).
OPERATOR_ONLY_KIND_PATTERNS = ("sales.%",)
_LIST_LIMIT = 100

_ELIGIBLE = """
SELECT a.id, a.kind, a.payload, a.created_at, a.expires_at, a.proposed_by_run_id,
       r.id AS room_id, r.task_id
FROM operation_actions a
JOIN rooms r ON r.tenant_id = a.tenant_id AND r.id = a.room_id
JOIN agent_runs ar ON ar.tenant_id = a.tenant_id AND ar.id::text = a.proposed_by_run_id
JOIN room_members m ON m.tenant_id = r.tenant_id AND m.room_id = r.id AND m.user_id = %(user)s
JOIN room_tasks rt ON rt.tenant_id = r.tenant_id AND rt.room_id = r.id AND rt.task_id = r.task_id
JOIN crm_tasks t ON t.tenant_id = r.tenant_id AND t.id = r.task_id AND t.deleted_at IS NULL
JOIN user_accounts u ON u.tenant_id = a.tenant_id AND u.id = %(user)s
WHERE a.tenant_id = %(tenant)s
  AND a.proposed_for_user_id = %(user)s
  AND a.proposed_by IS NULL
  AND NOT (a.kind LIKE ANY (%(operator_kinds)s))
  AND u.status = 'active'
  AND u.role IN ('member', 'user')
  AND t.resolved_at IS NULL
  AND COALESCE(t.status, '') <> 'DONE'
  AND ar.trigger_type = 'webchat'
  AND ar.trigger_detail = 'webchat:' || r.session_key
  AND ar.user_id = %(user)s::text
  AND r.kind = 'task'
  AND r.archived_at IS NULL
  AND t.assignee_id = %(user)s
"""


def member_actor_id(actor: Actor) -> str:
    """How the ledger records a member's verdict."""
    return f"member:{actor.user_id}"


def _member(actor: Actor) -> str | None:
    """The caller's user id when the member path applies to them at all."""
    if actor.is_service or actor.role not in MEMBER_ROLES:
        return None
    return canonical_uuid(actor.user_id)


def _eligible_in(
    cur: Any, actor: Actor, action_id: str | None, *, pending_only: bool = False
) -> list[dict[str, Any]]:
    """The eligible actions (one, or all) on ``cur``; the SQL half of the rule."""
    user = _member(actor)
    if user is None:
        return []
    sql = _ELIGIBLE
    params: dict[str, Any] = {
        "tenant": actor.tenant_id,
        "user": user,
        "operator_kinds": list(OPERATOR_ONLY_KIND_PATTERNS),
    }
    if action_id is not None:
        sql += " AND a.id = %(id)s"
        params["id"] = action_id
    if pending_only:
        sql += " AND a.status = 'review' AND a.expires_at > now()"
    sql += " ORDER BY a.expires_at, a.id LIMIT %(limit)s"
    params["limit"] = _LIST_LIMIT
    cur.execute(sql, params)
    return [
        {
            **dict(row),
            "id": str(row["id"]),
            "room_id": str(row["room_id"]),
            "task_id": str(row["task_id"]),
        }
        for row in cur.fetchall()
    ]


def _can_post(actor: Actor, room_id: str) -> bool:
    """The access half of the rule, through the rooms access rules themselves."""
    try:
        access.require(actor, room_id, "post")
    except RoomError:
        return False
    return True


def list_member_actions(actor: Actor) -> list[dict[str, Any]]:
    """The caller's own actions waiting for their verdict, soonest to expire
    first: ``{id, kind, payload, created_at, expires_at, room_id, task_id}``."""
    if _member(actor) is None:
        return []
    with Operations(actor.tenant_id).transaction() as cur:
        rows = _eligible_in(cur, actor, None, pending_only=True)
    return [
        {k: v for k, v in row.items() if k != "proposed_by_run_id"}
        for row in rows
        if _can_post(actor, row["room_id"])
    ]


def decide_member_action(actor: Actor, action_id: str, *, approved: bool) -> bool:
    """Approve or reject the caller's own action. True once settled.

    Raises ``NotPermitted`` when the member rule does not hold (or the action
    is not the tenant's), and ``Conflict`` when it was already decided or has
    expired."""
    action = canonical_uuid(action_id)
    if _member(actor) is None or action is None:
        raise NotPermitted("only an operator can decide this action")
    ops = Operations(actor.tenant_id)
    with ops.transaction() as cur:
        found = _eligible_in(cur, actor, action)
    if not found or not _can_post(actor, found[0]["room_id"]):
        raise NotPermitted("only an operator can decide this action")

    def recheck(cur: Any, row: dict[str, Any]) -> dict[str, Any] | None:
        again = _eligible_in(cur, actor, action)
        if not again:
            return None
        return {
            "room_id": again[0]["room_id"],
            "task_id": again[0]["task_id"],
            "run_id": again[0]["proposed_by_run_id"],
        }

    ops.decide(action, bool(approved), member_actor_id(actor), member_check=recheck)
    return True
