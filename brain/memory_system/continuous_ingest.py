#!/usr/bin/env python3
"""
Tier 1: Continuous Ingestion — runs every 10 minutes via crontab.

Incrementally ingests new/changed items from all data sources with dedup.
Each source is independent — failure in one doesn't block others.

Sources: email-log.json, calendar-log.json, tasks.json (general + Jira),
         CRM conversations, CRM notes/tasks,
         Google Meet transcripts (meet-transcripts.json).

Uses fcntl.flock() to prevent concurrent runs.
Skips if nightly pipeline (Tier 3) is running.

Cron: */10 * * * *
Expected: 0-3 items per run, ~2-5 min worst case.
"""

import asyncio
import fcntl
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Paths
MEMORY_DIR = Path("/home/philip/robothor/brain/memory")
MEMORY_SYSTEM_DIR = Path("/home/philip/robothor/brain/memory_system")
LOGS_DIR = MEMORY_SYSTEM_DIR / "logs"
LOCK_FILE = MEMORY_SYSTEM_DIR / "locks" / "continuous_ingest.lock"
NIGHTLY_LOCK = MEMORY_SYSTEM_DIR / "locks" / "nightly_pipeline.lock"
HANDOFF_FILE = MEMORY_DIR / "worker-handoff.json"

# Package imports — no sys.path manipulation needed
# robothor package is installed in the venv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Ensure dirs exist
LOGS_DIR.mkdir(exist_ok=True)
LOCK_FILE.parent.mkdir(exist_ok=True)


def acquire_lock() -> Any:
    """Acquire exclusive file lock. Returns file handle or None."""
    try:
        fh = open(LOCK_FILE, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(datetime.now().isoformat()))
        fh.flush()
        return fh
    except OSError:
        return None


def is_nightly_running() -> bool:
    """Check if the nightly pipeline has an active lock."""
    if not NIGHTLY_LOCK.exists():
        return False
    try:
        fh = open(NIGHTLY_LOCK)
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # We got the lock — nightly is NOT running
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
        return False
    except OSError:
        return True


def _escalate_errors(source: str, error_count: int, error_msg: str):
    """Write to worker-handoff.json when a source has 3+ consecutive errors."""
    if error_count < 3:
        return
    try:
        handoff = {"items": []}
        if HANDOFF_FILE.exists():
            handoff = json.loads(HANDOFF_FILE.read_text())

        handoff.setdefault("items", []).append(
            {
                "type": "ingestion_error",
                "source": source,
                "error_count": error_count,
                "error": error_msg[:500],
                "timestamp": datetime.now().isoformat(),
            }
        )
        fd, tmp = tempfile.mkstemp(dir=HANDOFF_FILE.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(handoff, indent=2))
            os.replace(tmp, HANDOFF_FILE)
        except BaseException:
            os.unlink(tmp)
            raise
        logger.warning("Escalated %s errors (%d consecutive) to handoff", source, error_count)
    except Exception as e:
        logger.error("Failed to escalate to handoff: %s", e)


# ═══════════════════════════════════════════════════════════════════
# Source: email-log.json
# ═══════════════════════════════════════════════════════════════════


