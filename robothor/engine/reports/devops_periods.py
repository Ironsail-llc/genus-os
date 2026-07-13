"""ET-aligned weekly windows for the DevOps report pipeline.

The pipeline's cron trigger fires Monday 09:00 America/New_York. Window
math is anchored to that timezone so the report's "last week" matches the
team's calendar, regardless of where the collector process runs.

GitHub stores `merged_at` in UTC, so GitHub windows are returned as UTC
RFC3339. JIRA evaluates bare-date JQL in the user's account timezone, so
JIRA windows are returned as YYYY-MM-DD strings of the ET-Monday date —
the JIRA account TZ must be America/New_York for exact alignment.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _et_monday_boundaries(now: datetime) -> tuple[datetime, datetime]:
    """Return (current_week_start, last_week_start) as ET-aware midnights.

    Uses date arithmetic (not timedelta on aware datetimes) so DST
    transitions don't shift the boundary by an hour.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now_et = now.astimezone(ET)
    current_date = now_et.date() - timedelta(days=now_et.weekday())
    last_date = current_date - timedelta(days=7)
    current_dt = datetime.combine(current_date, datetime.min.time(), tzinfo=ET)
    last_dt = datetime.combine(last_date, datetime.min.time(), tzinfo=ET)
    return current_dt, last_dt


def week_windows_github(now: datetime) -> tuple[str, str, str]:
    """Return (current_week_since, last_week_since, last_week_until) as UTC RFC3339."""
    current, last = _et_monday_boundaries(now)
    current_utc = current.astimezone(UTC).isoformat()
    last_utc = last.astimezone(UTC).isoformat()
    return current_utc, last_utc, current_utc


def week_windows_jira(now: datetime) -> tuple[str, str, str]:
    """Return (current_week_start, last_week_start, last_week_end) as YYYY-MM-DD ET dates."""
    current, last = _et_monday_boundaries(now)
    return (
        current.strftime("%Y-%m-%d"),
        last.strftime("%Y-%m-%d"),
        current.strftime("%Y-%m-%d"),
    )


def period_block(now: datetime) -> dict[str, Any]:
    """Build the period metadata block stamped into collector output JSON.

    Lets downstream steps (analyst, renderer, Telegram) quote the exact
    window the report covers, without re-computing it themselves.

    The `report_label` uses last_week_start — that's the Monday the report
    actually covers (the completed full week). At Monday 9 AM, current_week
    is only 9 hours of data; last_week is the meaningful window.
    """
    current, last = _et_monday_boundaries(now)
    return {
        "tz": "America/New_York",
        "current_week_start_et": current.isoformat(),
        "last_week_start_et": last.isoformat(),
        "last_week_end_et": current.isoformat(),
        "generated_at_et": now.astimezone(ET).isoformat(),
        "report_label": f"Week of {last.strftime('%Y-%m-%d')}",
    }


def period_label(period: Any) -> str:
    """Normalize a report period to a clean human-readable label.

    `data["period"]` may arrive as the `period_block` dict (collectors stamp
    it; the analyst is told to copy it verbatim) or as a pre-formatted string.
    This is the single source of truth so no consumer ever f-strings the raw
    dict and leaks a Python dict-repr into operator-facing output.
    """
    if isinstance(period, dict):
        label = period.get("report_label")
        if label:
            return str(label)
        # Older/aggregate payloads may omit report_label — derive from the
        # Monday the report covers (last_week_start_et is an ISO datetime).
        start = period.get("last_week_start_et")
        if isinstance(start, str) and start:
            return f"Week of {start[:10]}"
        return "this week"
    if isinstance(period, str) and period.strip():
        return period
    return "this week"
