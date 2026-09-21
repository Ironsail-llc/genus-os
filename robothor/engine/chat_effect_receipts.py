"""Project durable native effects into chat recovery without executing tools."""


def family_effect_receipts(cur, run, auth):
    cur.execute(
        """WITH RECURSIVE family AS (
            SELECT id FROM agent_runs WHERE id=%s AND tenant_id=%s AND user_id=%s
            UNION
            SELECT child.id FROM agent_runs child JOIN family parent
              ON child.parent_run_id=parent.id
                 OR child.runtime_context->>'resume_from_run_id'=parent.id::text
            WHERE child.tenant_id=%s AND child.user_id=%s
        ) SELECT e.id,e.agent_id,e.tool_name,e.state,e.resolution
          FROM agent_runtime_effects e JOIN family f ON e.run_id=f.id::text
          WHERE e.tenant_id=%s AND e.principal_id=%s
            AND e.state IN ('prepared','dispatching','uncertain','confirmed','not_applied')
          ORDER BY e.created_at,e.id""",
        (
            run["id"],
            auth.tenant_id,
            auth.user_id,
            auth.tenant_id,
            auth.user_id,
            auth.tenant_id,
            auth.user_id,
        ),
    )
    receipts = []
    for row in cur.fetchall():
        resolution = row["resolution"] if isinstance(row["resolution"], dict) else {}
        result = resolution.get("result")
        verified = (
            row["state"] == "confirmed"
            and isinstance(result, dict)
            and not result.get("error")
            and bool(resolution.get("reference"))
        )
        receipts.append(
            {
                "operation_id": str(row["id"]),
                "agent_id": row["agent_id"],
                "kind": "runtime_effect",
                "tool_name": row["tool_name"],
                "status": row["state"],
                "verified": verified,
                "deduplicated": verified and result.get("deduplicated") is True,
                "reconciliation_pending": row["state"] in {"prepared", "dispatching", "uncertain"},
            }
        )
    return receipts


def effect_summary(receipt):
    if receipt["verified"]:
        if receipt["tool_name"] == "create_note":
            text = "The CRM note was created. Robothor checked the stored note and recovered its result without creating another note."
        elif receipt["tool_name"] == "create_task":
            text = (
                "The existing task was found."
                if receipt.get("deduplicated")
                else "The task was created."
            )
            text += " Robothor checked the stored task and recovered its result without creating another task."
        else:
            text = "The recorded action is confirmed by its saved verification evidence."
    elif receipt["status"] == "prepared":
        text = "The action was recorded for execution; the audit does not show dispatch yet."
    elif receipt["status"] == "not_applied":
        text = "The audit confirms that this action was not applied."
    else:
        text = "The audit records an action whose outcome is still unresolved."
    return f"{text} (Operation {receipt['operation_id']})"