async def ingest_emails() -> dict[str, int]:
    """Ingest categorized emails (triage worker has processed them)."""
    from robothor.memory.ingest_state import (
        content_hash,
        is_already_ingested,
        record_error,
        record_ingested,
        update_watermark,
    )

    results = {"new": 0, "skipped": 0, "changed": 0, "errors": 0}
    email_log_path = MEMORY_DIR / "email-log.json"

    if not email_log_path.exists():
        update_watermark("email", 0)
        return results

    try:
        email_log = json.loads(email_log_path.read_text())

        for entry_id, entry in email_log.get("entries", {}).items():
            # Only ingest categorized entries (triage worker processed)
            if not entry.get("categorizedAt"):
                continue

            # Skip noreply + low urgency
            urgency = entry.get("urgency", "low")
            from_addr = entry.get("from", "")
            if urgency == "low" and any(
                skip in from_addr
                for skip in ["noreply", "no-reply", "notifications@", "team@", "marketing@"]
            ):
                results["skipped"] += 1
                continue

            h = content_hash(entry, ["from", "subject", "summary", "urgency"])

            if is_already_ingested("email", entry_id, h):
                results["skipped"] += 1
                continue

            # Build content for ingestion
            timestamp_str = (
                entry.get("processedAt") or entry.get("categorizedAt") or entry.get("date") or ""
            )
            content = f"""Email from {entry.get("from", "unknown")}
Subject: {entry.get("subject", "no subject")}
Date: {timestamp_str}
Summary: {entry.get("summary", "")}
Urgency: {urgency}
Needs Response: {entry.get("needsResponse", False)}
Action Owner: {entry.get("actionOwner", "none")}"""

            try:
                from robothor.memory.ingestion import ingest_content

                result = await ingest_content(
                    content=content,
                    source_channel="email",
                    content_type="email",
                    metadata={"email_id": entry_id, "urgency": urgency},
                )
                record_ingested("email", entry_id, h, result.get("fact_ids", []))
                results["new"] += 1
            except Exception as e:
                logger.error("Failed to ingest email %s: %s", entry_id, e)
                results["errors"] += 1

        update_watermark("email", results["new"])

    except Exception as e:
        logger.error("Email ingestion failed: %s", e)
        error_count = record_error("email", str(e))
        _escalate_errors("email", error_count, str(e))
        results["errors"] += 1

    return results


# ═══════════════════════════════════════════════════════════════════
# Source: calendar-log.json
# ═══════════════════════════════════════════════════════════════════


async def ingest_calendar() -> dict[str, int]:
    """Ingest calendar events in next 48h or recently modified."""
    from robothor.memory.ingest_state import (
        content_hash,
        is_already_ingested,
        record_error,
        record_ingested,
        update_watermark,
    )

    results = {"new": 0, "skipped": 0, "errors": 0}
    calendar_path = MEMORY_DIR / "calendar-log.json"

    if not calendar_path.exists():
        update_watermark("calendar", 0)
        return results

    try:
        calendar_data = json.loads(calendar_path.read_text())
        now = datetime.now()
        cutoff_future = now + timedelta(hours=48)

        for event_id, event in calendar_data.get("entries", {}).items():
            start_str = event.get("start", "")
            if not start_str:
                continue

            # Only events in next 48h
            try:
                event_time = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if event_time.tzinfo:
                    event_time = event_time.replace(tzinfo=None)
                if event_time < now - timedelta(hours=1) or event_time > cutoff_future:
                    continue
            except (ValueError, TypeError):
                continue

            h = content_hash(event, ["summary", "start", "end", "attendees"])

            if is_already_ingested("calendar", event_id, h):
                results["skipped"] += 1
                continue

            attendees = event.get("attendees", [])
            att_str = ", ".join(
                a if isinstance(a, str) else a.get("email", "") for a in attendees[:10]
            )

            content = f"""Calendar Event: {event.get("summary", "Meeting")}
Start: {start_str}
End: {event.get("end", "")}
Attendees: {att_str}
Location: {event.get("location", "")}
Description: {event.get("description", "")[:500]}"""

            try:
                from robothor.memory.ingestion import ingest_content

                result = await ingest_content(
                    content=content,
                    source_channel="api",
                    content_type="event",
                    metadata={"calendar_event_id": event_id},
                )
                record_ingested("calendar", event_id, h, result.get("fact_ids", []))
                results["new"] += 1
            except Exception as e:
                logger.error("Failed to ingest calendar event %s: %s", event_id, e)
                results["errors"] += 1

        update_watermark("calendar", results["new"])

    except Exception as e:
        logger.error("Calendar ingestion failed: %s", e)
        error_count = record_error("calendar", str(e))
        _escalate_errors("calendar", error_count, str(e))
        results["errors"] += 1

    return results


# ═══════════════════════════════════════════════════════════════════
# Source: tasks.json (general tasks)
# ═══════════════════════════════════════════════════════════════════


