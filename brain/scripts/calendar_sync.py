#!/usr/bin/env python3
"""
Calendar Sync - System Cron Script
Fetches calendar events and writes to calendar-log.json with null notifier fields.
Heartbeat agent processes and fills in the fields.
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/philip/robothor/brain/memory_system")
import event_bus

LOG_PATH = Path("/home/philip/robothor/brain/memory/calendar-log.json")
GOG_PASSWORD = os.environ["GOG_KEYRING_PASSWORD"]
ACCOUNT = "philip@ironsail.ai"


def run_gog(args: list[str]) -> str:
    """Run gog command and return output."""
    env = os.environ.copy()
    env["GOG_KEYRING_PASSWORD"] = GOG_PASSWORD
    result = subprocess.run(["gog"] + args, capture_output=True, text=True, env=env)
    return result.stdout


def load_log() -> dict:
    """Load existing log or create new one."""
    if LOG_PATH.exists():
        with open(LOG_PATH) as f:
            return json.load(f)
    return {"lastCheckedAt": None, "meetings": [], "changes": []}


def save_log(log: dict):
    """Save log to file."""
    with open(LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)


def fetch_calendar_events() -> list[dict]:
    """Fetch upcoming calendar events."""
    output = run_gog(["calendar", "events", ACCOUNT, "--json"])

    if not output.strip():
        return []

    try:
        data = json.loads(output)
        return data.get("events", []) if isinstance(data, dict) else data
    except json.JSONDecodeError:
        # Try parsing line by line for non-JSON output
        return []


def compute_epoch(dt_string: str) -> int | None:
    """Convert an ISO datetime string to Unix epoch seconds."""
    if not dt_string or "T" not in dt_string:
        return None
    try:
        return int(datetime.fromisoformat(dt_string).timestamp())
    except (ValueError, TypeError):
        return None


def compute_local_display(dt_string: str, tz_name: str = "America/New_York") -> str | None:
    """Convert an ISO datetime string to a local display time like '10:45 AM'."""
    if not dt_string or "T" not in dt_string:
        return None
    try:
        dt = datetime.fromisoformat(dt_string).astimezone(ZoneInfo(tz_name))
        return dt.strftime("%-I:%M %p")
    except (ValueError, TypeError):
        return None


def create_meeting_entry(event: dict, existing: dict = None) -> dict:
    """Create or update meeting entry with null notifier fields."""
    start_str = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
    end_str = event.get("end", {}).get("dateTime") or event.get("end", {}).get("date")

    # Preserve existing notifier values if updating
    if existing:
        return {
            **existing,
            # Update event details
            "title": event.get("summary", ""),
            "start": start_str,
            "startEpoch": compute_epoch(start_str),
            "startLocal": compute_local_display(start_str),
            "end": end_str,
            "endLocal": compute_local_display(end_str),
            "location": event.get("location"),
            "description": event.get("description"),
            "attendees": [a.get("email") for a in event.get("attendees", [])],
            "conferenceUrl": event.get("hangoutLink"),
            "updatedAt": datetime.now().isoformat(),
        }

    # New entry with all null notifiers
    return {
        # Basic info (from system cron)
        "id": event.get("id"),
        "title": event.get("summary", ""),
        "start": start_str,
        "startEpoch": compute_epoch(start_str),
        "startLocal": compute_local_display(start_str),
        "end": end_str,
        "endLocal": compute_local_display(end_str),
        "location": event.get("location"),
        "description": event.get("description"),
        "attendees": [a.get("email") for a in event.get("attendees", [])],
        "conferenceUrl": event.get("hangoutLink"),
        "fetchedAt": datetime.now().isoformat(),
        # Stage 1: Categorization (heartbeat fills these)
        "categorizedAt": None,
        "importance": None,  # low/medium/high/critical
        "category": None,  # standup/meeting/external/personal
        # Stage 2: Notification (heartbeat fills these)
        "notifyAt": None,  # when to alert (30 min before, etc)
        "notifiedAt": None,  # when alert was sent
        # Stage 3: Review (heartbeat fills these)
        "pendingReviewAt": None,
        "reviewedAt": None,
        # Change tracking
        "changeDetectedAt": None,
        "changeType": None,  # new/rescheduled/cancelled
        "previousStart": None,
        "previousEnd": None,
    }


def main():
    print(f"[{datetime.now().isoformat()}] Calendar sync starting...")

    log = load_log()
    existing_meetings = {m["id"]: m for m in log.get("meetings", [])}

    events = fetch_calendar_events()
    print(f"Found {len(events)} calendar events")

    updated_meetings = []
    changes = log.get("changes", [])

    for event in events:
        event_id = event.get("id")
        if not event_id:
            continue

        existing = existing_meetings.get(event_id)

        if existing:
            # Check for changes
            new_start = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
            new_end = event.get("end", {}).get("dateTime") or event.get("end", {}).get("date")
            new_title = event.get("summary", "")
            new_attendees = sorted(a.get("email", "") for a in event.get("attendees", []))
            old_attendees = sorted(existing.get("attendees", []))

            if existing.get("start") != new_start:
                change = {
                    "timestamp": datetime.now().isoformat(),
                    "eventId": event_id,
                    "type": "rescheduled",
                    "title": new_title,
                    "oldStart": existing.get("start"),
                    "newStart": new_start,
                    "reviewedAt": None,
                }
                changes.append(change)
                event_bus.publish(
                    "calendar", "calendar.rescheduled", change, source="calendar_sync"
                )
                print(f"  Change detected: {new_title} rescheduled")
            elif (
                existing.get("end") != new_end
                or existing.get("title") != new_title
                or old_attendees != new_attendees
            ):
                change = {
                    "timestamp": datetime.now().isoformat(),
                    "eventId": event_id,
                    "type": "modified",
                    "title": new_title,
                    "details": "attendees/duration/title changed",
                    "reviewedAt": None,
                }
                changes.append(change)
                event_bus.publish("calendar", "calendar.modified", change, source="calendar_sync")
                print(f"  Change detected: {new_title} modified")
        else:
            # New event
            change = {
                "timestamp": datetime.now().isoformat(),
                "eventId": event_id,
                "type": "new",
                "title": event.get("summary"),
                "start": event.get("start", {}).get("dateTime")
                or event.get("start", {}).get("date"),
                "reviewedAt": None,
            }
            changes.append(change)
            event_bus.publish("calendar", "calendar.new", change, source="calendar_sync")
            print(f"  New event: {event.get('summary')}")

        entry = create_meeting_entry(event, existing)
        updated_meetings.append(entry)

    log["meetings"] = updated_meetings
    log["changes"] = changes[-100:]  # Keep last 100 changes
    log["lastCheckedAt"] = datetime.now().isoformat()
    save_log(log)

    print(f"[{datetime.now().isoformat()}] Done. {len(updated_meetings)} meetings tracked.")


if __name__ == "__main__":
    main()
