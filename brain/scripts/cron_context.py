#!/usr/bin/env python3
"""
Shared context loader for all crons.

Every cron reads all relevant logs (for context) but writes only to its own log.
This module provides a unified way to load that context.

Usage:
    from cron_context import CronContext

    ctx = CronContext()
    print(ctx.calendar.meetings)
    print(ctx.emails.recent)
    print(ctx.tasks.pending)
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# === Paths ===
MEMORY_DIR = Path("/home/philip/robothor/brain/memory")
EMAIL_LOG = MEMORY_DIR / "email-log.json"
CALENDAR_LOG = MEMORY_DIR / "calendar-log.json"
JIRA_LOG = MEMORY_DIR / "jira-log.json"
TASKS_FILE = MEMORY_DIR / "tasks.json"
CONTACTS_FILE = MEMORY_DIR / "contacts.json"
SECURITY_LOG = MEMORY_DIR / "security-log.json"


def load_json(path: Path, default: Any = None) -> Any:
    """Load JSON file with default fallback."""
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except json.JSONDecodeError:
            return default
    return default


def save_json(path: Path, data: Any):
    """Save data to JSON file atomically (write to tmp, then rename)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def parse_datetime(dt_str: str | None, naive: bool = True) -> datetime | None:
    """Parse ISO datetime string.

    Args:
        dt_str: ISO format datetime string
        naive: If True, strip timezone info for easier comparison
    """
    if not dt_str:
        return None
    try:
        dt = None
        # Handle various formats
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ]:
            try:
                dt = datetime.strptime(dt_str, fmt)
                break
            except ValueError:
                continue

        if dt is None:
            # Try fromisoformat as fallback
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))

        # Strip timezone if naive requested
        if naive and dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)

        return dt
    except:
        return None


@dataclass
class CalendarContext:
    """Calendar log context."""

    raw: dict = field(default_factory=dict)
    meetings: list[dict] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    last_checked: datetime | None = None

    @classmethod
    def load(cls, days_back: int = 7, days_forward: int = 14) -> "CalendarContext":
        """Load calendar with time window filter."""
        data = load_json(CALENDAR_LOG, {"meetings": [], "changes": []})

        now = datetime.now()
        cutoff_back = now - timedelta(days=days_back)
        cutoff_forward = now + timedelta(days=days_forward)

        # Filter meetings to relevant window
        meetings = []
        for m in data.get("meetings", []):
            start = parse_datetime(m.get("start"))
            if start and cutoff_back <= start <= cutoff_forward:
                meetings.append(m)

        # Filter changes to recent
        changes = []
        for c in data.get("changes", []):
            detected = parse_datetime(c.get("detectedAt"))
            if detected and detected >= cutoff_back:
                changes.append(c)

        return cls(
            raw=data,
            meetings=meetings,
            changes=changes,
            last_checked=parse_datetime(data.get("lastCheckedAt")),
        )

    @property
    def upcoming(self) -> list[dict]:
        """Meetings in next 24 hours."""
        now = datetime.now()
        tomorrow = now + timedelta(days=1)
        return [
            m
            for m in self.meetings
            if parse_datetime(m.get("start")) and now <= parse_datetime(m.get("start")) <= tomorrow
        ]

    @property
    def unreviewed_changes(self) -> list[dict]:
        """Changes not yet reviewed by Heartbeat."""
        return [c for c in self.changes if not c.get("reviewedAt")]


@dataclass
class EmailContext:
    """Email log context."""

    raw: dict = field(default_factory=dict)
    entries: dict[str, dict] = field(default_factory=dict)
    last_processed: datetime | None = None

    @classmethod
    def load(cls, days_back: int = 3) -> "EmailContext":
        """Load recent emails."""
        data = load_json(EMAIL_LOG, {"entries": {}})

        cutoff = datetime.now() - timedelta(days=days_back)

        # Filter to recent entries
        entries = {}
        for id_, entry in data.get("entries", {}).items():
            processed = parse_datetime(entry.get("processedAt"))
            if processed and processed >= cutoff:
                entries[id_] = entry

        return cls(
            raw=data, entries=entries, last_processed=parse_datetime(data.get("lastProcessedAt"))
        )

    @property
    def unsurfaced(self) -> list[dict]:
        """Emails not yet surfaced to Philip."""
        return [e for e in self.entries.values() if not e.get("surfacedAt")]

    @property
    def needs_response(self) -> list[dict]:
        """Emails that need a response."""
        return [
            e for e in self.entries.values() if e.get("needsResponse") and not e.get("respondedAt")
        ]

    @property
    def by_urgency(self) -> dict[str, list[dict]]:
        """Group emails by urgency."""
        result = {"critical": [], "high": [], "medium": [], "low": []}
        for e in self.entries.values():
            urgency = e.get("urgency", "low")
            if urgency in result:
                result[urgency].append(e)
        return result