async def ingest_tasks() -> dict[str, int]:
    """Ingest active + recently completed tasks."""
    from robothor.memory.ingest_state import (
        content_hash,
        is_already_ingested,
        record_error,
        record_ingested,
        update_watermark,
    )

    results = {"new": 0, "skipped": 0, "errors": 0}
    tasks_path = MEMORY_DIR / "tasks.json"

    if not tasks_path.exists():
        update_watermark("tasks", 0)
        return results

    try:
        tasks_data = json.loads(tasks_path.read_text())
        cutoff_7d = datetime.now() - timedelta(days=7)

        for task in tasks_data.get("tasks", []):
            task_id = task.get("id", "")
            if not task_id:
                continue

            # Skip Jira tasks (handled separately)
            source = task.get("source", "")
            if source.startswith("jira:"):
                continue

            status = task.get("status", "")

            # Completed tasks: only if within 7 days
            if status == "completed":
                completed_at = task.get("completedAt", "")
                if completed_at:
                    try:
                        t = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
                        if t.tzinfo:
                            t = t.replace(tzinfo=None)
                        if t < cutoff_7d:
                            continue
                    except (ValueError, TypeError):
                        continue
                else:
                    continue

            # Active tasks: include all
            elif status not in ("pending", "in-progress", "active"):
                continue

            h = content_hash(task, ["description", "status", "priority", "notes"])

            if is_already_ingested("tasks", task_id, h):
                results["skipped"] += 1
                continue

            if status == "completed":
                content = f"""Task Completed: {task.get("description", "")}
Completed: {task.get("completedAt", "")}
Owner: {task.get("owner", "unknown")}
Notes: {task.get("notes", "")}
Source: {source}"""
                content_type = "decision"
            else:
                content = f"""Active Task: {task.get("description", "")}
Status: {status}
Priority: {task.get("priority", "medium")}
Owner: {task.get("owner", "unknown")}
Notes: {task.get("notes", "")}
Source: {source}"""
                content_type = "technical"

            try:
                from robothor.memory.ingestion import ingest_content

                result = await ingest_content(
                    content=content,
                    source_channel="moltbot",
                    content_type=content_type,
                    metadata={"task_id": task_id},
                )
                record_ingested("tasks", task_id, h, result.get("fact_ids", []))
                results["new"] += 1
            except Exception as e:
                logger.error("Failed to ingest task %s: %s", task_id, e)
                results["errors"] += 1

        update_watermark("tasks", results["new"])

    except Exception as e:
        logger.error("Tasks ingestion failed: %s", e)
        error_count = record_error("tasks", str(e))
        _escalate_errors("tasks", error_count, str(e))
        results["errors"] += 1

    return results


# ═══════════════════════════════════════════════════════════════════
# Source: tasks.json (Jira tickets)
# ═══════════════════════════════════════════════════════════════════


async def ingest_jira() -> dict[str, int]:
    """Ingest Jira-sourced tasks with full ticket context."""
    from robothor.memory.ingest_state import (
        content_hash,
        is_already_ingested,
        record_error,
        record_ingested,
        update_watermark,
    )

    results = {"new": 0, "skipped": 0, "errors": 0}
    tasks_path = MEMORY_DIR / "tasks.json"

    if not tasks_path.exists():
        update_watermark("jira", 0)
        return results

    try:
        tasks_data = json.loads(tasks_path.read_text())

        for task in tasks_data.get("tasks", []):
            source = task.get("source", "")
            if not source.startswith("jira:"):
                continue

            task_id = task.get("id", "")
            if not task_id:
                continue

            # Use lastSynced for recency — skip stale entries
            time_str = task.get("lastSynced") or task.get("createdAt") or ""
            if time_str:
                try:
                    t = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                    if t.tzinfo:
                        t = t.replace(tzinfo=None)
                    if t < datetime.now() - timedelta(days=7):
                        continue
                except (ValueError, TypeError):
                    pass

            h = content_hash(task, ["description", "jiraStatus", "priority", "notes"])

            if is_already_ingested("jira", task_id, h):
                results["skipped"] += 1
                continue

            content = f"""Jira Ticket: {task_id} — {task.get("description", "")}
Jira Status: {task.get("jiraStatus", task.get("status", ""))}
Priority: {task.get("priority", "medium")}
Owner: {task.get("owner", "unknown")}
Source: {task.get("sourceDetails", source)}
URL: {task.get("jiraUrl", "")}
Notes: {task.get("notes", "")}"""

            try:
                from robothor.memory.ingestion import ingest_content

                result = await ingest_content(
                    content=content,
                    source_channel="jira",
                    content_type="technical",
                    metadata={
                        "task_id": task_id,
                        "jira_project": source.replace("jira:", ""),
                    },
                )
                record_ingested("jira", task_id, h, result.get("fact_ids", []))
                results["new"] += 1
            except Exception as e:
                logger.error("Failed to ingest Jira ticket %s: %s", task_id, e)
                results["errors"] += 1

        update_watermark("jira", results["new"])

    except Exception as e:
        logger.error("Jira ingestion failed: %s", e)
        error_count = record_error("jira", str(e))
        _escalate_errors("jira", error_count, str(e))
        results["errors"] += 1

    return results


