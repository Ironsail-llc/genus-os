"""Goal-family action evidence from durable, tenant-scoped runtime records."""


def summaries(cur, tenant, goal_ids, *, exclude_id=None):
    """One aggregate query for a bounded goal list and all its descendants."""
    if not goal_ids:
        return {}
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
        (tenant, list(goal_ids), tenant, tenant, exclude_id, exclude_id),
    )
    return {
        str(row["root"]): {"pending": row["pending"], "confirmed": row["confirmed"]}
        for row in cur.fetchall()
    }


def summary(cur, tenant, goal_id, *, exclude_id=None):
    return summaries(cur, tenant, [goal_id], exclude_id=exclude_id)[goal_id]


def attach(cur, tenant, goals):
    counts = summaries(cur, tenant, [goal["id"] for goal in goals])
    for goal in goals:
        goal["action_evidence"] = counts[goal["id"]]


def require_settled(cur, tenant, goal_id):
    from robothor.engine.runtime.effects import active_effect

    # This control's own prepared admission is not an earlier business effect.
    current = active_effect.get()
    excluded = current["id"] if current and current["tool_name"] == "update_pursuit_goal" else None
    if summary(cur, tenant, goal_id, exclude_id=excluded)["pending"]:
        raise ValueError("earlier goal-family actions still require audit readback")
