"""The ``email-log.json`` writer: merge, never truncate; atomic; bounded.

The dashboards (:mod:`robothor.engine.dashboards.data`) and the memory ingest
read ``email-log.json`` (schema ``email_log``,
:mod:`robothor.events.contract`), and the triage stages write their verdicts
into its entries. So the writer:

* **merges** -- an entry it did not create keeps every field, a known id is
  never re-added, and a new message in a known conversation resets the
  conversation's earliest entry for re-triage (unless it was answered in the
  last :data:`REPLY_COOLDOWN_SECONDS`), as the Google sync does;
* writes **atomically** -- a temp file in the same directory then
  ``os.replace``, under the same ``.email-log.lock`` the Google sync takes, so
  a reader never sees half a file and two writers never interleave;
* stays **bounded** -- past :data:`MAX_ENTRIES` the oldest fetched entries go.

The path is the ``workspace_email_log_path`` setting; empty means
``<workspace>/brain/memory/email-log.json``, the file the dashboards read.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ENTRIES",
    "REPLY_COOLDOWN_SECONDS",
    "EmailLog",
    "default_path",
    "merge_entries",
    "new_entry",
]

MAX_ENTRIES = 2000
REPLY_COOLDOWN_SECONDS = 300
_LOCK_NAME = ".email-log.lock"
_TRIAGE_FIELDS = (
    "readAt",
    "categorizedAt",
    "urgency",
    "category",
    "actionRequired",
    "actionCompletedAt",
    "pendingReviewAt",
    "reviewedAt",
)


def default_path() -> Path:
    """The configured email-log path (see the module doc)."""
    from robothor.settings import get_settings

    settings = get_settings()
    configured = (settings.workspace.workspace_email_log_path or "").strip()
    if configured:
        return Path(configured).expanduser()
    workspace = (settings.paths.workspace or "").strip()
    root = Path(workspace).expanduser() if workspace else Path.home() / "robothor"
    return root / "brain" / "memory" / "email-log.json"


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).isoformat()


def new_entry(
    *,
    message_id: str,
    thread_id: str | None,
    sender: str | None,
    subject: str | None,
    date: str | None,
    labels: Iterable[str],
    provider: str,
    fetched_at: datetime | None = None,
) -> dict[str, Any]:
    """A fresh entry: identifiers and envelope, every triage stage still empty."""
    entry: dict[str, Any] = {
        "id": message_id,
        "threadId": thread_id,
        "fetchedAt": _stamp(fetched_at),
        "readAt": None,
        "from": sender,
        "subject": subject,
        "date": date,
        "labels": list(labels),
        "snippet": None,
        "categorizedAt": None,
        "urgency": None,
        "category": None,
        "actionRequired": None,
        "actionCompletedAt": None,
        "pendingReviewAt": None,
        "reviewedAt": None,
        "messageCount": 1,
        "provider": provider,
    }
    return entry


def _recently_answered(entry: dict[str, Any], now: datetime) -> bool:
    completed = entry.get("actionCompletedAt")
    if not completed:
        return False
    try:
        when = datetime.fromisoformat(str(completed))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (now - when).total_seconds() < REPLY_COOLDOWN_SECONDS


def _thread_parent(entries: dict[str, Any], thread_id: str, exclude: str) -> str | None:
    """The earliest-fetched entry of ``thread_id`` other than ``exclude``."""
    best: tuple[str, str] | None = None
    for eid, entry in entries.items():
        if eid == exclude or not isinstance(entry, dict):
            continue
        if (entry.get("threadId") or eid) == thread_id or eid == thread_id:
            fetched = str(entry.get("fetchedAt") or "")
            if best is None or fetched < best[0]:
                best = (fetched, eid)
    return best[1] if best else None


def merge_entries(
    log: dict[str, Any],
    entries: Iterable[dict[str, Any]],
    *,
    now: datetime | None = None,
    max_entries: int = MAX_ENTRIES,
) -> list[str]:
    """Merge ``entries`` into ``log`` in place; return the ids that were added.

    Pure apart from mutating ``log``: no IO, so the merge rules are tested on
    their own.
    """
    now = now or datetime.now(UTC)
    book = log.setdefault("entries", {})
    if not isinstance(book, dict):
        book = log["entries"] = {}
    added: list[str] = []
    for entry in entries:
        eid = str(entry.get("id") or "")
        if not eid or eid in book:
            continue
        book[eid] = dict(entry)
        added.append(eid)
        thread = entry.get("threadId")
        if thread and thread != eid:
            parent = _thread_parent(book, str(thread), eid)
            if parent and not _recently_answered(book[parent], now):
                target = book[parent]
                for name in _TRIAGE_FIELDS:
                    target[name] = None
                target["resetByReplyId"] = eid
                target["resetAt"] = _stamp(now)
    if len(book) > max_entries:
        oldest = sorted(book, key=lambda k: str((book[k] or {}).get("fetchedAt") or ""))
        for eid in oldest[: len(book) - max_entries]:
            del book[eid]
    log["lastCheckedAt"] = _stamp(now)
    return added


class EmailLog:
    """``email-log.json`` at ``path`` (default: :func:`default_path`)."""

    def __init__(self, path: Path | str | None = None, *, max_entries: int = MAX_ENTRIES) -> None:
        self.path = Path(path) if path is not None else default_path()
        self.max_entries = max_entries

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"lastCheckedAt": None, "entries": {}}
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (ValueError, OSError):
            data = None
        if not isinstance(data, dict):
            # Keep the unreadable file for a human; start a fresh log.
            stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
            backup = self.path.with_suffix(f".corrupt.{stamp}.json")
            with contextlib.suppress(OSError):
                self.path.rename(backup)
            logger.warning("email-log: unreadable %s moved aside", self.path.name)
            return {"lastCheckedAt": None, "entries": {}}
        data.setdefault("entries", {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".email-log.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp).replace(self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
            raise

    def merge_sync(
        self, entries: list[dict[str, Any]], *, now: datetime | None = None
    ) -> list[str]:
        """Blocking merge under the shared lock. Returns the added ids."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (self.path.parent / _LOCK_NAME).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = self._load()
                added = merge_entries(data, entries, now=now, max_entries=self.max_entries)
                self._save(data)
                return added
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    async def merge(
        self, entries: list[dict[str, Any]], *, now: datetime | None = None
    ) -> list[str]:
        return await asyncio.to_thread(self.merge_sync, entries, now=now)
