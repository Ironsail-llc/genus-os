"""Agent status routes — health tiers from the Python Agent Engine.

Reads schedule state from the ``agent_schedules`` PostgreSQL table
(written by ``robothor.engine.scheduler``) and status markdown files.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["agents"])

# Cache for agent status (30s TTL)
_cache: dict = {"data": None, "expires": 0.0}
CACHE_TTL = 30

# Agent status markdown files
STATUS_DIR = os.getenv(
    "AGENT_STATUS_DIR",
    str(Path("~/robothor/brain/memory").expanduser()),
)

# Manifest directory for display names
MANIFEST_DIR = os.getenv(
    "AGENT_MANIFEST_DIR",
    str(Path("~/robothor/docs/agents").expanduser()),
)


def _parse_interval_seconds(cron_expr: str) -> int:
    """Estimate interval in seconds from a cron expression."""
    parts = cron_expr.split()
    if len(parts) < 5:
        return 3600
    minute = parts[0]
    hour = parts[1]

    # */N minute interval
    if minute.startswith("*/"):
        try:
            return int(minute[2:]) * 60
        except ValueError:
            pass

    # Every N hours (e.g., */2 or 6-22/2)
    if hour.startswith("*/"):
        try:
            return int(hour[2:]) * 3600
        except ValueError:
            pass
    if "/" in hour:
        # Range with step like "6-22/2"
        try:
            step = int(hour.split("/")[1])
            return step * 3600
        except (ValueError, IndexError):
            pass

    # Specific comma-separated hours (e.g., "10,18")
    if "," in hour:
        hours = [int(h) for h in hour.split(",") if h.isdigit()]
        if len(hours) >= 2:
            diffs = [hours[i + 1] - hours[i] for i in range(len(hours) - 1)]
            avg_gap = sum(diffs) / len(diffs)
            return int(avg_gap * 3600)

    # Range-based hourly (e.g., "6-22" means once per hour within range)
    if "-" in hour and minute.isdigit():
        return 3600

    # Single specific hour (e.g., "30 6 * * *" = daily at 06:30)
    if minute.isdigit() and hour.isdigit():
        return 86400  # daily

    # Single minute value with range hours → hourly
    if minute.isdigit():
        return 3600

    return 3600  # default hourly


def _parse_active_hours(cron_expr: str) -> tuple[int, int] | None:
    """Return (start_hour, end_hour) from a cron expression, or None if always active.

    Examples:
        "0 6-22/2 * * *" → (6, 22)
        "0 8-20 * * *"   → (8, 20)
        "0 8,14,20 * * *" → (8, 20)
        "30 6 * * *"      → (6, 6)
        "0 * * * *"       → None  (every hour)
        "*/10 * * * *"    → None  (every 10 min)
    """
    parts = cron_expr.split()
    if len(parts) < 5:
        return None
    hour_field = parts[1]

    # "*" or "*/N" → always active
    if hour_field == "*" or hour_field.startswith("*/"):
        return None

    # Range with optional step: "6-22" or "6-22/2"
    range_match = re.match(r"^(\d+)-(\d+)(?:/\d+)?$", hour_field)
    if range_match:
        return int(range_match.group(1)), int(range_match.group(2))

    # Comma-separated: "8,14,20"
    if "," in hour_field:
        hours = [int(h) for h in hour_field.split(",") if h.isdigit()]
        if hours:
            return min(hours), max(hours)

    # Single hour: "6"
    if hour_field.isdigit():
        return int(hour_field), int(hour_field)

    return None


# Timezone for active-window checks (all agent crons use America/New_York)
_AGENT_TZ = ZoneInfo(os.getenv("AGENT_TIMEZONE", "America/New_York"))


def _compute_health_tier(
    last_run_ts: float | None,
    interval_s: int,
    consecutive_errors: int,
    enabled: bool,
    run_count: int,
    cron_expr: str = "",
) -> str:
    """Compute health tier: healthy, degraded, failed, sleeping, unknown."""
    if not enabled:
        return "unknown"
    if run_count < 3:
        return "unknown"
    if consecutive_errors >= 2:
        return "failed"

    # Check if agent is outside its active cron window → sleeping
    active_hours = _parse_active_hours(cron_expr)
    if active_hours is not None:
        now_local = datetime.now(_AGENT_TZ)
        current_hour = now_local.hour
        start_h, end_h = active_hours

        if current_hour < start_h or current_hour > end_h:
            return "sleeping"

        # Grace period: if window just opened (first hour), agent hasn't had
        # time to run yet — don't penalize for the overnight gap.
        hours_into_window = current_hour - start_h
        if hours_into_window == 0:
            return "healthy"

    if last_run_ts is None:
        return "unknown"

    age = time.time() - last_run_ts
    yellow_threshold = interval_s * 1.5
    red_threshold = interval_s * 2.0

    if age > red_threshold:
        return "failed"
    if age > yellow_threshold or consecutive_errors >= 1:
        return "degraded"
    return "healthy"


def _read_status_file(name: str) -> str | None:
    """Read a status markdown file, returning its content or None."""
    status_file = Path(STATUS_DIR) / f"{name}-status.md"
    try:
        if status_file.exists():
            content = status_file.read_text(encoding="utf-8").strip()
            return content[:200] if content else None
    except Exception as e:
        logger.debug("Failed to read status file %s: %s", status_file, e)
    return None


def _load_display_names() -> dict[str, str]:
    """Load agent display names from YAML manifests."""
    names: dict[str, str] = {}
    manifest_dir = Path(MANIFEST_DIR)
    if not manifest_dir.is_dir():
        return names
    try:
        import yaml

        for f in manifest_dir.glob("*.yaml"):
            if f.name == "schema.yaml":
                continue
            try:
                data = yaml.safe_load(f.read_text())
                if data and "id" in data and "name" in data:
                    names[data["id"]] = data["name"]
            except Exception:
                pass
    except ImportError:
        logger.debug("PyYAML not available, using agent_id as display name")
    return names


def _build_agent_status() -> dict:
    """Build agent status from the engine's agent_schedules table and status files."""
    agents: list[dict] = []
    summary = {"healthy": 0, "degraded": 0, "failed": 0, "sleeping": 0, "unknown": 0, "total": 0}

    # Load display names from manifests
    display_names = _load_display_names()

    # Read schedules from PostgreSQL
    schedules: list[dict] = []
    try:
        from robothor.engine.tracking import list_schedules

        schedules = list_schedules(enabled_only=False)
    except Exception as e:
        logger.warning("Failed to read agent_schedules: %s", e)

    # Also get recent run counts for health tier (need >= 3 runs)
    run_counts: dict[str, int] = {}
    try:
        from psycopg2.extras import RealDictCursor

        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute(
                "SELECT agent_id, COUNT(*) as cnt "
                "FROM agent_runs "
                "WHERE created_at > NOW() - INTERVAL '7 days' "
                "GROUP BY agent_id"
            )
            for row in cur.fetchall():
                run_counts[row["agent_id"]] = row["cnt"]
    except Exception as e:
        logger.debug("Failed to get run counts: %s", e)

    for schedule in schedules:
        agent_id = schedule["agent_id"]
        cron_expr = schedule.get("cron_expr", "")
        enabled = schedule.get("enabled", True)

        # Skip the interactive "main" agent (no cron schedule)
        if not cron_expr:
            continue

        interval_s = _parse_interval_seconds(cron_expr)

        # Get last run timestamp
        last_run_at = schedule.get("last_run_at")
        last_run_ts: float | None = None
        last_run_str: str | None = None
        if last_run_at:
            if isinstance(last_run_at, datetime):
                last_run_ts = last_run_at.timestamp()
                last_run_str = last_run_at.astimezone(UTC).isoformat()
            else:
                # Already a string
                last_run_str = str(last_run_at)

        last_duration = schedule.get("last_duration_ms")
        consecutive_errors = schedule.get("consecutive_errors", 0) or 0
        run_count = run_counts.get(agent_id, 10)  # default high if not found

        tier = _compute_health_tier(
            last_run_ts, interval_s, consecutive_errors, enabled, run_count, cron_expr
        )
        summary[tier] = summary.get(tier, 0) + 1
        summary["total"] += 1

        # Get display name from manifest, fallback to title-case agent_id
        display_name = display_names.get(agent_id, agent_id.replace("-", " ").title())

        # Read status file
        status_summary = _read_status_file(agent_id)

        agents.append(
            {
                "name": display_name,
                "agentId": agent_id,
                "schedule": cron_expr,
                "lastRun": last_run_str,
                "lastDuration": last_duration,
                "lastStatus": schedule.get("last_status"),
                "status": tier,
                "statusSummary": status_summary,
                "errorCount": consecutive_errors,
                "enabled": enabled,
                "model": schedule.get("model_primary"),
            }
        )

    # ── Merge per-agent RPG scores ──────────────────────────────────────────
    rpg_scores: dict[str, dict] = {}
    try:
        from robothor.db.connection import get_connection
        from robothor.engine.buddy import level_name

        cols = [
            "agent_id",
            "debugging_score",
            "patience_score",
            "chaos_score",
            "wisdom_score",
            "reliability_score",
            "overall_score",
            "level",
            "total_xp",
            "last_benchmark_score",
            "stat_date",
        ]
        with get_connection() as conn:
            cur = conn.cursor()
            # Use DISTINCT ON to get the most recent row per agent (today or yesterday).
            # This avoids a gap between midnight and the next refresh_daily().
            cur.execute(
                """
                SELECT DISTINCT ON (agent_id)
                       agent_id, debugging_score, patience_score, chaos_score,
                       wisdom_score, reliability_score, overall_score,
                       level, total_xp, last_benchmark_score, stat_date
                FROM agent_buddy_stats
                WHERE stat_date >= CURRENT_DATE - INTERVAL '1 day'
                ORDER BY agent_id, stat_date DESC
                """
            )
            for row in cur.fetchall():
                r = dict(zip(cols, row, strict=False))
                lvl = r["level"] or 1
                is_stale = str(r["stat_date"]) != str(datetime.now(UTC).date())
                rpg_scores[r["agent_id"]] = {
                    "overall": r["overall_score"],
                    "level": lvl,
                    "levelName": level_name(lvl),
                    "totalXp": r["total_xp"] or 0,
                    "scores": {
                        "debugging": r["debugging_score"],
                        "patience": r["patience_score"],
                        "chaos": r["chaos_score"],
                        "wisdom": r["wisdom_score"],
                        "reliability": r["reliability_score"],
                    },
                    "benchmarkScore": float(r["last_benchmark_score"])
                    if r["last_benchmark_score"] is not None
                    else None,
                    "stale": is_stale,
                }
    except Exception as e:
        logger.debug("Failed to load per-agent RPG scores: %s", e)

    # Assign ranks by overall score and merge into agent dicts
    ranked_ids = sorted(rpg_scores.keys(), key=lambda k: rpg_scores[k]["overall"], reverse=True)
    for rank, aid in enumerate(ranked_ids, 1):
        rpg_scores[aid]["rank"] = rank

    for agent in agents:
        rpg = rpg_scores.get(agent["agentId"])
        if rpg:
            agent["rpg"] = rpg

    return {"agents": agents, "summary": summary}


@router.get("/agents/status")
async def api_agent_status():
    """Get status of all agent cron jobs with health tiers and RPG scores."""
    now = time.time()
    if _cache["data"] and now < _cache["expires"]:
        return _cache["data"]

    result = _build_agent_status()
    _cache["data"] = result
    _cache["expires"] = now + CACHE_TTL
    return result
