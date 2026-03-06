#!/usr/bin/env python3
"""
Email Sync - System Cron Script
Fetches unread email IDs and writes minimal entries to email-log.json.
Heartbeat agent reads full content on-demand via gog gmail get.

Includes:
- Metadata preservation from gog search results (from, subject, date, labels)
- Content validation guard (resets entries categorized with null metadata)
- Backfill for existing broken entries
- CRM logging via Bridge /log-interaction
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, "/home/philip/robothor/brain/memory_system")
import event_bus

MEMORY_DIR = Path("/home/philip/robothor/brain/memory")
LOG_PATH = MEMORY_DIR / "email-log.json"
CALENDAR_LOG_PATH = MEMORY_DIR / "calendar-log.json"
JIRA_LOG_PATH = MEMORY_DIR / "jira-log.json"
TRIAGE_INBOX_PATH = MEMORY_DIR / "triage-inbox.json"
HANDOFF_PATH = MEMORY_DIR / "worker-handoff.json"
REPLY_COOLDOWN_SECONDS = 300  # 5 minutes
LOCK_PATH = MEMORY_DIR / ".email-log.lock"
GOG_PASSWORD = os.environ["GOG_KEYRING_PASSWORD"]
ACCOUNT = "robothor@ironsail.ai"


def _get_bridge_url():
    try:
        from memory_system.service_registry import get_service_url

        url = get_service_url("bridge")
        if url:
            return url
    except ImportError:
        pass
    return "http://localhost:9100"


BRIDGE_URL = _get_bridge_url()


def run_gog(args: list[str]) -> str:
    """Run gog command and return output."""
    env = os.environ.copy()
    env["GOG_KEYRING_PASSWORD"] = GOG_PASSWORD
    result = subprocess.run(["gog"] + args, capture_output=True, text=True, env=env)
    return result.stdout


def load_log(path: Path = None) -> dict:
    """Load existing log or create new one. Survives corrupted JSON."""
    path = path or LOG_PATH
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            # Corrupted file — back it up and start fresh
            backup = path.with_suffix(f".corrupt.{datetime.now().strftime('%Y%m%d%H%M%S')}.json")
            path.rename(backup)
            print(f"  WARNING: Corrupted {path.name}, backed up to {backup.name}")
    return {"lastCheckedAt": None, "entries": {}}


def save_log(log: dict, path: Path = None):
    """Atomic save — writes to temp file then renames. Prevents corruption on crash."""
    path = path or LOG_PATH
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(log, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def fetch_unread_emails() -> list[dict]:
    """Fetch unread emails from Gmail (minimal data)."""
    output = run_gog(
        ["gmail", "search", "is:unread", "--account", ACCOUNT, "--max", "20", "--json"]
    )

    if not output.strip():
        return []

    try:
        data = json.loads(output)
        if data is None:
            return []
        # gog returns "threads" not "messages"
        if isinstance(data, dict):
            return data.get("threads", data.get("messages", []))
        if isinstance(data, list):
            return data
        return []
    except json.JSONDecodeError:
        return []


def create_minimal_entry(email: dict) -> dict:
    """Create minimal log entry - preserving metadata gog already returns."""
    return {
        # Identifiers
        "id": email.get("id"),
        "threadId": email.get("threadId"),
        "fetchedAt": datetime.now().isoformat(),
        # Read state - null means full thread content not yet read
        "readAt": None,
        # Metadata from gog search results (available at sync time)
        "from": email.get("from"),
        "subject": email.get("subject"),
        "date": email.get("date"),
        "labels": email.get("labels", []),
        # snippet requires full thread read, stays null
        "snippet": None,
        # Stage 1: Categorization (after reading)
        "categorizedAt": None,
        "urgency": None,
        "category": None,
        # Stage 2: Action
        "actionRequired": None,
        "actionCompletedAt": None,
        # Stage 3: Review
        "pendingReviewAt": None,
        "reviewedAt": None,
    }


def find_thread_parent(log: dict, thread_id: str, exclude_id: str) -> str | None:
    """Find the earliest existing entry in a thread (the parent)."""
    if not thread_id:
        return None
    earliest_id = None
    earliest_time = None
    for eid, entry in log.get("entries", {}).items():
        if eid == exclude_id:
            continue
        # Match by threadId, or by id == threadId (root message)
        entry_tid = entry.get("threadId") or eid
        if entry_tid == thread_id or eid == thread_id:
            fetched = entry.get("fetchedAt", "")
            if earliest_time is None or fetched < earliest_time:
                earliest_time = fetched
                earliest_id = eid
    return earliest_id


def should_skip_reset(entry: dict) -> bool:
    """Check if a thread was replied to within the cooldown window.

    Prevents rapid re-processing when a user replies immediately after Robothor,
    which would cause the thread to be reset and re-routed before the reply is
    fully processed.
    """
    completed = entry.get("actionCompletedAt")
    if not completed:
        return False
    try:
        completed_dt = datetime.fromisoformat(completed)
        now = datetime.now(UTC)
        if completed_dt.tzinfo is None:
            completed_dt = completed_dt.replace(tzinfo=UTC)
        return (now - completed_dt).total_seconds() < REPLY_COOLDOWN_SECONDS
    except (ValueError, TypeError):
        return False


def reset_entry_for_reprocessing(entry: dict, reply_id: str):
    """Null out processing fields so the worker re-picks-up this thread."""
    now = datetime.now().isoformat()
    entry["readAt"] = None
    entry["categorizedAt"] = None
    entry["urgency"] = None
    entry["category"] = None
    entry["actionRequired"] = None
    entry["actionCompletedAt"] = None
    entry["pendingReviewAt"] = None
    entry["reviewedAt"] = None
    # Breadcrumb so the worker knows why this was reset
    entry["resetByReplyId"] = reply_id
    entry["resetAt"] = now


def validate_entries(log: dict) -> int:
    """Reset entries that were categorized but are incomplete.

    Catches two cases:
    1. Entries with null from AND subject (no real content)
    2. Entries with categorizedAt set but missing urgency (incomplete classification)

    Resetting forces re-processing on next worker run.

    Returns count of entries reset.
    """
    reset_count = 0
    for eid, entry in log.get("entries", {}).items():
        if entry.get("categorizedAt") and (
            (entry.get("from") is None and entry.get("subject") is None)
            or entry.get("urgency") is None
        ):
            entry["categorizedAt"] = None
            entry["urgency"] = None
            entry["category"] = None
            entry["actionRequired"] = None
            entry["actionCompletedAt"] = None
            entry["pendingReviewAt"] = None
            entry["reviewedAt"] = None
            entry["readAt"] = None
            entry["summary"] = None
            entry["resetByValidation"] = True
            entry["resetAt"] = datetime.now().isoformat()
            reset_count += 1
    return reset_count


def backfill_null_metadata(log: dict) -> int:
    """Re-fetch metadata for entries with null from AND null subject.

    Calls gog gmail search to get metadata, matches by ID, and populates
    from/subject/date/labels. Resets processing fields so the worker
    re-processes with real content.

    Returns count of entries backfilled.
    """
    # Find entries needing backfill
    needs_backfill = [
        eid
        for eid, entry in log.get("entries", {}).items()
        if entry.get("from") is None and entry.get("subject") is None
    ]

    if not needs_backfill:
        return 0

    # Fetch recent emails with metadata
    output = run_gog(
        ["gmail", "search", "in:anywhere", "--account", ACCOUNT, "--max", "50", "--json"]
    )

    if not output.strip():
        return 0

    try:
        data = json.loads(output)
        emails = data.get("threads", data.get("messages", [])) if isinstance(data, dict) else data
    except json.JSONDecodeError:
        return 0

    # Build lookup by ID
    email_by_id = {e.get("id"): e for e in emails if e.get("id")}

    backfill_count = 0
    for eid in needs_backfill:
        if eid in email_by_id:
            email = email_by_id[eid]
            entry = log["entries"][eid]
            entry["from"] = email.get("from")
            entry["subject"] = email.get("subject")
            entry["date"] = email.get("date")
            entry["labels"] = email.get("labels", [])
            # Reset processing fields for re-processing
            entry["categorizedAt"] = None
            entry["urgency"] = None
            entry["category"] = None
            entry["actionRequired"] = None
            entry["actionCompletedAt"] = None
            entry["pendingReviewAt"] = None
            entry["reviewedAt"] = None
            entry["readAt"] = None
            entry["summary"] = None
            entry["backfilledAt"] = datetime.now().isoformat()
            backfill_count += 1

    return backfill_count


def parse_sender(from_field: str | None) -> tuple[str | None, str | None]:
    """Parse sender name and email from gog's from field.

    Handles formats like:
    - '"Name" <email@example.com>'
    - 'Name <email@example.com>'
    - 'email@example.com'

    Returns (name, email) tuple. Either may be None.
    """
    if not from_field:
        return None, None

    # Try "Name" <email> or Name <email> or bare <email>
    match = re.match(r'"?([^"<]*?)"?\s*<([^>]+)>', from_field)
    if match:
        name = match.group(1).strip()
        email = match.group(2).strip()
        if not name:
            name = email.split("@")[0]
        return name, email

    # Try bare email
    match = re.match(r"([^@\s]+@[^@\s]+)", from_field)
    if match:
        email = match.group(1)
        name = email.split("@")[0]
        return name, email

    return from_field, None


def log_email_to_crm(entry: dict) -> bool:
    """Log a new email to CRM via Bridge /log-interaction.

    Creates Chatwoot conversation records at sync time, providing
    redundancy that doesn't depend on the triage worker.

    Returns True if logged successfully.
    """
    sender = entry.get("from")
    if not sender:
        return False

    name, email_addr = parse_sender(sender)
    if not name:
        return False

    subject = entry.get("subject") or "(no subject)"
    content_summary = f"{sender}: '{subject}'"

    try:
        resp = requests.post(
            f"{BRIDGE_URL}/log-interaction",
            json={
                "contact_name": name,
                "channel": "email",
                "direction": "incoming",
                "content_summary": content_summary,
                "channel_identifier": email_addr or sender,
            },
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"    CRM log failed: {e}")
        return False


def _load_json(path: Path) -> dict:
    """Load JSON file, returning empty dict on any error."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _get_pending_emails(email_log: dict) -> list[dict]:
    """Extract uncategorized or follow-up-due emails from the email log."""
    entries = email_log.get("entries", {})
    now = datetime.now(timezone.utc).isoformat()
    pending = []
    for eid, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        if not entry.get("from"):
            continue
        if not entry.get("categorizedAt"):
            pending.append({
                "source": "email",
                "type": "new",
                "id": eid,
                "from": entry.get("from"),
                "subject": entry.get("subject"),
                "date": entry.get("date"),
                "labels": entry.get("labels", []),
                "snippet": entry.get("snippet"),
                "messageCount": entry.get("messageCount", 1),
            })
        elif entry.get("pendingReviewAt") and not entry.get("reviewedAt"):
            try:
                if entry["pendingReviewAt"] <= now:
                    pending.append({
                        "source": "email",
                        "type": "follow-up",
                        "id": eid,
                        "from": entry.get("from"),
                        "subject": entry.get("subject"),
                        "date": entry.get("date"),
                        "pendingReviewAt": entry["pendingReviewAt"],
                    })
            except (TypeError, ValueError):
                pass
    return pending


