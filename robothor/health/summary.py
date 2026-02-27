"""
Health Summary — reads PostgreSQL health tables, writes garmin-health.md.

Runs 2x daily (before morning briefing and evening wind-down).
Generates a concise markdown summary that agents parse for health context.

Usage:
    python -m robothor.health.summary
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from robothor.health import dal

OUTPUT_PATH = Path("/home/philip/clawd/memory/garmin-health.md")

STRESS_LABELS = [
    (25, "rest"),
    (50, "low"),
    (75, "medium"),
    (100, "high"),
]


def format_duration(seconds: int | None) -> str:
    """Convert seconds to 'Xh Ym' format."""
    if seconds is None:
        return "N/A"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def stress_label(avg: float | None) -> str:
    """Map average stress to qualitative label."""
    if avg is None:
        return "N/A"
    for threshold, label in STRESS_LABELS:
        if avg <= threshold:
            return label
    return "high"


def generate_summary(now: datetime | None = None) -> str:
    """Generate the health summary markdown from PostgreSQL."""
    if now is None:
        now = datetime.now()

    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_ts = int(midnight.timestamp())
    now_ts = int(now.timestamp())

    sleep = dal.get_sleep(today, yesterday)
    bb = dal.get_body_battery(today_start_ts, now_ts)
    stress = dal.get_stress_avg(today_start_ts, now_ts)
    steps = dal.get_steps(today)
    rhr = dal.get_resting_hr(today, yesterday)
    hrv = dal.get_hrv_latest(today_start_ts, now_ts)

    generated = now.strftime("%Y-%m-%d %H:%M EST")
    lines = [
        "# Health Status",
        f"Generated: {generated}",
        "",
        "## Last Night",
    ]

    if sleep:
        lines.append(
            f"Sleep: {format_duration(sleep['total'])} "
            f"(score {sleep['score']}, {sleep['quality']})"
        )
        lines.append(
            f"Deep {format_duration(sleep['deep'])} | "
            f"Light {format_duration(sleep['light'])} | "
            f"REM {format_duration(sleep['rem'])}"
        )
    else:
        lines.append("Sleep: N/A")

    lines.append(f"HRV: {hrv} ms" if hrv is not None else "HRV: N/A")

    lines.extend(["", "## Today"])

    if bb["current"] is not None:
        peak_str = f" (peak {bb['peak']})" if bb["peak"] is not None else ""
        lines.append(f"Body Battery: {bb['current']}{peak_str}")
    else:
        lines.append("Body Battery: N/A")

    lines.append(f"Resting HR: {rhr} bpm" if rhr is not None else "Resting HR: N/A")

    stress_label_val = stress_label(stress["avg"])
    if stress["avg"] is not None:
        lines.append(
            f"Stress: avg {stress['avg']} (peak {stress['peak']}) "
            f"— {stress_label_val}"
        )
    else:
        lines.append("Stress: N/A")

    if steps:
        pct_str = f" ({steps['pct']}%)" if steps.get("pct") is not None else ""
        lines.append(
            f"Steps: {steps['total']:,} / {steps['goal']:,}{pct_str}"
        )
    else:
        lines.append("Steps: N/A")

    return "\n".join(lines) + "\n"


def write_summary(content: str, output_path: Path = OUTPUT_PATH) -> None:
    """Atomically write summary to output file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=".health-summary-",
        suffix=".tmp",
    )
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        os.replace(tmp, output_path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main() -> None:
    content = generate_summary()
    write_summary(content)
    print(f"Health summary written to {OUTPUT_PATH}")
    print(content)


if __name__ == "__main__":
    main()