# ═══════════════════════════════════════════════════════════════════
# Source: CRM conversations
# ═══════════════════════════════════════════════════════════════════


async def ingest_conversations() -> dict[str, int]:
    """Ingest CRM conversations from the last 48h."""
    from robothor.memory.ingest_state import (
        content_hash,
        is_already_ingested,
        record_error,
        record_ingested,
        update_watermark,
    )

    results = {"new": 0, "skipped": 0, "errors": 0}

    try:
        from crm_fetcher import fetch_conversations, format_conversation_for_ingestion

        conversations = fetch_conversations(hours=48)

        for conv in conversations:
            if not conv.get("messages"):
                continue

            conv_id = str(conv["id"])
            h = content_hash(conv, ["last_activity_at", "id"])

            if is_already_ingested("conversation", conv_id, h):
                results["skipped"] += 1
                continue

            content = format_conversation_for_ingestion(conv)

            try:
                from robothor.memory.ingestion import ingest_content

                result = await ingest_content(
                    content=content,
                    source_channel="conversation",
                    content_type="conversation",
                    metadata={
                        "conversation_id": conv["id"],
                        "contact_name": conv.get("contact_name"),
                        "contact_email": conv.get("contact_email"),
                    },
                )
                record_ingested("conversation", conv_id, h, result.get("fact_ids", []))
                results["new"] += 1
            except Exception as e:
                logger.error("Failed to ingest conversation %s: %s", conv_id, e)
                results["errors"] += 1

        update_watermark("conversation", results["new"])

    except Exception as e:
        logger.error("Conversation ingestion failed: %s", e)
        error_count = record_error("conversation", str(e))
        _escalate_errors("conversation", error_count, str(e))
        results["errors"] += 1

    return results


# ═══════════════════════════════════════════════════════════════════
# Source: Twenty CRM (notes + tasks)
# ═══════════════════════════════════════════════════════════════════


async def ingest_twenty_crm() -> dict[str, int]:
    """Ingest Twenty CRM notes and tasks from the last 48h."""
    from robothor.memory.ingest_state import (
        content_hash,
        is_already_ingested,
        record_error,
        record_ingested,
        update_watermark,
    )

    results = {"new": 0, "skipped": 0, "errors": 0}

    try:
        from crm_fetcher import fetch_twenty_notes, fetch_twenty_tasks

        # Notes
        notes = fetch_twenty_notes(hours=48)
        for note in notes:
            note_id = note["id"]
            h = content_hash(note, ["title", "body", "updatedAt"])

            if is_already_ingested("twenty_notes", note_id, h):
                results["skipped"] += 1
                continue

            content = f"""CRM Note: {note.get("title", "Untitled")}
Body: {note.get("body", "")[:1000]}
Related to: {", ".join(note.get("targets", []))}
Created: {note.get("createdAt", "")}"""

            try:
                from robothor.memory.ingestion import ingest_content

                result = await ingest_content(
                    content=content,
                    source_channel="crm",
                    content_type="decision",
                    metadata={"twenty_note_id": note_id},
                )
                record_ingested("twenty_notes", note_id, h, result.get("fact_ids", []))
                results["new"] += 1
            except Exception as e:
                logger.error("Failed to ingest Twenty note %s: %s", note_id, e)
                results["errors"] += 1

        # Tasks
        tasks = fetch_twenty_tasks(hours=48)
        for task in tasks:
            task_id = task["id"]
            h = content_hash(task, ["title", "body", "status", "updatedAt"])

            if is_already_ingested("twenty_tasks", task_id, h):
                results["skipped"] += 1
                continue

            content = f"""CRM Task: {task.get("title", "Untitled")}
Status: {task.get("status", "")}
Body: {task.get("body", "")[:500]}
Due: {task.get("dueAt", "none")}
Related to: {", ".join(task.get("targets", []))}"""

            try:
                from robothor.memory.ingestion import ingest_content

                result = await ingest_content(
                    content=content,
                    source_channel="crm",
                    content_type="decision",
                    metadata={"twenty_task_id": task_id},
                )
                record_ingested("twenty_tasks", task_id, h, result.get("fact_ids", []))
                results["new"] += 1
            except Exception as e:
                logger.error("Failed to ingest Twenty task %s: %s", task_id, e)
                results["errors"] += 1

        update_watermark("twenty_crm", results["new"])

    except Exception as e:
        logger.error("Twenty CRM ingestion failed: %s", e)
        error_count = record_error("twenty_crm", str(e))
        _escalate_errors("twenty_crm", error_count, str(e))
        results["errors"] += 1

    return results


