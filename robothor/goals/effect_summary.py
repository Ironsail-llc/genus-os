"""Goal-family action evidence and explicit operator reconciliation."""

from typing import Any

from psycopg2.extras import Json


def installed(cur: Any) -> bool:
    cur.execute("SELECT to_regclass('agent_runtime_effects') IS NOT NULL AS present")
    row = cur.fetchone()
    return bool(row and row["present"])


def summaries(
    cur: Any, tenant: str, goal_ids: list[str], *, exclude_id: Any = None
) -> dict[str, dict[str, int]]:
    if not goal_ids:
        return {}
    if not installed(cur):
        return {identifier: {"pending": 0, "confirmed": 0} for identifier in goal_ids}
    cur.execute(
        """WITH RECURSIVE family AS (
            SELECT id AS root,id FROM pursuit_goals WHERE tenant_id=%s AND id::text=ANY(%s)
            UNION
            SELECT parent.root,child.id FROM pursuit_goals child JOIN family parent
              ON child.data->>'parent_goal_id'=parent.id::text WHERE child.tenant_id=%s
        ) SELECT f.root,
                 count(*) FILTER (WHERE e.state IN ('prepared','dispatching','uncertain')) AS pending,
                 count(*) FILTER (WHERE e.state='confirmed') AS confirmed
          FROM family f LEFT JOIN agent_runtime_effects e
            ON e.goal_id=f.id::text AND e.tenant_id=%s
              AND e.state IN ('prepared','dispatching','uncertain','confirmed')
              AND (%s::uuid IS NULL OR e.id<>%s::uuid)
          GROUP BY f.root""",
        (tenant, goal_ids, tenant, tenant, exclude_id, exclude_id),
    )
    return {
        str(row["root"]): {"pending": row["pending"], "confirmed": row["confirmed"]}
        for row in cur.fetchall()
    }


def summary(cur: Any, tenant: str, goal_id: str, *, exclude_id: Any = None) -> dict[str, int]:
    return summaries(cur, tenant, [goal_id], exclude_id=exclude_id)[goal_id]


def attach(cur: Any, tenant: str, goals: list[dict[str, Any]]) -> None:
    counts = summaries(cur, tenant, [goal["id"] for goal in goals])
    for goal in goals:
        goal["action_evidence"] = counts[goal["id"]]


def require_settled(cur: Any, tenant: str, goal_id: str) -> None:
    from robothor.engine.runtime.effects import active_effect

    current = active_effect.get()
    excluded = current["id"] if current and current["tool_name"] == "update_pursuit_goal" else None
    if summary(cur, tenant, goal_id, exclude_id=excluded)["pending"]:
        raise ValueError("earlier goal-family actions still require audit readback")


def reconcile_family(
    cur: Any, tenant: str, goal_id: str, actor: str, note: str, *, operator: bool
) -> None:
    """Settle unknown receipts in the goal transaction, without claiming verification."""
    if not installed(cur):
        return
    cur.execute(
        """WITH RECURSIVE family AS (
            SELECT id FROM pursuit_goals WHERE tenant_id=%s AND id=%s
            UNION SELECT g.id FROM pursuit_goals g JOIN family f
              ON g.data->>'parent_goal_id'=f.id::text WHERE g.tenant_id=%s
        ) SELECT e.id,e.state FROM agent_runtime_effects e JOIN family f ON e.goal_id=f.id::text
          WHERE e.tenant_id=%s AND e.state IN ('prepared','dispatching','uncertain') FOR UPDATE OF e""",
        (tenant, goal_id, tenant, tenant),
    )
    rows = cur.fetchall()
    if not rows:
        return
    if not operator or not actor.strip() or not note.strip():
        raise ValueError(
            "operator reconciliation with an audit note is required for unknown actions"
        )
    if any(row["state"] != "uncertain" for row in rows):
        raise ValueError("stop active dispatches before reconciliation")
    cur.execute(
        """UPDATE agent_runtime_effects SET state='finished',resolution=%s,
           version=version+1,updated_at=now() WHERE tenant_id=%s AND id=ANY(%s::uuid[])
           AND state='uncertain'""",
        (
            Json({"source": "reconciled", "verified": False, "actor": actor, "note": note}),
            tenant,
            [str(row["id"]) for row in rows],
        ),
    )
