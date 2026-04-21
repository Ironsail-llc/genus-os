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
    from datetime import datetime

import contextlib

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)


def get_agent_stats(
    agent_id: str,
    days: int = 7,
    tenant_id: str = DEFAULT_TENANT,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Get detailed stats for a single agent over a time window.

    Returns: success_rate, avg_tokens, avg_cost, avg_duration_ms,
    error_rate, total_runs, top_error_types, daily_breakdown.

    ``as_of`` anchors the window's right edge (default: now). Rolling-history
    callers pass distinct ``as_of`` to get snapshots at past timestamps.
    """
    # Window predicate shared by every SELECT. When ``as_of`` is None we keep
    # the NOW()-relative shape; otherwise the window is bounded above by
    # ``as_of``. ``window_sql`` is a literal string, never user input.
    if as_of is None:
        window_sql = "created_at > NOW() - make_interval(days := %s)"
        window_params: tuple[Any, ...] = (days,)
    else:
        window_sql = (
            "created_at > %s::timestamptz - make_interval(days := %s) "
            "AND created_at <= %s::timestamptz"
        )
        window_params = (as_of, days, as_of)

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Aggregate stats
        cur.execute(
            f"""
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
              AND {window_sql}
              AND parent_run_id IS NULL
            """,  # noqa: S608 — window_sql is a literal, not user input
            (agent_id, tenant_id, *window_params),
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
            f"""
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
              AND {window_sql}
              AND parent_run_id IS NULL
            GROUP BY error_type
            ORDER BY count DESC
            LIMIT 5
            """,  # noqa: S608
            (agent_id, tenant_id, *window_params),
        )
        stats["top_error_types"] = [dict(r) for r in cur.fetchall()]

        # Outcome assessment distribution (interactive runs only)
        cur.execute(
            f"""
            SELECT
                outcome_assessment,
                COUNT(*) as count
            FROM agent_runs
            WHERE agent_id = %s
              AND tenant_id = %s
              AND {window_sql}
              AND parent_run_id IS NULL
              AND outcome_assessment IS NOT NULL
            GROUP BY outcome_assessment
            ORDER BY count DESC
            """,  # noqa: S608
            (agent_id, tenant_id, *window_params),
        )
        outcome_rows = cur.fetchall()
        outcome_dist = {r["outcome_assessment"]: r["count"] for r in outcome_rows}
        stats["outcome_distribution"] = outcome_dist

        successful = outcome_dist.get("successful", 0)
        unsatisfied = sum(outcome_dist.get(k, 0) for k in ("partial", "incorrect"))
        stats["satisfaction_rate"] = (
            round(successful / (successful + unsatisfied), 4)
            if (successful + unsatisfied) > 0
            else None
        )

        # ── Goal-system metrics ──
        # p95 latency + cost
        cur.execute(
            f"""
            SELECT
                percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) as p95_duration_ms,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY total_cost_usd) as p95_cost_usd
            FROM agent_runs
            WHERE agent_id = %s
              AND tenant_id = %s
              AND status = 'completed'
              AND {window_sql}
              AND parent_run_id IS NULL
            """,  # noqa: S608
            (agent_id, tenant_id, *window_params),
        )
        percentiles = cur.fetchone() or {}
        stats["p95_duration_ms"] = (
            float(percentiles["p95_duration_ms"])
            if percentiles.get("p95_duration_ms") is not None
            else None
        )
        stats["p95_cost_usd"] = (
            float(percentiles["p95_cost_usd"])
            if percentiles.get("p95_cost_usd") is not None
            else None
        )

        # Delivery success rate (among runs with an announce-style delivery)
        cur.execute(
            f"""
            SELECT
                COUNT(*) FILTER (WHERE delivery_mode = 'announce') as announce_runs,
                COUNT(*) FILTER (WHERE delivery_mode = 'announce' AND delivery_status = 'delivered') as delivered
            FROM agent_runs
            WHERE agent_id = %s
              AND tenant_id = %s
              AND {window_sql}
              AND parent_run_id IS NULL
            """,  # noqa: S608
            (agent_id, tenant_id, *window_params),
        )
        drow = cur.fetchone() or {}
        announce_runs = drow.get("announce_runs") or 0
        delivered = drow.get("delivered") or 0
        stats["delivery_success_rate"] = (
            round(delivered / announce_runs, 4) if announce_runs > 0 else None
        )

        # Median + min_output_chars proxy (median char length of output_text)
        cur.execute(
            f"""
            SELECT
                percentile_cont(0.5) WITHIN GROUP (ORDER BY char_length(output_text))
                    as median_chars,
                AVG(char_length(output_text)) as avg_chars
            FROM agent_runs
            WHERE agent_id = %s
              AND tenant_id = %s
              AND status = 'completed'
              AND output_text IS NOT NULL
              AND {window_sql}
              AND parent_run_id IS NULL
            """,  # noqa: S608
            (agent_id, tenant_id, *window_params),
        )
        crow = cur.fetchone() or {}
        # Goal uses min_output_chars as "median char length must be above N"
        stats["min_output_chars"] = (
            float(crow["median_chars"]) if crow.get("median_chars") is not None else None
        )

        # Operator rating avg (from agent_reviews). Wrapped: if the fresh-install
        # skipped migration 031 the table is absent — return None rather than
        # crashing get_agent_stats entirely.
        try:
            cur.execute(
                f"""
                SELECT AVG(rating)::float as avg_rating
                FROM agent_reviews
                WHERE agent_id = %s
                  AND tenant_id = %s
                  AND reviewer_type = 'operator'
                  AND {window_sql}
                """,  # noqa: S608
                (agent_id, tenant_id, *window_params),
            )
            rrow = cur.fetchone() or {}
            stats["operator_rating_avg"] = (
                float(rrow["avg_rating"]) if rrow.get("avg_rating") is not None else None
            )
        except Exception as e:
            logger.warning("agent_reviews query failed (migration 031 applied?): %s", e)
            stats["operator_rating_avg"] = None

        # Build a window predicate that's qualified with `r.` so joins with
        # agent_run_steps don't make `created_at` ambiguous.
        if as_of is None:
            r_window_sql = "r.created_at > NOW() - make_interval(days := %s)"
        else:
            r_window_sql = (
                "r.created_at > %s::timestamptz - make_interval(days := %s) "
                "AND r.created_at <= %s::timestamptz"
            )

        # Tool success rate — fraction of tool_call steps that did not error.
        # Joins agent_run_steps to agent_runs so the window filter applies to
        # the parent run, not the step row.
        try:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (WHERE s.step_type = 'tool_call') AS tool_calls,
                    COUNT(*) FILTER (
                        WHERE s.step_type = 'tool_call' AND s.error_message IS NULL
                    ) AS tool_ok
                FROM agent_run_steps s
                JOIN agent_runs r ON r.id = s.run_id
                WHERE r.agent_id = %s
                  AND r.tenant_id = %s
                  AND r.parent_run_id IS NULL
                  AND {r_window_sql}
                """,  # noqa: S608 — r_window_sql is a literal
                (agent_id, tenant_id, *window_params),
            )
            trow = cur.fetchone() or {}
            tool_calls = trow.get("tool_calls") or 0
            tool_ok = trow.get("tool_ok") or 0
            stats["tool_success_rate"] = round(tool_ok / tool_calls, 4) if tool_calls > 0 else None
        except Exception as e:
            logger.warning("tool_success_rate query failed: %s", e)
            stats["tool_success_rate"] = None
            # Clear the aborted tx so the next query doesn't inherit it.
            with contextlib.suppress(Exception):
                conn.rollback()

        # Recovery rate — fraction of runs that had an error step followed by
        # a non-error step in the same run. 1.0 means every error was recovered
        # from within the run; 0.0 means errors always terminated the run.
        try:
            cur.execute(
                f"""
                WITH error_runs AS (
                    SELECT s.run_id, MIN(s.step_number) AS first_error
                    FROM agent_run_steps s
                    JOIN agent_runs r ON r.id = s.run_id
                    WHERE r.agent_id = %s
                      AND r.tenant_id = %s
                      AND r.parent_run_id IS NULL
                      AND {r_window_sql}
                      AND s.step_type = 'error'
                    GROUP BY s.run_id
                ),
                recovered AS (
                    SELECT er.run_id
                    FROM error_runs er
                    WHERE EXISTS (
                        SELECT 1 FROM agent_run_steps s2
                        WHERE s2.run_id = er.run_id
                          AND s2.step_number > er.first_error
                          AND s2.step_type != 'error'
                    )
                )
                SELECT
                    (SELECT COUNT(*) FROM error_runs) AS error_total,
                    (SELECT COUNT(*) FROM recovered) AS recovered_total
                """,  # noqa: S608
                (agent_id, tenant_id, *window_params),
            )
            rrow2 = cur.fetchone() or {}
            error_total = rrow2.get("error_total") or 0
            recovered_total = rrow2.get("recovered_total") or 0
            stats["recovery_rate"] = (
                round(recovered_total / error_total, 4) if error_total > 0 else None
            )
        except Exception as e:
            logger.warning("recovery_rate query failed: %s", e)
            stats["recovery_rate"] = None
            with contextlib.suppress(Exception):
                conn.rollback()

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


