#!/usr/bin/env python3
"""
Cron Health Check — reads agent_runs from PostgreSQL and writes cron-health-status.md.

Runs every 30 min via crontab. Queries the agent engine's tracking tables for
per-agent stats, fleet health, and tool performance. Writes a structured markdown
status file that the heartbeat agent reads.

Replaces the old jobs.json-based check (OpenClaw era).
"""

import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

# Ensure the robothor package is importable
sys.path.insert(0, os.path.expanduser("~/robothor"))

OUTPUT_PATH = Path("/home/philip/robothor/brain/memory/cron-health-status.md")


def _get_connection():
    """Get a database connection via the robothor DAL."""
    from robothor.db.connection import get_connection

    return get_connection()


def get_per_agent_stats(hours: int = 168) -> list[dict]:
    """Per-agent stats over the last N hours (default 7 days)."""
    from psycopg2.extras import RealDictCursor

    with _get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT
                agent_id,
                COUNT(*) as total_runs,
                COUNT(*) FILTER (WHERE status = 'completed') as completed,
                COUNT(*) FILTER (WHERE status = 'failed') as failed,
                COUNT(*) FILTER (WHERE status = 'timeout') as timeouts,
                ROUND(AVG(duration_ms) FILTER (WHERE status = 'completed'))::int as avg_duration_ms,
                ROUND(SUM(total_cost_usd)::numeric, 4) as total_cost_usd,
                MAX(started_at) as last_run_at,
                MAX(completed_at) FILTER (WHERE status = 'completed') as last_success_at
            FROM agent_runs
            WHERE created_at > NOW() - INTERVAL '%s hours'
              AND parent_run_id IS NULL
            GROUP BY agent_id
            ORDER BY agent_id
            """,
            (hours,),
        )
        return [dict(r) for r in cur.fetchall()]


def get_fleet_summary(hours: int = 24) -> dict:
    """Fleet-wide stats for the last N hours."""
    from psycopg2.extras import RealDictCursor

    with _get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT
                COUNT(*) as total_runs,
                COUNT(*) FILTER (WHERE status = 'completed') as completed,
                COUNT(*) FILTER (WHERE status = 'failed') as failed,
                COUNT(*) FILTER (WHERE status = 'timeout') as timeouts,
                ROUND(SUM(total_cost_usd)::numeric, 4) as total_cost_usd,
                ROUND(AVG(duration_ms) FILTER (WHERE status = 'completed'))::int as avg_duration_ms
            FROM agent_runs
            WHERE created_at > NOW() - INTERVAL '%s hours'
              AND parent_run_id IS NULL
            """,
            (hours,),
        )
        row = cur.fetchone()
        return dict(row) if row else {}


def get_tool_health(hours: int = 24) -> dict:
    """Tool health: slowest and most-failing tools."""
    from psycopg2.extras import RealDictCursor

    with _get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # Slowest tools
        cur.execute(
            """
            SELECT tool_name, ROUND(AVG(duration_ms))::int as avg_ms,
                   COUNT(*) as calls
            FROM agent_tool_events
            WHERE created_at > NOW() - INTERVAL '%s hours'
              AND success = TRUE
            GROUP BY tool_name
            HAVING COUNT(*) >= 3
            ORDER BY AVG(duration_ms) DESC
            LIMIT 5
            """,
            (hours,),
        )
        slowest = [dict(r) for r in cur.fetchall()]

        # Most-failing tools
        cur.execute(
            """
            SELECT tool_name,
                   COUNT(*) FILTER (WHERE NOT success) as failures,
                   COUNT(*) as total,
                   ROUND(100.0 * COUNT(*) FILTER (WHERE NOT success) / COUNT(*), 1) as fail_pct
            FROM agent_tool_events
            WHERE created_at > NOW() - INTERVAL '%s hours'
            GROUP BY tool_name
            HAVING COUNT(*) FILTER (WHERE NOT success) > 0
            ORDER BY COUNT(*) FILTER (WHERE NOT success) DESC
            LIMIT 5
            """,
            (hours,),
        )
        failing = [dict(r) for r in cur.fetchall()]

        return {"slowest": slowest, "failing": failing}


def format_duration(ms) -> str:
    """Format duration in ms to human string."""
    if ms is None or ms <= 0:
        return "—"
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.0f}s"


def format_cost(cost) -> str:
    """Format cost in USD."""
    if cost is None or cost <= 0:
        return "$0"
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"


def format_age(dt) -> str:
    """Format a datetime as relative time string."""
    if dt is None:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    age_s = (datetime.now(UTC) - dt).total_seconds()
    if age_s < 60:
        return f"{age_s:.0f}s ago"
    if age_s < 3600:
        return f"{age_s / 60:.0f}m ago"
    if age_s < 86400:
        return f"{age_s / 3600:.1f}h ago"
    return f"{age_s / 86400:.1f}d ago"


def classify_agent(agent: dict) -> str:
    """Classify agent as error, stale, or healthy."""
    total = agent["total_runs"]
    failed = agent["failed"] + agent["timeouts"]
    if total == 0:
        return "stale"
    fail_rate = failed / total
    if fail_rate > 0.5 and failed >= 2:
        return "error"
    # Agent with no successes AND has failed runs is an error
    if agent["last_success_at"] is None and failed > 0:
        return "error"
    return "healthy"


