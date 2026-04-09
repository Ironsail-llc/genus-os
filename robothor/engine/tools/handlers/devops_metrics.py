"""DevOps metrics storage and query tool handlers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


def _get_conn() -> Any:
    """Get a database connection. Use: with _get_conn() as conn:"""
    from robothor.engine.tools.dispatch import get_db

    return get_db()


@_handler("devops_store_metric")
async def _devops_store_metric(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Store a devops metric snapshot (upsert)."""
    source = args.get("source", "")
    metric_type = args.get("metric_type", "")
    value = args.get("value")

    if not source or not metric_type or value is None:
        return {"error": "source, metric_type, and value are required"}

    snapshot_date = args.get("snapshot_date", datetime.now(UTC).date().isoformat())
    scope = args.get("scope", "team")
    scope_key = args.get("scope_key", "")

    import json

    try:
        with _get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devops_metrics_snapshots
                    (tenant_id, snapshot_date, source, metric_type, scope, scope_key, value_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, snapshot_date, source, metric_type, scope, scope_key)
                DO UPDATE SET value_json = EXCLUDED.value_json, collected_at = now()
                """,
                (
                    ctx.tenant_id,
                    snapshot_date,
                    source,
                    metric_type,
                    scope,
                    scope_key,
                    json.dumps(value) if not isinstance(value, str) else value,
                ),
            )
            conn.commit()
    except Exception as e:
        return {"error": f"Failed to store metric: {e}"}

    return {
        "stored": True,
        "source": source,
        "metric_type": metric_type,
        "snapshot_date": snapshot_date,
        "scope": scope,
        "scope_key": scope_key,
    }


@_handler("devops_query_metrics")
async def _devops_query_metrics(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Query stored devops metrics for trend analysis."""
    source = args.get("source", "")
    metric_type = args.get("metric_type", "")
    days = min(args.get("days", 30), 90)

    if not source or not metric_type:
        return {"error": "source and metric_type are required"}

    scope = args.get("scope")
    scope_key = args.get("scope_key")
    since = (datetime.now(UTC).date() - timedelta(days=days)).isoformat()

    try:
        with _get_conn() as conn, conn.cursor() as cur:
            query = """
                SELECT snapshot_date, scope, scope_key, value_json, collected_at
                FROM devops_metrics_snapshots
                WHERE tenant_id = %s
                  AND source = %s
                  AND metric_type = %s
                  AND snapshot_date >= %s
            """
            params: list[Any] = [ctx.tenant_id, source, metric_type, since]

            if scope:
                query += " AND scope = %s"
                params.append(scope)
            if scope_key:
                query += " AND scope_key = %s"
                params.append(scope_key)

            query += " ORDER BY snapshot_date DESC"
            cur.execute(query, params)

            rows = cur.fetchall()
    except Exception as e:
        return {"error": f"Failed to query metrics: {e}"}

    snapshots = [
        {
            "snapshot_date": row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0]),
            "scope": row[1],
            "scope_key": row[2],
            "value": row[3],
            "collected_at": row[4].isoformat() if hasattr(row[4], "isoformat") else str(row[4]),
        }
        for row in rows
    ]

    return {
        "source": source,
        "metric_type": metric_type,
        "days": days,
        "count": len(snapshots),
        "snapshots": snapshots,
    }