# ═══════════════════════════════════════════════════════════════════
# Source: contacts (Twenty CRM first, fallback JSON)
# ═══════════════════════════════════════════════════════════════════


async def ingest_contacts() -> dict[str, int]:
    """Ingest contacts from Twenty CRM (fallback to contacts.json)."""
    from robothor.memory.ingest_state import (
        content_hash,
        is_already_ingested,
        record_error,
        record_ingested,
        update_watermark,
    )

    results = {"new": 0, "skipped": 0, "errors": 0}

    try:
        from crm_fetcher import fetch_twenty_contacts, format_contact_for_ingestion

        contacts = fetch_twenty_contacts(hours=168)  # 7 days

        for contact in contacts:
            contact_id = contact["id"]
            h = content_hash(contact, ["firstName", "lastName", "email", "jobTitle", "company"])

            if is_already_ingested("contacts", contact_id, h):
                results["skipped"] += 1
                continue

            content = format_contact_for_ingestion(contact)

            try:
                from robothor.memory.ingestion import ingest_content

                result = await ingest_content(
                    content=content,
                    source_channel="crm",
                    content_type="contact",
                    metadata={
                        "twenty_id": contact_id,
                        "contact_email": contact.get("email"),
                    },
                )
                record_ingested("contacts", contact_id, h, result.get("fact_ids", []))
                results["new"] += 1
            except Exception as e:
                logger.error("Failed to ingest contact %s: %s", contact_id, e)
                results["errors"] += 1

        update_watermark("contacts", results["new"])

    except Exception as e:
        # Fallback to contacts.json
        logger.warning("CRM contact fetch failed, trying contacts.json: %s", e)
        contacts_path = MEMORY_DIR / "contacts.json"
        if contacts_path.exists():
            try:
                contacts_data = json.loads(contacts_path.read_text())
                cutoff_30d = datetime.now() - timedelta(days=30)

                for contact in contacts_data.get("contacts", []):
                    recent_activity = contact.get("recentActivity", [])
                    if not recent_activity:
                        continue

                    latest = recent_activity[-1]
                    activity_date = latest.get("date", "")
                    if not activity_date:
                        continue

                    try:
                        activity_time = datetime.strptime(activity_date, "%Y-%m-%d")
                        if activity_time < cutoff_30d:
                            continue
                    except (ValueError, TypeError):
                        continue

                    email = contact.get("email", "")
                    h = content_hash(contact, ["name", "email", "company", "role"])

                    if is_already_ingested("contacts", email, h):
                        results["skipped"] += 1
                        continue

                    content = f"""Contact: {contact.get("name", "Unknown")}
Email: {email}
Phone: {contact.get("phone", "")}
Company: {contact.get("company", "")}
Role: {contact.get("role", "")}
Profile: {contact.get("profile", "")}
Recent Activity: {latest.get("note", "")} ({activity_date})"""

                    try:
                        from robothor.memory.ingestion import ingest_content

                        result = await ingest_content(
                            content=content,
                            source_channel="moltbot",
                            content_type="contact",
                            metadata={"contact_email": email},
                        )
                        record_ingested("contacts", email, h, result.get("fact_ids", []))
                        results["new"] += 1
                    except Exception as e2:
                        logger.error("Failed to ingest contact %s: %s", email, e2)
                        results["errors"] += 1

                update_watermark("contacts", results["new"])

            except Exception as e2:
                error_count = record_error("contacts", str(e2))
                _escalate_errors("contacts", error_count, str(e2))
                results["errors"] += 1
        else:
            error_count = record_error("contacts", str(e))
            _escalate_errors("contacts", error_count, str(e))
            results["errors"] += 1

    return results