def write_status(agents: list[dict], fleet: dict, tools: dict, output_path: Path | None = None):
    """Write structured markdown status file atomically."""
    if output_path is None:
        output_path = OUTPUT_PATH
    now = datetime.now()
    lines = [
        "# Cron Health Status",
        f"Updated: {now.strftime('%Y-%m-%d %H:%M')} ET",
        "",
    ]

    # Classify agents
    errors = [a for a in agents if classify_agent(a) == "error"]
    stale = [a for a in agents if classify_agent(a) == "stale"]
    healthy = [a for a in agents if classify_agent(a) == "healthy"]

    # Fleet summary
    lines.append("## Fleet Summary (24h)")
    total_runs = fleet.get("total_runs", 0) or 0
    completed = fleet.get("completed", 0) or 0
    failed = fleet.get("failed", 0) or 0
    timeouts = fleet.get("timeouts", 0) or 0
    fail_rate = f"{100 * (failed + timeouts) / total_runs:.0f}%" if total_runs > 0 else "—"
    lines.append(f"- Runs: {total_runs} ({completed} ok, {failed} failed, {timeouts} timeout)")
    lines.append(f"- Failure rate: {fail_rate}")
    lines.append(f"- Cost: {format_cost(fleet.get('total_cost_usd'))}")
    lines.append(f"- Avg duration: {format_duration(fleet.get('avg_duration_ms'))}")
    lines.append("")

    # Agent counts
    if errors:
        lines.append(
            f"**{len(errors)} ERROR**, {len(stale)} stale, {len(healthy)} healthy ({len(agents)} total)"
        )
    elif stale:
        lines.append(f"{len(stale)} STALE, {len(healthy)} healthy ({len(agents)} total)")
    else:
        lines.append(f"All {len(agents)} agents healthy")
    lines.append("")

    # Errors section
    if errors:
        lines.append("## Errors (7d)")
        for a in errors:
            fail_rate_a = (
                f"{100 * (a['failed'] + a['timeouts']) / a['total_runs']:.0f}%"
                if a["total_runs"] > 0
                else "—"
            )
            lines.append(
                f"- **{a['agent_id']}**: {a['failed']} failed, {a['timeouts']} timeout "
                f"({fail_rate_a} fail rate), last: {format_age(a['last_run_at'])}"
            )
        lines.append("")

    # Stale section
    if stale:
        lines.append("## Stale (no runs in 7d)")
        for a in stale:
            lines.append(f"- **{a['agent_id']}**")
        lines.append("")

    # Healthy section
    lines.append("## Healthy Agents (7d)")
    lines.append("| Agent | Runs | Failed | Avg Duration | Cost | Last Run |")
    lines.append("|-------|------|--------|-------------|------|----------|")
    for a in healthy:
        lines.append(
            f"| {a['agent_id']} | {a['total_runs']} | {a['failed']} | "
            f"{format_duration(a['avg_duration_ms'])} | {format_cost(a['total_cost_usd'])} | "
            f"{format_age(a['last_run_at'])} |"
        )
    lines.append("")

    # Tool health
    if tools.get("slowest") or tools.get("failing"):
        lines.append("## Tool Health (24h)")
        if tools["slowest"]:
            lines.append("### Slowest Tools")
            for t in tools["slowest"][:3]:
                lines.append(
                    f"- `{t['tool_name']}`: avg {format_duration(t['avg_ms'])} ({t['calls']} calls)"
                )
        if tools["failing"]:
            lines.append("### Most-Failing Tools")
            for t in tools["failing"][:3]:
                lines.append(
                    f"- `{t['tool_name']}`: {t['failures']}/{t['total']} failed ({t['fail_pct']}%)"
                )
        lines.append("")

    content = "\n".join(lines) + "\n"

    # Atomic write
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=output_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp_path, output_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main():
    now = datetime.now()
    print(f"[{now.isoformat()}] Cron health check starting...")

    try:
        agents = get_per_agent_stats(hours=168)  # 7 days
        fleet = get_fleet_summary(hours=24)
        tools = get_tool_health(hours=24)
    except Exception as e:
        print(f"  ERROR querying database: {e}")
        # Write empty status so heartbeat knows the check failed
        write_status([], {}, {})
        return

    write_status(agents, fleet, tools)

    total = len(agents)
    errors = sum(1 for a in agents if classify_agent(a) == "error")
    stale_count = sum(1 for a in agents if classify_agent(a) == "stale")
    healthy = total - errors - stale_count
    print(f"  Agents: {total} total, {errors} errors, {stale_count} stale, {healthy} healthy")
    print(
        f"  Fleet (24h): {fleet.get('total_runs', 0)} runs, {format_cost(fleet.get('total_cost_usd'))} cost"
    )

    # Optional: publish to event bus
    try:
        from robothor.events.bus import publish

        publish(
            "agent",
            "cron.health_check",
            {
                "total": total,
                "errors": errors,
                "stale": stale_count,
                "healthy": healthy,
            },
            source="cron_health_check",
        )
    except Exception:
        pass

    print(f"[{datetime.now().isoformat()}] Cron health check done.")


if __name__ == "__main__":
    main()
