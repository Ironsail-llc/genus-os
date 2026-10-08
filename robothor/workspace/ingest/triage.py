"""``triage-inbox.json``: what the triage agents still have to look at, in one small file.

The email classifier and the calendar monitor read this file instead of the
full logs. It is rebuilt from the logs that sit next to ``email-log.json``:

* ``email-log.json`` -- every entry not yet categorized (``type: "new"``) and
  every categorized entry whose ``pendingReviewAt`` is due and not reviewed
  (``type: "follow-up"``);
* ``calendar-log.json``, when present -- uncategorized meetings and unreviewed
  changes from the last 24 hours (a meeting that also has a change appears
  once, as the change);
* ``jira-log.json``, when present -- pending actions not completed.

Items whose id (or, for mail, conversation id) belongs to an open escalation
task -- or one resolved in the last 72 hours -- are left out, and those ids are
listed in ``activeEscalationIds`` so an agent never escalates a thread twice.

This is the shape the Google instance's sync script writes
(schema ``triage_inbox``, :mod:`robothor.events.contract`); mail items also
carry ``threadId``, which the classifier puts in the tasks it creates. The
file is written atomically, a temp file then a rename. The path is the
``workspace_triage_inbox_path`` setting; empty means ``triage-inbox.json``
beside the email log.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "CALENDAR_LOOKBACK",
    "ESCALATION_WINDOW",
    "TriageInbox",
    "build_triage_inbox",
    "default_path",
    "pg_escalation_ids",
]

FILE_NAME = "triage-inbox.json"
#: Calendar items older than this are no longer news.
CALENDAR_LOOKBACK = timedelta(hours=24)
#: How long a resolved escalation still suppresses its thread.
ESCALATION_WINDOW = timedelta(hours=72)
#: ``threadId: <id>`` in an escalation task body. Gmail ids are hex; Exchange
#: conversation ids are base64 (``+``, ``/``, ``=``, ``-``, ``_``).
_THREAD_ID = re.compile(r"threadId:\s*([A-Za-z0-9+/=_-]+)")


def default_path(email_log_path: Path | str) -> Path:
    """The configured triage-inbox path, else ``triage-inbox.json`` beside the email log."""
    from robothor.settings import get_settings

    configured = (get_settings().workspace.workspace_triage_inbox_path or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(email_log_path).parent / FILE_NAME


# ── the pure build ─────────────────────────────────────────────────────


def _pending_emails(email_log: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    entries = email_log.get("entries")
    if not isinstance(entries, dict):
        return []
    stamp = now.isoformat()
    pending: list[dict[str, Any]] = []
    for eid, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        if not entry.get("categorizedAt"):
            item: dict[str, Any] = {
                "source": "email",
                "type": "new",
                "id": eid,
                "threadId": entry.get("threadId"),
                "from": entry.get("from"),
                "subject": entry.get("subject"),
                "date": entry.get("date"),
                "labels": entry.get("labels", []),
                "snippet": entry.get("snippet"),
                "messageCount": entry.get("messageCount", 1),
            }
            if not entry.get("from"):
                item["needsBackfill"] = True
            pending.append(item)
        elif entry.get("from") and entry.get("pendingReviewAt") and not entry.get("reviewedAt"):
            due = entry["pendingReviewAt"]
            if isinstance(due, str) and due <= stamp:
                pending.append(
                    {
                        "source": "email",
                        "type": "follow-up",
                        "id": eid,
                        "threadId": entry.get("threadId"),
                        "from": entry.get("from"),
                        "subject": entry.get("subject"),
                        "date": entry.get("date"),
                        "pendingReviewAt": due,
                    }
                )
    return pending


def _pending_calendar(calendar: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    cutoff = (now - CALENDAR_LOOKBACK).isoformat()
    meetings = [m for m in calendar.get("meetings") or [] if isinstance(m, dict)]
    pending: list[dict[str, Any]] = []
    for meeting in meetings:
        if meeting.get("categorizedAt"):
            continue
        synced = meeting.get("fetchedAt", meeting.get("start", ""))
        if synced and str(synced) < cutoff:
            continue
        pending.append(
            {
                "source": "calendar",
                "type": "meeting",
                "id": meeting.get("id"),
                "title": meeting.get("title"),
                "start": meeting.get("start"),
                "startLocal": meeting.get("startLocal"),
                "end": meeting.get("end"),
                "endLocal": meeting.get("endLocal"),
                "attendees": meeting.get("attendees", []),
            }
        )
    for change in calendar.get("changes") or []:
        if not isinstance(change, dict) or change.get("reviewedAt"):
            continue
        detected = change.get("timestamp", "")
        if detected and str(detected) < cutoff:
            continue
        if not change.get("start"):
            continue
        item: dict[str, Any] = {
            "source": "calendar",
            "type": "change",
            "id": change.get("eventId"),
            "title": change.get("title"),
            "changeType": change.get("type"),
            "details": change.get("details"),
            "start": change.get("start"),
            "end": change.get("end"),
            "detectedAt": change.get("timestamp"),
        }
        for meeting in meetings:
            if meeting.get("id") == change.get("eventId"):
                item["startLocal"] = meeting.get("startLocal")
                item["endLocal"] = meeting.get("endLocal")
                item["attendees"] = meeting.get("attendees", [])
                break
        pending.append(item)
    changed = {i["id"] for i in pending if i["type"] == "change"}
    return [i for i in pending if not (i["type"] == "meeting" and i["id"] in changed)]


def _pending_jira(jira: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "source": "jira",
            "type": "pending-action",
            "ticket": action.get("ticket"),
            "action": action.get("action"),
            "summary": action.get("summary"),
        }
        for action in jira.get("pendingActions") or []
        if isinstance(action, dict) and not action.get("completedAt")
    ]


def build_triage_inbox(
    email_log: dict[str, Any],
    calendar_log: dict[str, Any],
    jira_log: dict[str, Any],
    escalation_ids: list[str],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The triage inbox for these logs. Pure: no IO."""
    now = now or datetime.now(UTC)
    items = (
        _pending_emails(email_log, now)
        + _pending_calendar(calendar_log, now)
        + _pending_jira(jira_log)
    )
    escalated = set(escalation_ids)
    items = [
        i
        for i in items
        if i.get("id") not in escalated
        and not (i["source"] == "email" and i.get("threadId") in escalated)
    ]
    counts = {
        source: sum(1 for i in items if i["source"] == source)
        for source in ("email", "calendar", "jira")
    }
    return {
        "preparedAt": now.isoformat(),
        "counts": {
            "emails": counts["email"],
            "calendar": counts["calendar"],
            "jira": counts["jira"],
            "total": len(items),
        },
        "items": items,
        "activeEscalationIds": list(escalation_ids),
    }