# ═══════════════════════════════════════════════════════════════════
# Source: Google Meet transcripts (Gemini Notes)
# ═══════════════════════════════════════════════════════════════════


def _segment_transcript(transcript: str, max_chars: int = 3000) -> list[str]:
    """Split a transcript into segments at speaker boundaries.

    Splits at newlines before "SpeakerName:" lines, keeping each segment
    under max_chars. Returns empty list for empty input.
    """
    if not transcript or not transcript.strip():
        return []

    lines = transcript.split("\n")
    segments = []
    current = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1  # +1 for newline

        # Check if this line starts a new speaker turn
        is_speaker_line = bool(re.match(r"^[A-Z][a-zA-Z\s'-]+:\s", line))

        # If adding this line would exceed limit AND we're at a speaker boundary
        if current_len + line_len > max_chars and is_speaker_line and current:
            segments.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len

    if current:
        segments.append("\n".join(current))

    return segments


async def ingest_google_meet() -> dict[str, int]:
    """Ingest Google Meet transcripts from meet-transcripts.json."""
    from robothor.memory.ingest_state import (
        content_hash,
        is_already_ingested,
        record_error,
        record_ingested,
        update_watermark,
    )

    results = {"new": 0, "skipped": 0, "errors": 0}
    transcripts_path = MEMORY_DIR / "meet-transcripts.json"

    if not transcripts_path.exists():
        update_watermark("google_meet", 0)
        return results

    try:
        data = json.loads(transcripts_path.read_text())

        for doc_id, entry in data.get("entries", {}).items():
            h = content_hash(entry, ["docId", "modifiedTime"])

            if is_already_ingested("google_meet", doc_id, h):
                results["skipped"] += 1
                continue

            title = entry.get("title", "Meeting")
            date = entry.get("date", "")
            attendees = ", ".join(entry.get("attendees", []))

            # Segment 0: Summary block (always ingested)
            decisions_str = "\n".join(f"- {d}" for d in entry.get("decisions", []))
            next_steps_str = "\n".join(f"- {s}" for s in entry.get("nextSteps", []))

            summary_content = f"""Google Meet: {title}
Date: {date}
Attendees: {attendees}
Summary: {entry.get("summary", "")}
Decisions:
{decisions_str}
Next Steps:
{next_steps_str}"""

            all_fact_ids = []

            try:
                from robothor.memory.ingestion import ingest_content

                result = await ingest_content(
                    content=summary_content,
                    source_channel="api",
                    content_type="conversation",
                    metadata={
                        "meet_doc_id": doc_id,
                        "meeting_title": title,
                        "meeting_date": date,
                        "segment": "summary",
                    },
                )
                all_fact_ids.extend(result.get("fact_ids", []))
            except Exception as e:
                logger.error("Failed to ingest Meet summary %s: %s", doc_id, e)
                results["errors"] += 1
                continue

            # Segments 1-N: Transcript chunks
            transcript = entry.get("transcript", "")
            if transcript.strip():
                segments = _segment_transcript(transcript)
                for i, segment in enumerate(segments):
                    segment_content = f"""Google Meet Transcript: {title} (Part {i + 1}/{len(segments)})
Date: {date}
Attendees: {attendees}

{segment}"""

                    try:
                        result = await ingest_content(
                            content=segment_content,
                            source_channel="api",
                            content_type="conversation",
                            metadata={
                                "meet_doc_id": doc_id,
                                "meeting_title": title,
                                "meeting_date": date,
                                "segment": f"transcript_{i + 1}",
                            },
                        )
                        all_fact_ids.extend(result.get("fact_ids", []))
                    except Exception as e:
                        logger.error(
                            "Failed to ingest Meet transcript segment %s/%d: %s", doc_id, i + 1, e
                        )
                        results["errors"] += 1

            record_ingested("google_meet", doc_id, h, all_fact_ids)
            results["new"] += 1

        update_watermark("google_meet", results["new"])

    except Exception as e:
        logger.error("Google Meet ingestion failed: %s", e)
        error_count = record_error("google_meet", str(e))
        _escalate_errors("google_meet", error_count, str(e))
        results["errors"] += 1

    return results


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════