@dataclass
class JiraContext:
    """Jira log context."""

    raw: dict = field(default_factory=dict)
    active_tickets: dict[str, dict] = field(default_factory=dict)
    pending_actions: list[dict] = field(default_factory=list)
    last_sync: datetime | None = None
    last_status: str | None = None

    @classmethod
    def load(cls) -> "JiraContext":
        """Load Jira context."""
        data = load_json(
            JIRA_LOG,
            {"activeTickets": {}, "pendingActions": [], "lastSyncAt": None, "lastSyncStatus": None},
        )

        return cls(
            raw=data,
            active_tickets=data.get("activeTickets", {}),
            pending_actions=data.get("pendingActions", []),
            last_sync=parse_datetime(data.get("lastSyncAt")),
            last_status=data.get("lastSyncStatus"),
        )

    @property
    def unsurfaced_actions(self) -> list[dict]:
        """Pending actions not yet surfaced."""
        return [a for a in self.pending_actions if not a.get("surfacedAt")]


@dataclass
class TasksContext:
    """Tasks context."""

    raw: dict = field(default_factory=dict)
    tasks: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, active_only: bool = True) -> "TasksContext":
        """Load tasks."""
        data = load_json(TASKS_FILE, {"tasks": []})

        tasks = data.get("tasks", [])
        if active_only:
            tasks = [t for t in tasks if t.get("status") != "completed"]

        return cls(raw=data, tasks=tasks)

    @property
    def pending(self) -> list[dict]:
        """Pending tasks."""
        return [t for t in self.tasks if t.get("status") == "pending"]

    @property
    def overdue(self) -> list[dict]:
        """Overdue tasks."""
        now = datetime.now()
        result = []
        for t in self.tasks:
            if t.get("status") == "completed":
                continue
            due = parse_datetime(t.get("dueAt"))
            if due and due < now:
                result.append(t)
        return result

    @property
    def due_today(self) -> list[dict]:
        """Tasks due today."""
        now = datetime.now()
        today_end = now.replace(hour=23, minute=59, second=59)
        result = []
        for t in self.tasks:
            if t.get("status") == "completed":
                continue
            due = parse_datetime(t.get("dueAt"))
            if due and now <= due <= today_end:
                result.append(t)
        return result

    @property
    def from_jira(self) -> list[dict]:
        """Tasks from Jira."""
        return [t for t in self.tasks if t.get("source", "").startswith("jira:")]


@dataclass
class ContactsContext:
    """Contacts context."""

    raw: dict = field(default_factory=dict)
    contacts: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "ContactsContext":
        """Load contacts."""
        data = load_json(CONTACTS_FILE, {"contacts": {}})
        return cls(raw=data, contacts=data.get("contacts", {}))

    def find_by_email(self, email: str) -> dict | None:
        """Find contact by email."""
        email_lower = email.lower()
        for contact in self.contacts.values():
            if contact.get("email", "").lower() == email_lower:
                return contact
        return None

    def find_by_name(self, name: str) -> dict | None:
        """Find contact by name (fuzzy)."""
        name_lower = name.lower()
        for contact in self.contacts.values():
            if name_lower in contact.get("name", "").lower():
                return contact
        return None


@dataclass
class CronContext:
    """
    Full context for crons.

    Every cron should instantiate this to get context from all logs,
    then do its focused work and update only its own log.
    """

    calendar: CalendarContext = field(default_factory=CalendarContext)
    emails: EmailContext = field(default_factory=EmailContext)
    jira: JiraContext = field(default_factory=JiraContext)
    tasks: TasksContext = field(default_factory=TasksContext)
    contacts: ContactsContext = field(default_factory=ContactsContext)
    loaded_at: datetime = field(default_factory=datetime.now)

    @classmethod
    def load(
        cls,
        calendar_days_back: int = 7,
        calendar_days_forward: int = 14,
        email_days_back: int = 3,
        tasks_active_only: bool = True,
    ) -> "CronContext":
        """Load full context from all logs."""
        return cls(
            calendar=CalendarContext.load(calendar_days_back, calendar_days_forward),
            emails=EmailContext.load(email_days_back),
            jira=JiraContext.load(),
            tasks=TasksContext.load(tasks_active_only),
            contacts=ContactsContext.load(),
            loaded_at=datetime.now(),
        )

    def summary(self) -> dict:
        """Quick summary of context state."""
        return {
            "loaded_at": self.loaded_at.isoformat(),
            "calendar": {
                "meetings": len(self.calendar.meetings),
                "upcoming_24h": len(self.calendar.upcoming),
                "unreviewed_changes": len(self.calendar.unreviewed_changes),
            },
            "emails": {
                "recent": len(self.emails.entries),
                "unsurfaced": len(self.emails.unsurfaced),
                "needs_response": len(self.emails.needs_response),
            },
            "jira": {
                "active_tickets": len(self.jira.active_tickets),
                "unsurfaced_actions": len(self.jira.unsurfaced_actions),
            },
            "tasks": {
                "total_active": len(self.tasks.tasks),
                "pending": len(self.tasks.pending),
                "overdue": len(self.tasks.overdue),
                "due_today": len(self.tasks.due_today),
            },
            "contacts": {"total": len(self.contacts.contacts)},
        }


# === Convenience Functions ===


def get_context() -> CronContext:
    """Get full context with defaults."""
    return CronContext.load()


if __name__ == "__main__":
    # Quick test
    ctx = get_context()
    import pprint

    pprint.pprint(ctx.summary())
