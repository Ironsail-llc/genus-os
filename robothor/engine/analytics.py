"""
Run Analytics — cross-agent performance analysis and anomaly detection.

Provides fleet-level health summaries, per-agent trend analysis, failure
pattern grouping, and anomaly detection against rolling baselines.

Used by: Failure Analyzer agent, Improvement Analyst agent, morning briefing.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "robothor-primary"


def get_agent_stats(
    agent_id: str,
    days: int = 7,
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any]:
    """Get detailed stats for a single agent over a time window.

    Returns: success_rate, avg_tokens, avg_cost, avg_duration_ms,
    error_rate, total_runs, top_error_types, daily_breakdown.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Aggregate stats
        cur.execute(
            """
            SELECT
                COUNT(*) as total_runs,
                COUNT(*) FILTER (WHERE status = 'completed') as completed,
                COUNT(*) FILTER (WHERE status = 'failed') as failed,
                COUNT(*) FILTER (WHERE status = 'timeout') as timeouts,
                COUNT(*) FILTER (WHERE budget_exhausted = true) as budget_exhausted,
                AVG(duration_ms) FILTER (WHERE status IN ('completed', 'failed')) as avg_duration_ms,
                AVG(input_tokens + output_tokens) FILTER (WHERE status = 'completed') as avg_tokens,
                AVG(total_cost_usd) FILTER (WHERE status = 'completed') as avg_cost_usd,
                SUM(total_cost_usd) as total_cost_usd,
                SUM(input_tokens) as total_input_tokens,
                SUM(output_tokens) as total_output_tokens
            FROM agent_runs
            WHERE agent_id = %s
              AND tenant_id = %s
              AND created_at > NOW() - make_interval(days := %s)
              AND parent_run_id IS NULL
            """,
            (agent_id, tenant_id, days),
        )
        stats = dict(cur.fetchone() or {})

        total = stats.get("total_runs", 0) or 0
        completed = stats.get("completed", 0) or 0
        failed = stats.get("failed", 0) or 0

        stats["success_rate"] = round(completed / total, 4) if total > 0 else None
        stats["error_rate"] = round(failed / total, 4) if total > 0 else None

        # Convert Decimals
        for key in ("avg_duration_ms", "avg_tokens", "avg_cost_usd", "total_cost_usd"):
            if stats.get(key) is not None:
                stats[key] = float(stats[key])

        # Top error types (from error_message patterns)
        cur.execute(
            """
            SELECT
                COALESCE(
                    CASE
                        WHEN error_message LIKE '%%timeout%%' THEN 'timeout'
                        WHEN error_message LIKE '%%rate%%limit%%' THEN 'rate_limit'
                        WHEN error_message LIKE '%%budget%%' THEN 'budget_exhausted'
                        WHEN error_message LIKE '%%auth%%' THEN 'auth_error'
                        WHEN error_message LIKE '%%connection%%' THEN 'connection_error'
                        ELSE 'other'
                    END,
                    'unknown'
                ) as error_type,
                COUNT(*) as count
            FROM agent_runs
            WHERE agent_id = %s
              AND tenant_id = %s
              AND status IN ('failed', 'timeout')
              AND created_at > NOW() - make_interval(days := %s)
              AND parent_run_id IS NULL
            GROUP BY error_type
            ORDER BY count DESC
            LIMIT 5
            """,
            (agent_id, tenant_id, days),
        )
        stats["top_error_types"] = [dict(r) for r in cur.fetchall()]

    return stats


