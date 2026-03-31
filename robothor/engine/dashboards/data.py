"""Memory file loaders — read JSON state files from brain/memory/."""

from __future__ import annotations

import json
import logging
from datetime import UTC
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MEMORY_DIR = Path.home() / "robothor" / "brain" / "memory"
LOGS_DIR = Path.home() / "robothor" / "brain" / "memory_system" / "logs"


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception as e:
        logger.warning("Failed to load %s: %s", path, e)
    return None


def get_emails() -> dict[str, Any]:
    data = _load_json(MEMORY_DIR / "email-log.json")
    if not data:
        return {"lastChecked": None, "emails": [], "stats": {"total": 0, "unread": 0, "urgent": 0}}

    entries = list((data.get("entries") or {}).values())
    unread = [e for e in entries if not e.get("reviewedAt")]
    urgent = [e for e in entries if e.get("urgency") in ("high", "critical")]

    return {
        "lastChecked": data.get("lastCheckedAt"),
        "emails": entries[-20:][::-1],
        "stats": {"total": len(entries), "unread": len(unread), "urgent": len(urgent)},
    }


def get_calendar() -> dict[str, Any]:
    data = _load_json(MEMORY_DIR / "calendar-log.json")
    if not data:
        return {"lastChecked": None, "meetings": [], "upcoming": [], "changes": []}

    from datetime import datetime

    now = datetime.now(UTC)
    today = now.strftime("%Y-%m-%d")
    meetings = data.get("meetings") or []

    today_meetings = sorted(
        [m for m in meetings if m.get("start", "").startswith(today)],
        key=lambda m: m.get("start", ""),
    )

    upcoming = []
    for m in meetings:
        start_str = m.get("start")
        if not start_str:
            continue
        try:
            start = datetime.fromisoformat(start_str)
            diff_mins = (start - now).total_seconds() / 60
            if 0 < diff_mins <= 60:
                upcoming.append(m)
        except (ValueError, TypeError):
            pass

    changes = (data.get("changes") or [])[-10:][::-1]
    return {
        "lastChecked": data.get("lastCheckedAt"),
        "meetings": today_meetings,
        "upcoming": upcoming,
        "changes": changes,
    }


def get_tasks() -> dict[str, Any]:
    data = _load_json(MEMORY_DIR / "tasks.json")
    if not data:
        return {
            "tasks": [],
            "pending": [],
            "inProgress": [],
            "completed": [],
            "stats": {"total": 0, "pending": 0, "inProgress": 0},
        }

    tasks = data.get("tasks") or []
    pending = [t for t in tasks if t.get("status") == "pending"]
    in_progress = [t for t in tasks if t.get("status") == "in_progress"]
    completed = [t for t in tasks if t.get("status") == "completed"][-5:]

    return {
        "tasks": tasks,
        "pending": pending,
        "inProgress": in_progress,
        "completed": completed,
        "stats": {"total": len(tasks), "pending": len(pending), "inProgress": len(in_progress)},
    }


def get_jira() -> dict[str, Any]:
    data = _load_json(MEMORY_DIR / "jira-log.json")
    if not data:
        return {"lastSync": None, "tickets": [], "pending": []}

    return {
        "lastSync": data.get("lastSyncAt"),
        "status": data.get("lastSyncStatus"),
        "tickets": list((data.get("activeTickets") or {}).values()),
        "pending": data.get("pendingActions") or [],
        "history": (data.get("syncHistory") or [])[:5],
    }


def get_security() -> dict[str, Any]:
    data = _load_json(MEMORY_DIR / "security-log.json")
    if not data:
        return {"entries": [], "unreviewed": 0}

    entries = data.get("entries") or []
    unreviewed = sum(1 for e in entries if not e.get("reviewedAt"))
    return {"entries": entries[-10:], "unreviewed": unreviewed}


def get_worker_handoff() -> dict[str, Any]:
    data = _load_json(MEMORY_DIR / "worker-handoff.json")
    if not data:
        return {"escalations": {"total": 0, "active": 0}, "lastRunAt": None}

    all_esc = data.get("escalations") or []
    return {
        "escalations": {
            "total": len(all_esc),
            "active": sum(1 for e in all_esc if not e.get("resolvedAt")),
        },
        "lastRunAt": data.get("lastRunAt"),
    }


def get_next_event() -> dict[str, Any] | None:
    data = _load_json(MEMORY_DIR / "calendar-log.json")
    if not data:
        return None

    from datetime import datetime

    now = datetime.now(UTC)
    meetings = data.get("meetings") or []

    upcoming = []
    for m in meetings:
        start_str = m.get("start")
        if not start_str:
            continue
        try:
            start = datetime.fromisoformat(start_str)
            if start > now:
                upcoming.append(m)
        except (ValueError, TypeError):
            pass

    upcoming.sort(key=lambda m: m.get("start", ""))
    if not upcoming:
        return None

    e = upcoming[0]
    return {"title": e.get("title", ""), "start": e.get("start"), "location": e.get("location")}


def get_stats() -> dict[str, int]:
    """Quick summary stats for the homepage."""
    from datetime import datetime

    stats: dict[str, int] = {"tasks": 0, "emails": 0, "contacts": 0, "meetings": 0}

    tasks_data = _load_json(MEMORY_DIR / "tasks.json")
    if tasks_data:
        all_tasks = tasks_data.get("tasks") or []
        stats["tasks"] = sum(1 for t in all_tasks if t.get("status") in ("pending", "in_progress"))

    email_data = _load_json(MEMORY_DIR / "email-log.json")
    if email_data:
        stats["emails"] = len(email_data.get("entries") or {})

    contacts_data = _load_json(MEMORY_DIR / "contacts.json")
    if contacts_data:
        stats["contacts"] = len(contacts_data.get("contacts") or [])

    cal_data = _load_json(MEMORY_DIR / "calendar-log.json")
    if cal_data:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        stats["meetings"] = sum(
            1 for m in (cal_data.get("meetings") or []) if m.get("start", "").startswith(today)
        )

    return stats


def get_cron_status() -> dict[str, Any]:
    """Parse crontab and check log freshness."""
    import subprocess

    system_crons: list[dict[str, str]] = []
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 5)
                if len(parts) >= 6:
                    schedule = " ".join(parts[:5])
                    command = parts[5].split("/")[-1].split()[0]
                    system_crons.append({"schedule": schedule, "command": command})
                else:
                    system_crons.append({"schedule": "?", "command": line[:50]})
    except Exception:
        pass

    return {"systemCrons": system_crons}