# ── escalations ────────────────────────────────────────────────────────


def _escalation_ids_sync(tenant_id: str, connect: Callable[[], Any] | None) -> list[str]:
    from robothor.db.connection import get_connection, tenant_scope

    scope = contextlib.nullcontext() if connect is not None else tenant_scope(tenant_id)
    with scope, (connect or get_connection)() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT body FROM crm_tasks WHERE tenant_id = %s AND tags @> ARRAY['escalation'] "
            "AND deleted_at IS NULL AND (resolved_at IS NULL OR resolved_at > now() - %s) "
            "ORDER BY created_at",
            (tenant_id, ESCALATION_WINDOW),
        )
        rows = cur.fetchall()
    ids: list[str] = []
    for row in rows:
        match = _THREAD_ID.search(str(row[0] or ""))
        if match and match.group(1) not in ids:
            ids.append(match.group(1))
    return ids


def pg_escalation_ids(
    tenant_id: str, *, connect: Callable[[], Any] | None = None
) -> Callable[[], Awaitable[list[str]]]:
    """The thread ids of ``tenant_id``'s open (or recently resolved) escalation tasks.

    ``connect`` (tests) is a zero-argument callable returning a context manager
    that yields a psycopg2 connection; by default the platform pool, bound to
    the tenant.
    """
    if not tenant_id:
        raise ValueError("explicit platform tenant required")

    async def lookup() -> list[str]:
        return await asyncio.to_thread(_escalation_ids_sync, tenant_id, connect)

    return lookup


# ── the file ───────────────────────────────────────────────────────────


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".triage-inbox.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise


class TriageInbox:
    """``triage-inbox.json`` at ``path``, built from the logs beside ``email_log_path``."""

    def __init__(
        self,
        path: Path | str,
        *,
        email_log_path: Path | str,
        escalations: Callable[[], Awaitable[list[str]]],
    ) -> None:
        self.path = Path(path)
        self.email_log_path = Path(email_log_path)
        self._escalations = escalations

    def _build_sync(self, escalation_ids: list[str], now: datetime) -> dict[str, Any]:
        memory = self.email_log_path.parent
        inbox = build_triage_inbox(
            _read_json(self.email_log_path),
            _read_json(memory / "calendar-log.json"),
            _read_json(memory / "jira-log.json"),
            escalation_ids,
            now=now,
        )
        _write_atomic(self.path, inbox)
        return inbox

    async def rebuild(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Rebuild and write the file; return what was written.

        An escalation lookup that fails filters nothing (the Google script's
        rule): the classifier's own duplicate checks still apply.
        """
        try:
            escalation_ids = list(await self._escalations())
        except Exception as exc:  # noqa: BLE001 - a missing filter must not stop the rebuild
            logger.warning("triage inbox: escalation lookup deferred: %s", type(exc).__name__)
            escalation_ids = []
        return await asyncio.to_thread(self._build_sync, escalation_ids, now or datetime.now(UTC))