def get_fleet_health(
    days: int = 1,
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any]:
    """Get health summary for all agents in the fleet.

    Returns per-agent: total_runs, success_rate, avg_cost, last_run_status.
    Plus fleet-wide totals.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT
                agent_id,
                COUNT(*) as total_runs,
                COUNT(*) FILTER (WHERE status = 'completed') as completed,
                COUNT(*) FILTER (WHERE status = 'failed') as failed,
                COUNT(*) FILTER (WHERE status = 'timeout') as timeouts,
                AVG(total_cost_usd) FILTER (WHERE status = 'completed') as avg_cost_usd,
                SUM(total_cost_usd) as total_cost_usd,
                MAX(created_at) as last_run_at
            FROM agent_runs
            WHERE tenant_id = %s
              AND created_at > NOW() - make_interval(days := %s)
              AND parent_run_id IS NULL
            GROUP BY agent_id
            ORDER BY agent_id
            """,
            (tenant_id, days),
        )
        rows = cur.fetchall()

    agents = []
    fleet_total = 0
    fleet_completed = 0
    fleet_failed = 0
    fleet_cost = 0.0

    for row in rows:
        row = dict(row)
        total = row["total_runs"] or 0
        completed = row["completed"] or 0
        failed = row["failed"] or 0

        fleet_total += total
        fleet_completed += completed
        fleet_failed += failed
        fleet_cost += float(row["total_cost_usd"] or 0)

        agents.append(
            {
                "agent_id": row["agent_id"],
                "total_runs": total,
                "completed": completed,
                "failed": failed,
                "timeouts": row["timeouts"] or 0,
                "success_rate": round(completed / total, 4) if total > 0 else None,
                "avg_cost_usd": float(row["avg_cost_usd"]) if row.get("avg_cost_usd") else None,
                "total_cost_usd": float(row["total_cost_usd"])
                if row.get("total_cost_usd")
                else None,
                "last_run_at": str(row["last_run_at"]) if row.get("last_run_at") else None,
            }
        )

    return {
        "agents": agents,
        "fleet_totals": {
            "total_runs": fleet_total,
            "completed": fleet_completed,
            "failed": fleet_failed,
            "success_rate": round(fleet_completed / fleet_total, 4) if fleet_total > 0 else None,
            "total_cost_usd": round(fleet_cost, 4),
        },
        "period_days": days,
    }