async def run_continuous_ingest() -> dict[str, Any]:
    """Run all source ingestions. Returns combined results."""
    results = {}

    sources = [
        ("email", ingest_emails),
        ("calendar", ingest_calendar),
        ("tasks", ingest_tasks),
        ("jira", ingest_jira),
        ("conversation", ingest_conversations),
        ("twenty_crm", ingest_twenty_crm),
        ("contacts", ingest_contacts),
        ("google_meet", ingest_google_meet),
    ]

    for name, func in sources:
        try:
            results[name] = await func()
            new = results[name].get("new", 0)
            if new > 0:
                logger.info("  %s: %d new items ingested", name, new)
        except Exception as e:
            logger.error("  %s: FAILED — %s", name, e)
            results[name] = {"new": 0, "errors": 1, "error": str(e)}

    return results


async def main():
    start_time = datetime.now()
    logger.info("═══ Continuous Ingest Started: %s ═══", start_time)

    # Acquire lock
    lock_fh = acquire_lock()
    if lock_fh is None:
        logger.info("Another instance is running, skipping.")
        return

    # Check nightly pipeline
    if is_nightly_running():
        logger.info("Nightly pipeline is running, skipping.")
        lock_fh.close()
        return

    try:
        results = await run_continuous_ingest()

        total_new = sum(r.get("new", 0) for r in results.values())
        total_errors = sum(r.get("errors", 0) for r in results.values())
        duration = (datetime.now() - start_time).total_seconds()

        total_skipped = sum(r.get("skipped", 0) for r in results.values())

        logger.info(
            "═══ Continuous Ingest Complete: %d new, %d skipped, %d errors (%.1fs) ═══",
            total_new,
            total_skipped,
            total_errors,
            duration,
        )

        # Write quality metrics to log file for observability
        metrics = {
            "timestamp": start_time.isoformat(),
            "duration_s": round(duration, 1),
            "facts_extracted": total_new,
            "facts_skipped_dedup": total_skipped,
            "errors": total_errors,
            "sources": {name: dict(r) for name, r in results.items()},
        }
        metrics_path = LOGS_DIR / "ingest-metrics.jsonl"
        try:
            with open(metrics_path, "a") as f:
                f.write(json.dumps(metrics, default=str) + "\n")
        except Exception as e:
            logger.warning("Failed to write metrics: %s", e)

        # Escalate if 3+ consecutive runs extract 0 facts from non-empty sources
        if total_new == 0 and total_skipped > 0:
            zero_runs_file = LOGS_DIR / "zero-extract-count.txt"
            try:
                count = int(zero_runs_file.read_text().strip()) if zero_runs_file.exists() else 0
                count += 1
                zero_runs_file.write_text(str(count))
                if count >= 3:
                    logger.warning(
                        "ALERT: %d consecutive runs with 0 new facts from non-empty content", count
                    )
                    _escalate_errors(
                        "quality_gate", count, f"{count} consecutive zero-extraction runs"
                    )
            except Exception:
                pass
        else:
            # Reset counter on success
            zero_runs_file = LOGS_DIR / "zero-extract-count.txt"
            try:
                if zero_runs_file.exists():
                    zero_runs_file.unlink()
            except Exception:
                pass

        # Intra-day consolidation + insight discovery after new facts
        if total_new > 0:
            try:
                from robothor.memory.lifecycle import run_intraday_consolidation

                consolidation_result = await run_intraday_consolidation(threshold=5)
                metrics["consolidation"] = consolidation_result

                # Run insight discovery if consolidation actually merged something
                if not consolidation_result.get("skipped"):
                    try:
                        from robothor.memory.lifecycle import run_insight_discovery

                        insight_result = await run_insight_discovery(hours_back=12)
                        metrics["insights"] = insight_result
                    except Exception as e:
                        logger.warning("Intra-day insight discovery failed: %s", e)
            except Exception as e:
                logger.warning("Intra-day consolidation failed: %s", e)

            print(f"Continuous Ingest — {start_time.strftime('%H:%M')}")
            for name, r in results.items():
                new = r.get("new", 0)
                if new > 0:
                    print(f"  {name}: {new} new")
            print(f"  Duration: {duration:.1f}s")

    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


if __name__ == "__main__":
    asyncio.run(main())