def thread_pool_metrics(tenant_id: str = DEFAULT_TENANT, window_days: int = 7) -> dict[str, Any]:
    """Stage 4 observability — how well is the thread pool advancing?

    Returns counts and rates for:
      - threads_advanced_per_beat — spawns with parent_task_id
      - next_action_source — breakdown of who wrote the active next_action
        (planner vs main-agent vs operator)
      - questions_answered_within_24h — % of questions_for_operator that
        received a history row within 24h of being set
      - stall_rate — count of threads entering stall2 (requires_human flip)
        per day in the window
      - planner_override_rate — fraction of stall2 candidates where the
        planner produced action=execute or a concrete question
        (vs the legacy bare-flag flip)
    """
    out: dict[str, Any] = {
        "threads_advanced_per_beat": 0,
        "next_action_source": {"planner": 0, "agent": 0, "operator": 0},
        "questions_answered_within_24h": 0.0,
        "stall_rate": 0.0,
        "planner_override_rate": 0.0,
        "window_days": window_days,
    }
    try:
        with get_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            # next_action_source breakdown — by changed_by on latest plan-kind
            # history row per task.
            cur.execute(
                """SELECT COALESCE(h.changed_by, 'agent') AS source, COUNT(*) AS n
                   FROM crm_tasks t
                   LEFT JOIN LATERAL (
                       SELECT changed_by
                       FROM crm_task_history
                       WHERE task_id = t.id
                         AND (metadata->>'kind') = 'plan'
                       ORDER BY created_at DESC
                       LIMIT 1
                   ) h ON TRUE
                   WHERE t.tenant_id = %s
                     AND t.deleted_at IS NULL
                     AND t.next_action IS NOT NULL
                     AND t.updated_at >= NOW() - (%s || ' days')::interval
                   GROUP BY 1""",
                (tenant_id, str(window_days)),
            )
            for row in cur.fetchall():
                src = row["source"] or "agent"
                if src not in out["next_action_source"]:
                    out["next_action_source"][src] = 0
                out["next_action_source"][src] = int(row["n"])

            # questions_answered_within_24h — count of questions set in the
            # window that moved off requires_human within 24h.
            cur.execute(
                """SELECT
                       COUNT(*) FILTER (WHERE question_for_operator IS NOT NULL) AS asked,
                       COUNT(*) FILTER (
                           WHERE question_for_operator IS NOT NULL
                             AND requires_human = FALSE
                             AND updated_at <= created_at + INTERVAL '24 hours'
                       ) AS answered
                   FROM crm_tasks
                   WHERE tenant_id = %s
                     AND deleted_at IS NULL
                     AND updated_at >= NOW() - (%s || ' days')::interval""",
                (tenant_id, str(window_days)),
            )
            row = cur.fetchone() or {}
            asked = int(row.get("asked") or 0)
            answered = int(row.get("answered") or 0)
            if asked:
                out["questions_answered_within_24h"] = round(answered / asked, 3)

            # stall_rate — threads flipped to REVIEW+requires_human via
            # stall classifier in the window, normalized per day.
            cur.execute(
                """SELECT COUNT(*) AS n
                   FROM crm_task_history
                   WHERE tenant_id = %s
                     AND (metadata->>'kind') = 'ask'
                     AND created_at >= NOW() - (%s || ' days')::interval""",
                (tenant_id, str(window_days)),
            )
            stall_count = int((cur.fetchone() or {}).get("n") or 0)
            out["stall_rate"] = round(stall_count / max(1, window_days), 3) if window_days else 0.0

            # planner_override_rate — fraction of stall2-candidate rows where
            # the planner recorded a plan or question-with-planner-author.
            cur.execute(
                """SELECT
                       COUNT(*) FILTER (WHERE changed_by = 'planner') AS planner,
                       COUNT(*) AS total
                   FROM crm_task_history
                   WHERE tenant_id = %s
                     AND (metadata->>'kind') IN ('plan', 'ask')
                     AND created_at >= NOW() - (%s || ' days')::interval""",
                (tenant_id, str(window_days)),
            )
            row = cur.fetchone() or {}
            total = int(row.get("total") or 0)
            by_planner = int(row.get("planner") or 0)
            if total:
                out["planner_override_rate"] = round(by_planner / total, 3)

            # threads_advanced_per_beat — spawn runs with a parent_task_id
            # recorded in agent_runs.trigger_detail in the window.
            cur.execute(
                """SELECT COUNT(*) AS n
                   FROM agent_runs
                   WHERE tenant_id = %s
                     AND trigger_type = 'sub_agent'
                     AND created_at >= NOW() - (%s || ' days')::interval""",
                (tenant_id, str(window_days)),
            )
            out["threads_advanced_per_beat"] = int((cur.fetchone() or {}).get("n") or 0)
    except Exception as e:
        logger.debug("thread_pool_metrics failed: %s", e)
    return out