def detect_anomalies(
    agent_id: str,
    baseline_days: int = 7,
    recent_hours: int = 24,
    sigma_threshold: float = 2.0,
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any]:
    """Compare recent performance against a rolling baseline.

    Flags anomalies when recent metrics deviate by more than sigma_threshold
    standard deviations from the baseline mean.

    Returns: anomalies list (metric, baseline_mean, baseline_stddev, recent_value, sigma_deviation).
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Baseline: daily aggregates over baseline_days
        cur.execute(
            """
            SELECT
                DATE(created_at) as day,
                COUNT(*) as total_runs,
                COUNT(*) FILTER (WHERE status = 'completed') as completed,
                COUNT(*) FILTER (WHERE status = 'failed') as failed,
                AVG(duration_ms) FILTER (WHERE status = 'completed') as avg_duration_ms,
                AVG(total_cost_usd) FILTER (WHERE status = 'completed') as avg_cost_usd,
                AVG(input_tokens + output_tokens) FILTER (WHERE status = 'completed') as avg_tokens
            FROM agent_runs
            WHERE agent_id = %s
              AND tenant_id = %s
              AND created_at > NOW() - make_interval(days := %s)
              AND created_at <= NOW() - make_interval(hours := %s)
              AND parent_run_id IS NULL
            GROUP BY DATE(created_at)
            ORDER BY day
            """,
            (agent_id, tenant_id, baseline_days, recent_hours),
        )
        baseline_rows = [dict(r) for r in cur.fetchall()]

        # Recent: aggregate over recent_hours
        cur.execute(
            """
            SELECT
                COUNT(*) as total_runs,
                COUNT(*) FILTER (WHERE status = 'completed') as completed,
                COUNT(*) FILTER (WHERE status = 'failed') as failed,
                AVG(duration_ms) FILTER (WHERE status = 'completed') as avg_duration_ms,
                AVG(total_cost_usd) FILTER (WHERE status = 'completed') as avg_cost_usd,
                AVG(input_tokens + output_tokens) FILTER (WHERE status = 'completed') as avg_tokens
            FROM agent_runs
            WHERE agent_id = %s
              AND tenant_id = %s
              AND created_at > NOW() - make_interval(hours := %s)
              AND parent_run_id IS NULL
            """,
            (agent_id, tenant_id, recent_hours),
        )
        recent = dict(cur.fetchone() or {})

    if not baseline_rows or (recent.get("total_runs") or 0) == 0:
        return {"agent_id": agent_id, "anomalies": [], "baseline_days": len(baseline_rows)}

    # Calculate baseline stats and check for anomalies
    anomalies = []
    metrics_to_check: list[tuple[str, Callable[[dict[str, Any]], float], bool]] = [
        ("error_rate", lambda r: (r["failed"] or 0) / max(r["total_runs"] or 1, 1), True),
        ("avg_duration_ms", lambda r: float(r.get("avg_duration_ms") or 0), True),
        ("avg_cost_usd", lambda r: float(r.get("avg_cost_usd") or 0), True),
        ("avg_tokens", lambda r: float(r.get("avg_tokens") or 0), True),
    ]

    for metric_name, extractor, higher_is_worse in metrics_to_check:
        baseline_values = [extractor(r) for r in baseline_rows]
        recent_value = extractor(recent)

        if len(baseline_values) < 2:
            continue

        mean = sum(baseline_values) / len(baseline_values)
        variance = sum((v - mean) ** 2 for v in baseline_values) / len(baseline_values)
        stddev = math.sqrt(variance) if variance > 0 else 0

        if stddev == 0:
            # No variance — flag if recent differs from mean at all (by more than 10%)
            if mean > 0 and abs(recent_value - mean) / mean > 0.1:
                anomalies.append(
                    {
                        "metric": metric_name,
                        "baseline_mean": round(mean, 4),
                        "baseline_stddev": 0,
                        "recent_value": round(recent_value, 4),
                        "sigma_deviation": None,
                        "direction": "higher" if recent_value > mean else "lower",
                    }
                )
            continue

        deviation = (recent_value - mean) / stddev
        if (higher_is_worse and deviation > sigma_threshold) or (
            not higher_is_worse and deviation < -sigma_threshold
        ):
            anomalies.append(
                {
                    "metric": metric_name,
                    "baseline_mean": round(mean, 4),
                    "baseline_stddev": round(stddev, 4),
                    "recent_value": round(recent_value, 4),
                    "sigma_deviation": round(deviation, 2),
                    "direction": "higher" if deviation > 0 else "lower",
                }
            )

    return {
        "agent_id": agent_id,
        "anomalies": anomalies,
        "baseline_days": len(baseline_rows),
        "recent_hours": recent_hours,
    }


def get_failure_patterns(
    hours: int = 24,
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any]:
    """Group recent failures by agent and error type.

    Returns failure clusters with counts — used by Failure Analyzer to
    prioritize which failures to investigate.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT
                agent_id,
                COALESCE(
                    CASE
                        WHEN error_message LIKE '%%timeout%%' THEN 'timeout'
                        WHEN error_message LIKE '%%rate%%limit%%' THEN 'rate_limit'
                        WHEN error_message LIKE '%%budget%%' THEN 'budget_exhausted'
                        WHEN error_message LIKE '%%auth%%' THEN 'auth_error'
                        WHEN error_message LIKE '%%connection%%' THEN 'connection_error'
                        WHEN error_message LIKE '%%not found%%' THEN 'not_found'
                        WHEN error_message LIKE '%%permission%%' THEN 'permission_error'
                        WHEN status = 'timeout' THEN 'timeout'
                        ELSE 'other'
                    END,
                    'unknown'
                ) as error_type,
                COUNT(*) as count,
                MAX(created_at) as last_occurrence,
                array_agg(DISTINCT LEFT(error_message, 200)) FILTER (WHERE error_message IS NOT NULL)
                    as sample_messages
            FROM agent_runs
            WHERE tenant_id = %s
              AND status IN ('failed', 'timeout')
              AND created_at > NOW() - make_interval(hours := %s)
              AND parent_run_id IS NULL
            GROUP BY agent_id, error_type
            ORDER BY count DESC
            LIMIT 20
            """,
            (tenant_id, hours),
        )
        patterns = []
        for row in cur.fetchall():
            row = dict(row)
            row["last_occurrence"] = (
                str(row["last_occurrence"]) if row.get("last_occurrence") else None
            )
            # Trim sample messages to first 3
            samples = row.get("sample_messages") or []
            row["sample_messages"] = samples[:3]
            patterns.append(row)

    return {
        "patterns": patterns,
        "total_clusters": len(patterns),
        "period_hours": hours,
    }
