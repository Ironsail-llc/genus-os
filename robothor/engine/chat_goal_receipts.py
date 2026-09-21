"""Recover committed goal controls from history attributed by trusted dispatch."""


def family_goal_receipts(cur, run, auth):
    cur.execute(
        """WITH RECURSIVE family AS (
            SELECT id FROM agent_runs
            WHERE id=%s AND tenant_id=%s AND user_id=%s
            UNION
            SELECT child.id FROM agent_runs child JOIN family parent
              ON (child.parent_run_id=parent.id
                  OR child.runtime_context->>'resume_from_run_id'=parent.id::text)
            WHERE child.tenant_id=%s AND child.user_id=%s
        ) SELECT h.id,h.goal_id,h.action,h.detail FROM pursuit_goal_history h
          JOIN family ON h.detail->'control_receipt'->>'run_id'=family.id::text
          WHERE h.tenant_id=%s AND h.actor=%s
            AND h.detail->'control_receipt'->>'principal_id'=%s
            AND h.action IN ('pause','cancel','resume')
          ORDER BY h.created_at,h.id""",
        (
            run["id"],
            auth.tenant_id,
            auth.user_id,
            auth.tenant_id,
            auth.user_id,
            auth.tenant_id,
            auth.user_id,
            auth.user_id,
        ),
    )
    return [
        {
            "kind": "goal_control",
            "operation_id": str(row["id"]),
            "goal_id": str(row["goal_id"]),
            "action": row["action"],
            "status": row["detail"]["control_receipt"]["status"],
            "version": row["detail"]["control_receipt"]["version"],
            "verified": True,
        }
        for row in cur.fetchall()
    ]
