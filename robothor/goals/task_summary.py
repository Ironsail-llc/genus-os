"""Bounded task facts for goal discovery, without inferring goal completion."""

from typing import Any


def attach_task_summaries(cur: Any, tenant: str, goals: list[dict[str, Any]]) -> None:
    summaries: dict[str, dict[str, Any]] = {}
    for goal in goals:
        summary: dict[str, Any] = {"total": 0, "by_status": {}}
        goal["task_summary"] = summary
        summaries[goal["id"]] = summary
    cur.execute(
        """WITH ranked AS (
            SELECT l.goal_id,t.id,t.title,t.status,
                   count(*) OVER (PARTITION BY l.goal_id,t.status) AS status_count,
                   row_number() OVER (PARTITION BY l.goal_id,t.status ORDER BY t.id) AS position
            FROM pursuit_goal_tasks l JOIN crm_tasks t
              ON t.id=l.task_id AND t.tenant_id=l.tenant_id
            WHERE l.tenant_id=%s AND l.goal_id=ANY(%s::uuid[])
        ) SELECT goal_id,id,title,status,status_count FROM ranked
          WHERE position<=3 ORDER BY goal_id,status,id""",
        (tenant, [goal["id"] for goal in goals]),
    )
    for row in cur.fetchall():
        summary = summaries[str(row["goal_id"])]
        status = row["status"] or "UNKNOWN"
        if status not in summary["by_status"]:
            summary["total"] += row["status_count"]
            summary["by_status"][status] = {
                "count": row["status_count"],
                "preview": [],
                "truncated": row["status_count"] > 3,
            }
        summary["by_status"][status]["preview"].append(
            {"id": str(row["id"]), "title": row["title"]}
        )
