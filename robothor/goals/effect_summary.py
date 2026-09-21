"""Goal-family action evidence from durable, tenant-scoped runtime records."""


def summary(cur, tenant, goal_id, *, exclude_id=None):
    cur.execute(
        """WITH RECURSIVE family AS (
            SELECT id FROM pursuit_goals WHERE tenant_id=%s AND id=%s
            UNION
            SELECT child.id FROM pursuit_goals child JOIN family parent
              ON child.data->>'parent_goal_id'=parent.id::text WHERE child.tenant_id=%s
        ) SELECT count(*) FILTER (WHERE e.state IN ('prepared','dispatching','uncertain')) AS pending,
                 count(*) FILTER (WHERE e.state='confirmed') AS confirmed
          FROM agent_runtime_effects e JOIN family f ON e.goal_id=f.id::text
          WHERE e.tenant_id=%s AND (%s::uuid IS NULL OR e.id<>%s::uuid)""",
        (tenant, goal_id, tenant, tenant, exclude_id, exclude_id),
    )
    return dict(cur.fetchone())


def require_settled(cur, tenant, goal_id):
    from robothor.engine.runtime.effects import active_effect

    # This control's own prepared admission is not an earlier business effect.
    current = active_effect.get()
    excluded = current["id"] if current and current["tool_name"] == "update_pursuit_goal" else None
    if summary(cur, tenant, goal_id, exclude_id=excluded)["pending"]:
        raise ValueError("earlier goal-family actions still require audit readback")