def _get_pending_calendar(calendar_data: dict) -> list[dict]:
    """Extract calendar items needing review."""
    from datetime import timedelta

    pending = []
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=24)).isoformat()

    for meeting in calendar_data.get("meetings", []):
        if not meeting.get("categorizedAt"):
            synced = meeting.get("fetchedAt", meeting.get("start", ""))
            if synced and synced < cutoff:
                continue
            pending.append({
                "source": "calendar",
                "type": "meeting",
                "id": meeting.get("id"),
                "title": meeting.get("title"),
                "start": meeting.get("start"),
                "startLocal": meeting.get("startLocal"),
                "end": meeting.get("end"),
                "endLocal": meeting.get("endLocal"),
                "attendees": meeting.get("attendees", []),
            })

    for change in calendar_data.get("changes", []):
        if not change.get("reviewedAt"):
            detected = change.get("timestamp", "")
            if detected and detected < cutoff:
                continue
            if not change.get("start"):
                continue
            pending.append({
                "source": "calendar",
                "type": "change",
                "id": change.get("eventId"),
                "title": change.get("title"),
                "changeType": change.get("type"),
                "details": change.get("details"),
                "start": change.get("start"),
                "end": change.get("end"),
                "detectedAt": change.get("timestamp"),
            })
            for meeting in calendar_data.get("meetings", []):
                if meeting.get("id") == change.get("eventId"):
                    pending[-1]["startLocal"] = meeting.get("startLocal")
                    pending[-1]["endLocal"] = meeting.get("endLocal")
                    pending[-1]["attendees"] = meeting.get("attendees", [])
                    break

    change_ids = {item["id"] for item in pending if item["type"] == "change"}
    return [item for item in pending if not (item["type"] == "meeting" and item["id"] in change_ids)]


