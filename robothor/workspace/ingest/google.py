"""Gmail ingestion over the history API, woken by push.

:class:`GmailHistoryIngestor` follows the assistant's Gmail inbox with
``users.history.list`` (``historyTypes=messageAdded``, ``labelId=INBOX``) from
a saved ``historyId`` and publishes ``email.new`` for each new unread inbox
message, after merging it into ``email-log.json`` and logging it to the CRM,
exactly as the Microsoft 365 mail ingestor does. The push consumer
(:mod:`~robothor.workspace.ingest.gmail_push`) runs a round whenever Gmail
says the mailbox changed.

**The cursor** is the mailbox ``historyId``, kept in the sync state's
``delta_link`` column. A round reads every history page after it, and saves
the ``historyId`` the last page reports once everything new is published.

**The seen set decides what is new**, as for Microsoft 365: a message id in it
(``mail:<id>``) is never published again, so a round that dies part-way and
re-reads the same history publishes only the rest.

**No replay.** The first round, and a round whose cursor Gmail no longer has
(``history.list`` answers 404 once a ``historyId`` is too old), publish
nothing: they start again at the profile's current ``historyId``. Mail that
arrived while the cursor was lost is left as history, the price of never
re-announcing a week of old mail.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from robothor.events import contract
from robothor.workspace.errors import NotFound, WorkspaceError
from robothor.workspace.ingest.base import Ingestor, IngestReport, Publisher
from robothor.workspace.ingest.crm import InteractionLogger, log_new_mail
from robothor.workspace.ingest.email_log import EmailLog, new_entry
from robothor.workspace.ingest.state import IngestStore, SyncState

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_PAGES",
    "PROVIDER",
    "GmailHistoryApi",
    "GmailHistoryIngestor",
    "GwsGmailHistory",
]

PROVIDER = "google"
#: Upper bound on history pages one round follows.
MAX_PAGES = 200


class GmailHistoryApi(Protocol):
    """The four Gmail calls push ingest makes. Errors raise; a 404 is :class:`NotFound`."""

    async def profile(self) -> dict[str, Any]: ...
    async def history(
        self, start_history_id: str, page_token: str | None = None
    ) -> dict[str, Any]: ...
    async def message(self, message_id: str) -> dict[str, Any]: ...
    async def watch(self, topic: str, label_ids: list[str]) -> dict[str, Any]: ...


def _checked(result: Any, operation: str) -> dict[str, Any]:
    """A gws answer, or the error it carried as a :class:`WorkspaceError`."""
    if not isinstance(result, dict):
        raise WorkspaceError(f"gmail {operation}: unexpected answer")
    if "error" not in result:
        return result
    text = f"{result.get('error')}".lower()
    hint = str(result.get("hint") or "failed")
    if "404" in text or "not found" in text:
        raise NotFound(f"gmail {operation}: not found", status=404, code="not_found")
    raise WorkspaceError(f"gmail {operation}: {hint}")


class GwsGmailHistory:
    """:class:`GmailHistoryApi` over the gws CLI (the Google mail adapter), off the loop."""

    def __init__(self) -> None:
        from robothor.workspace.google.adapter import GoogleMailSync

        self._sync = GoogleMailSync()

    async def profile(self) -> dict[str, Any]:
        return _checked(await asyncio.to_thread(self._sync.get_profile), "profile")

    async def history(self, start_history_id: str, page_token: str | None = None) -> dict[str, Any]:
        result = await asyncio.to_thread(
            self._sync.list_history, start_history_id, page_token=page_token
        )
        return _checked(result, "history")

    async def message(self, message_id: str) -> dict[str, Any]:
        result = await asyncio.to_thread(self._sync.get_message, message_id, fmt="metadata")
        return _checked(result, "message")

    async def watch(self, topic: str, label_ids: list[str]) -> dict[str, Any]:
        result = await asyncio.to_thread(self._sync.watch, topic, label_ids=label_ids)
        return _checked(result, "watch")


def _key(message_id: str) -> str:
    return f"mail:{message_id}"


class GmailHistoryIngestor(Ingestor):
    """The assistant's Gmail inbox -> ``email.new`` + ``email-log.json``, by history id."""

    provider = PROVIDER
    resource = "mail"

    def __init__(
        self,
        *,
        gmail: GmailHistoryApi,
        mailbox: str,
        tenant_id: str,
        store: IngestStore,
        publish: Publisher,
        email_log: EmailLog | None = None,
        log_interaction: InteractionLogger | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        mailbox = (mailbox or "").strip()
        if not mailbox:
            raise ValueError("gmail ingest needs a mailbox")
        if not tenant_id:
            raise ValueError("explicit platform tenant required")
        self.gmail = gmail
        self.mailbox = mailbox.lower()
        self.tenant_id = tenant_id
        self.store = store
        self.publish = publish
        self.email_log = email_log
        self.log_interaction = log_interaction
        self._now = now or (lambda: datetime.now(UTC))

    async def run_once(self) -> IngestReport:
        state = await self.store.load(self.tenant_id, PROVIDER, self.mailbox, self.resource)
        if state is None or not state.delta_link:
            return await self._reset(state, "initial" if state is None else "resync")
        try:
            stubs, cursor = await self._read(state.delta_link)
        except NotFound:
            logger.warning("gmail history cursor expired; starting again without replay")
            return await self._reset(state, "resync")
        report = IngestReport(resource=self.resource, mode="incremental", read=len(stubs))
        await self._process(stubs, report)
        await self.store.save(state.with_(delta_link=cursor))
        return report

    async def _reset(self, state: SyncState | None, mode: str) -> IngestReport:
        profile = await self.gmail.profile()
        history_id = str(profile.get("historyId") or "")
        if not history_id:
            raise WorkspaceError("gmail profile carried no historyId")
        base = state or SyncState(self.tenant_id, PROVIDER, self.mailbox, self.resource)
        await self.store.save(base.with_(delta_link=history_id, initialized_at=self._now()))
        return IngestReport(resource=self.resource, mode=mode)

    async def _read(self, start: str) -> tuple[list[dict[str, Any]], str]:
        """Every added message after ``start``, in history order, and the new cursor."""
        stubs: dict[str, dict[str, Any]] = {}
        cursor = start
        token: str | None = None
        for _ in range(MAX_PAGES):
            page = await self.gmail.history(start, token)
            for record in page.get("history") or []:
                if not isinstance(record, dict):
                    continue
                for added in record.get("messagesAdded") or []:
                    message = added.get("message") if isinstance(added, dict) else None
                    if isinstance(message, dict) and message.get("id"):
                        stubs.setdefault(str(message["id"]), message)
            cursor = str(page.get("historyId") or cursor)
            token = page.get("nextPageToken") or None
            if not token:
                return list(stubs.values()), cursor
        raise WorkspaceError(f"gmail history did not finish within {MAX_PAGES} pages")

    async def _process(self, stubs: list[dict[str, Any]], report: IngestReport) -> None:
        already = await self.store.seen(
            self.tenant_id, PROVIDER, [_key(str(s["id"])) for s in stubs]
        )
        history: list[str] = []
        entries: list[dict[str, Any]] = []
        for stub in stubs:
            mid = str(stub["id"])
            if _key(mid) in already:
                continue
            labels = stub.get("labelIds")
            if isinstance(labels, list) and ("INBOX" not in labels or "DRAFT" in labels):
                history.append(_key(mid))
                continue
            try:
                raw = await self.gmail.message(mid)
            except NotFound:
                history.append(_key(mid))  # gone before we could read it
                continue
            entry = self._entry(raw)
            if "UNREAD" not in entry["labels"] or "INBOX" not in entry["labels"]:
                history.append(_key(mid))
                continue
            entries.append(entry)
        if history:
            await self.store.mark_seen(self.tenant_id, PROVIDER, history)
        report.crm_logged = await log_new_mail(
            entries,
            log_interaction=self.log_interaction,
            seen=lambda keys: self.store.seen(self.tenant_id, PROVIDER, keys),
            mark=lambda keys: self.store.mark_seen(self.tenant_id, PROVIDER, keys),
            stamp=lambda: self._now().isoformat(),
            label="gmail",
        )
        if entries and self.email_log is not None:
            report.logged = len(await self.email_log.merge(entries, now=self._now()))
        for entry in entries:
            await self.publish(
                contract.EMAIL_STREAM, contract.EMAIL_NEW, contract.email_new_payload(entry)
            )
            await self.store.mark_seen(self.tenant_id, PROVIDER, [_key(str(entry["id"]))])
            report.published += 1

    def _entry(self, raw: dict[str, Any]) -> dict[str, Any]:
        from robothor.workspace.google import gmail_parse

        envelope = gmail_parse._shape_envelope(raw)
        return new_entry(
            message_id=envelope["id"],
            thread_id=envelope["thread_id"] or None,
            sender=envelope["from"] or None,
            subject=envelope["subject"] or None,
            date=envelope["date"] or None,
            labels=envelope["labels"],
            provider=PROVIDER,
            fetched_at=self._now(),
        )