def _get_pending_jira(jira_data: dict) -> list[dict]:
    """Extract pending Jira actions."""
    pending = []
    for action in jira_data.get("pendingActions", []):
        if not action.get("completedAt"):
            pending.append({
                "source": "jira",
                "type": "pending-action",
                "ticket": action.get("ticket"),
                "action": action.get("action"),
                "summary": action.get("summary"),
            })
    return pending


def _get_escalation_thread_ids() -> list[str]:
    """Query task DB for threadIds from escalation tasks (active or resolved <72h)."""
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT body FROM crm_tasks
                WHERE tags @> ARRAY['escalation']
                  AND deleted_at IS NULL
                  AND (resolved_at IS NULL OR resolved_at > NOW() - INTERVAL '72 hours')
            """)
            ids = []
            for row in cur.fetchall():
                m = re.search(r"threadId:\s*([a-zA-Z0-9]+)", row[0] or "")
                if m:
                    ids.append(m.group(1))
            return ids
    except Exception:
        return []


def build_triage_inbox(email_log: dict | None = None) -> int:
    """Build triage-inbox.json from email-log, calendar-log, and jira-log.

    Called at the end of every email sync. Returns total item count.
    """
    if email_log is None:
        email_log = load_log()

    calendar_data = _load_json(CALENDAR_LOG_PATH)
    jira_data = _load_json(JIRA_LOG_PATH)

    emails = _get_pending_emails(email_log)
    calendar = _get_pending_calendar(calendar_data)
    jira = _get_pending_jira(jira_data)

    all_items = emails + calendar + jira

    # Filter out already-escalated items (query task DB for active escalation threadIds)
    active_escalation_ids = _get_escalation_thread_ids()
    escalated_set = set(active_escalation_ids)
    all_items = [item for item in all_items if item.get("id") not in escalated_set]

    filtered_emails = [i for i in all_items if i.get("source") == "email"]
    filtered_calendar = [i for i in all_items if i.get("source") == "calendar"]
    filtered_jira = [i for i in all_items if i.get("source") == "jira"]

    inbox = {
        "preparedAt": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "emails": len(filtered_emails),
            "calendar": len(filtered_calendar),
            "jira": len(filtered_jira),
            "total": len(all_items),
        },
        "items": all_items,
        "activeEscalationIds": active_escalation_ids,
    }

    save_log(inbox, TRIAGE_INBOX_PATH)

    # Publish event so email-classifier hook fires
    if all_items:
        event_bus.publish(
            "email",
            "triage.refreshed",
            {"total": len(all_items), "emails": len(filtered_emails)},
            source="email_sync",
        )

    return len(all_items)


def main():
    print(f"[{datetime.now().isoformat()}] Email sync starting...")

    # Prevent concurrent runs via file lock
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("  Another email_sync instance is running, skipping.")
        lock_file.close()
        return

    try:
        _run_sync()
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def _run_sync():
    log = load_log()
    existing_ids = set(log.get("entries", {}).keys())

    # Step 1: Validate existing entries (self-healing loop)
    validation_resets = validate_entries(log)
    if validation_resets > 0:
        print(f"  Validation: reset {validation_resets} entries with null metadata")

    # Step 2: Backfill broken entries from previous syncs
    backfill_count = backfill_null_metadata(log)
    if backfill_count > 0:
        print(f"  Backfill: recovered metadata for {backfill_count} entries")

    # Step 3: Fetch new unread emails
    emails = fetch_unread_emails()
    print(f"Found {len(emails)} unread emails")

    new_count = 0
    reset_count = 0
    crm_logged = 0
    for email in emails:
        email_id = email.get("id")
        if not email_id:
            continue

        msg_count = email.get("messageCount", 1)

        if email_id in existing_ids:
            # Thread already in log — check if messageCount increased (new reply)
            entry = log["entries"][email_id]
            old_count = entry.get("messageCount", 1)
            if msg_count > old_count:
                if should_skip_reset(entry):
                    print(f"  Thread {email_id} recently replied, deferring reset")
                    entry["messageCount"] = msg_count
                    continue
                print(
                    f"  Thread {email_id} has new replies ({old_count} -> {msg_count}), resetting"
                )
                reset_entry_for_reprocessing(entry, f"thread-update-{msg_count}")
                entry["messageCount"] = msg_count
                reset_count += 1
            continue

        print(f"  New email: {email_id}")
        entry = create_minimal_entry(email)
        entry["messageCount"] = msg_count
        log["entries"][email_id] = entry
        new_count += 1

        # Dual-write: publish to event bus
        event_bus.publish(
            "email",
            "email.new",
            {
                "id": email_id,
                "from": entry.get("from"),
                "subject": entry.get("subject"),
                "date": entry.get("date"),
                "labels": entry.get("labels", []),
            },
            source="email_sync",
        )

        # Log to CRM if we have a real sender
        if entry.get("from"):
            if log_email_to_crm(entry):
                entry["crmLoggedAt"] = datetime.now().isoformat()
                crm_logged += 1

        # If this is a reply in an existing thread, reset the parent entry
        thread_id = email.get("threadId")
        if thread_id and thread_id != email_id:
            parent_id = find_thread_parent(log, thread_id, email_id)
            if parent_id and parent_id in log["entries"]:
                print(f"    -> Reply in thread {thread_id}, resetting parent {parent_id}")
                reset_entry_for_reprocessing(log["entries"][parent_id], email_id)
                reset_count += 1

    log["lastCheckedAt"] = datetime.now().isoformat()
    save_log(log)

    parts = [f"{new_count} new emails queued", f"{reset_count} thread parents reset"]
    if crm_logged > 0:
        parts.append(f"{crm_logged} logged to CRM")
    if backfill_count > 0:
        parts.append(f"{backfill_count} backfilled")
    if validation_resets > 0:
        parts.append(f"{validation_resets} validation resets")
    print(f"[{datetime.now().isoformat()}] Done. {', '.join(parts)}.")

    # Rebuild triage-inbox.json after every sync
    triage_count = build_triage_inbox(log)
    if triage_count > 0:
        print(f"  Triage inbox: {triage_count} items")


if __name__ == "__main__":
    main()
