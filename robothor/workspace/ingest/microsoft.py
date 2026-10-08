"""Microsoft 365 ingestion over Microsoft Graph delta queries.

:class:`GraphMailIngestor` follows the assistant's inbox
(``/users/{assistant}/mailFolders/inbox/messages/delta``) and publishes
``email.new`` for each new unread message, after merging it into
``email-log.json``. :class:`GraphCalendarIngestor` follows the owner's
calendar (``/users/{owner}/calendarView/delta`` over a window fixed when the
delta starts: one day back to thirty ahead, renewed daily) and publishes
``calendar.new`` / ``.modified`` / ``.rescheduled`` / ``.cancellation``.

**A lost delta never replays old mail.** Exchange can throw a delta away
(``410 syncStateNotFound`` and friends). The ingestor then starts a new one,
which answers the whole folder again -- and publishes from it only items that
are newer than the high-water mark (the newest item it had processed) AND
whose id it has not seen. The first ever sync publishes at most
:data:`INITIAL_PUBLISH_LIMIT` unread messages from the last week, then
everything else is history.

A message is mapped with the same translation the mail tools use
(:func:`robothor.workspace.microsoft.mail.gmail_envelope`), so the event, the
log entry and a later ``gws_gmail_get`` agree on ``from``, ``date`` and labels.

Order of effects in a round: read every page, merge new mail into the log,
publish each event and mark it seen, then save the delta position. A crash
part-way re-reads the same changes next round; the seen set makes that a no-op.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from robothor.events import contract
from robothor.workspace.errors import NotFound, WorkspaceError
from robothor.workspace.ingest.base import Ingestor, IngestReport, Publisher
from robothor.workspace.ingest.crm import InteractionLogger, crm_key, interaction_payload
from robothor.workspace.ingest.email_log import EmailLog, new_entry
from robothor.workspace.ingest.state import IngestStore, SyncState
from robothor.workspace.microsoft.mail import ENVELOPE_FIELDS, gmail_envelope

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.workspace.microsoft.graph import GraphClient

logger = logging.getLogger(__name__)

__all__ = [
    "CALENDAR_REINIT_AFTER",
    "INITIAL_PUBLISH_LIMIT",
    "PAGE_SIZE",
    "PROVIDER",
    "GraphCalendarIngestor",
    "GraphMailIngestor",
    "needs_resync",
]

PROVIDER = "microsoft365"
PAGE_SIZE = 50
#: Most unread messages the very first sync publishes (the Google sync reads 20).
INITIAL_PUBLISH_LIMIT = 20
INITIAL_LOOKBACK = timedelta(days=7)
#: How far before the high-water mark a resync re-reads; older mail is excluded
#: by the query itself, newer-but-seen mail by the seen set.
RESYNC_LOOKBACK = timedelta(days=1)
CALENDAR_BACK = timedelta(days=1)
CALENDAR_AHEAD = timedelta(days=30)
CALENDAR_REINIT_AFTER = timedelta(days=1)
#: Upper bound on pages one round follows (50 items each).
MAX_PAGES = 400

_RESYNC_CODES = frozenset({"syncstatenotfound", "syncstateinvalid", "resyncrequired"})
_PREFER_PAGE = f"odata.maxpagesize={PAGE_SIZE}"


def needs_resync(exc: BaseException) -> bool:
    """Is this Graph's "your delta is gone, start again"?"""
    if not isinstance(exc, WorkspaceError):
        return False
    return exc.status == 410 or (exc.code or "").lower() in _RESYNC_CODES


def _parse(stamp: Any) -> datetime | None:
    if not stamp:
        return None
    text = str(stamp).strip().replace("Z", "+00:00")
    head, dot, frac = text.partition(".")
    if dot:
        # Graph writes 7 fractional digits; fromisoformat takes at most 6.
        digits = "".join(c for c in frac if c.isdigit())
        tail = frac[len(digits) :]
        text = f"{head}.{digits[:6]}{tail}"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _later(first: datetime | None, second: datetime | None) -> datetime | None:
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


class _GraphDeltaIngestor(Ingestor):
    """The delta mechanics both resources share: pages, links, resync, state."""

    provider = PROVIDER
    resource = ""

    def __init__(
        self,
        *,
        graph: GraphClient,
        mailbox: str,
        tenant_id: str,
        store: IngestStore,
        publish: Publisher,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        mailbox = (mailbox or "").strip()
        if not mailbox:
            raise ValueError(f"microsoft365 {self.resource} ingest needs a mailbox")
        if not tenant_id:
            raise ValueError("explicit platform tenant required")
        self.graph = graph
        self.mailbox = mailbox.lower()
        self.tenant_id = tenant_id
        self.store = store
        self.publish = publish
        self._now = now or (lambda: datetime.now(UTC))
        self._base = f"/users/{quote(self.mailbox, safe='@')}"

    # ── delta plumbing ──────────────────────────────────────────────────

    async def _first_page(self, state: SyncState | None, mode: str) -> dict[str, Any]:
        raise NotImplementedError

    async def _read(self, state: SyncState | None, mode: str) -> tuple[list[dict[str, Any]], str]:
        """Every item of one delta round and the deltaLink that ends it."""
        headers = {"Prefer": _PREFER_PAGE}
        if mode == "incremental" and state is not None:
            page = await self.graph.get_link(state.delta_link, headers=headers)
        else:
            page = await self._first_page(state, mode)
        items: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            values = page.get("value")
            if not isinstance(values, list):
                raise WorkspaceError("graph delta page carried no value list")
            items.extend(v for v in values if isinstance(v, dict) and v.get("id"))
            delta = page.get("@odata.deltaLink")
            if delta:
                return items, str(delta)
            nxt = page.get("@odata.nextLink")
            if not nxt:
                raise WorkspaceError("graph delta page carried neither a nextLink nor a deltaLink")
            page = await self.graph.get_link(str(nxt), headers=headers)
        raise WorkspaceError(f"graph delta did not finish within {MAX_PAGES} pages")

    def _mode(self, state: SyncState | None) -> str:
        if state is None:
            return "initial"
        return "incremental" if state.delta_link else "resync"

    async def run_once(self) -> IngestReport:
        state = await self.store.load(self.tenant_id, PROVIDER, self.mailbox, self.resource)
        mode = self._mode(state)
        try:
            items, delta_link = await self._read(state, mode)
        except WorkspaceError as exc:
            if mode != "incremental" or not needs_resync(exc):
                raise
            logger.warning(
                "microsoft365 %s delta expired (%s); starting a new one without replay",
                self.resource,
                exc.code or exc.status,
            )
            mode = "resync"
            items, delta_link = await self._read(state, mode)
        report = IngestReport(resource=self.resource, mode=mode, read=len(items))
        high_water = await self._process(state, mode, items, report)
        now = self._now()
        base = state or SyncState(self.tenant_id, PROVIDER, self.mailbox, self.resource)
        await self.store.save(
            base.with_(
                delta_link=delta_link,
                high_water=high_water,
                initialized_at=now if mode != "incremental" else (base.initialized_at or now),
            )
        )
        return report

    async def _process(
        self,
        state: SyncState | None,
        mode: str,
        items: list[dict[str, Any]],
        report: IngestReport,
    ) -> datetime | None:
        raise NotImplementedError

    async def _seen(self, keys: list[str]) -> set[str]:
        return await self.store.seen(self.tenant_id, PROVIDER, keys)

    async def _mark(self, keys: list[str]) -> None:
        if keys:
            await self.store.mark_seen(self.tenant_id, PROVIDER, keys)


class GraphMailIngestor(_GraphDeltaIngestor):
    """The assistant's Exchange inbox -> ``email.new`` + ``email-log.json``."""

    resource = "mail"

    def __init__(
        self,
        *,
        email_log: EmailLog | None = None,
        log_interaction: InteractionLogger | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.email_log = email_log
        self.log_interaction = log_interaction

    async def _first_page(self, state: SyncState | None, mode: str) -> dict[str, Any]:
        now = self._now()
        if mode == "resync" and state is not None and state.high_water is not None:
            floor = min(state.high_water, now) - RESYNC_LOOKBACK
        else:
            floor = now - INITIAL_LOOKBACK
        params = {
            "$select": ",".join(ENVELOPE_FIELDS),
            "$filter": f"receivedDateTime ge {_iso(floor)}",
        }
        return await self.graph.get(
            f"{self._base}/mailFolders/inbox/messages/delta",
            params,
            headers={"Prefer": _PREFER_PAGE},
        )

    def _entry(self, message: dict[str, Any]) -> dict[str, Any]:
        folders = {"inbox": str(message.get("parentFolderId") or "")}
        envelope = gmail_envelope(message, folders)
        return new_entry(
            message_id=envelope["id"],
            thread_id=envelope["thread_id"] or None,
            sender=envelope["from"] or None,
            subject=envelope["subject"],
            date=envelope["date"] or None,
            labels=envelope["labels"],
            provider=PROVIDER,
            fetched_at=self._now(),
        )

    async def _process(
        self,
        state: SyncState | None,
        mode: str,
        items: list[dict[str, Any]],
        report: IngestReport,
    ) -> datetime | None:
        high_water = state.high_water if state is not None else None
        messages = [
            m for m in items if "@removed" not in m and not m.get("isDraft") and m.get("id")
        ]
        messages.sort(
            key=lambda m: _parse(m.get("receivedDateTime")) or datetime.min.replace(tzinfo=UTC)
        )
        keys = {str(m["id"]): f"mail:{m['id']}" for m in messages}
        already = await self._seen(list(keys.values()))

        publish: list[dict[str, Any]] = []
        history: list[str] = []
        newest = high_water
        for message in messages:
            key = keys[str(message["id"])]
            received = _parse(message.get("receivedDateTime"))
            newest = _later(newest, received)
            if key in already:
                continue
            unread = not message.get("isRead", False)
            if mode == "initial":
                if unread:
                    publish.append(message)
                else:
                    history.append(key)
                continue
            fresh = received is not None and (high_water is None or received > high_water)
            if unread and fresh:
                publish.append(message)
            else:
                if not fresh:
                    report.held_back += 1
                history.append(key)
        if mode == "initial" and len(publish) > INITIAL_PUBLISH_LIMIT:
            older, publish = publish[:-INITIAL_PUBLISH_LIMIT], publish[-INITIAL_PUBLISH_LIMIT:]
            history.extend(keys[str(m["id"])] for m in older)

        await self._mark(history)
        entries = [self._entry(m) for m in publish]
        await self._log_to_crm(entries, report)
        if entries and self.email_log is not None:
            report.logged = len(await self.email_log.merge(entries, now=self._now()))
        for message, entry in zip(publish, entries, strict=True):
            payload = contract.email_new_payload(entry)
            await self.publish(contract.EMAIL_STREAM, contract.EMAIL_NEW, payload)
            await self._mark([keys[str(message["id"])]])
            report.published += 1
        return newest

    async def _log_to_crm(self, entries: list[dict[str, Any]], report: IngestReport) -> None:
        """One CRM interaction per new message, before it is published.

        Before, so a round that dies between the two re-publishes next round
        without logging again (the ``crm:`` key is already seen). A CRM failure
        is logged by type and never holds the message back from the pipeline.
        """
        if self.log_interaction is None or not entries:
            return
        keys = {str(e["id"]): crm_key(str(e["id"])) for e in entries}
        done = await self._seen(list(keys.values()))
        for entry in entries:
            key = keys[str(entry["id"])]
            payload = interaction_payload(entry)
            if key in done or payload is None:
                continue
            try:
                logged = await self.log_interaction(payload)
            except Exception as exc:  # noqa: BLE001 - the CRM must never stop ingestion
                logger.warning("microsoft365 mail CRM log deferred: %s", type(exc).__name__)
                continue
            if logged:
                await self._mark([key])
                entry["crmLoggedAt"] = self._now().isoformat()
                report.crm_logged += 1


class GraphCalendarIngestor(_GraphDeltaIngestor):
    """The owner's Exchange calendar -> ``calendar.*`` change events."""

    resource = "calendar"

    def _mode(self, state: SyncState | None) -> str:
        mode = super()._mode(state)
        if mode == "incremental" and state is not None:
            started = state.initialized_at
            if started is None or self._now() - started >= CALENDAR_REINIT_AFTER:
                return "resync"  # a new window, fixed from now
        return mode

    async def _first_page(self, state: SyncState | None, mode: str) -> dict[str, Any]:
        now = self._now()
        params = {
            "startDateTime": _iso(now - CALENDAR_BACK),
            "endDateTime": _iso(now + CALENDAR_AHEAD),
        }
        return await self.graph.get(
            f"{self._base}/calendarView/delta", params, headers={"Prefer": _PREFER_PAGE}
        )

    @staticmethod
    def _when(part: Any) -> str:
        if not isinstance(part, dict):
            return ""
        parsed = _parse(part.get("dateTime"))
        return _iso(parsed) if parsed is not None else str(part.get("dateTime") or "")

    def _payload(self, event: dict[str, Any], change: str) -> dict[str, Any]:
        attendees = [
            str((a.get("emailAddress") or {}).get("address") or "")
            for a in event.get("attendees") or []
            if isinstance(a, dict)
        ]
        online = event.get("onlineMeeting") or {}
        join = (online.get("joinUrl") if isinstance(online, dict) else "") or event.get(
            "onlineMeetingUrl"
        )
        return {
            "id": str(event["id"]),
            "change_type": change,
            "title": str(event.get("subject") or ""),
            "start": self._when(event.get("start")),
            "end": self._when(event.get("end")),
            "attendees": [a for a in attendees if a],
            "hangoutLink": str(join or ""),
            "provider": PROVIDER,
        }

    def _keys(self, event: dict[str, Any]) -> dict[str, str]:
        eid = str(event["id"])
        return {
            "id": f"event:{eid}",
            "version": f"event:{eid}:v:{event.get('changeKey') or event.get('lastModifiedDateTime') or ''}",
            "slot": f"event:{eid}:t:{self._when(event.get('start'))}/{self._when(event.get('end'))}",
            "cancelled": f"event:{eid}:cancelled",
        }

    async def _emit(self, event: dict[str, Any], change: str, keys: list[str]) -> None:
        await self.publish(
            contract.CALENDAR_STREAM,
            contract.CALENDAR_EVENT_TYPES[change],
            self._payload(event, change),
        )
        await self._mark(keys)

    async def _process(
        self,
        state: SyncState | None,
        mode: str,
        items: list[dict[str, Any]],
        report: IngestReport,
    ) -> datetime | None:
        high_water = state.high_water if state is not None else None
        newest = high_water
        for item in items:
            if "@removed" in item:
                await self._removed(str(item["id"]), report)
                continue
            keys = self._keys(item)
            modified = _parse(item.get("lastModifiedDateTime"))
            newest = _later(newest, modified)
            already = await self._seen(list(keys.values()))
            if keys["version"] in already:
                continue
            if mode == "initial":
                # The first sync is the baseline: the calendar as it stands is
                # not a stream of changes.
                await self._mark([keys["id"], keys["version"], keys["slot"]])
                continue
            if mode == "resync" and (
                modified is None or (high_water is not None and modified <= high_water)
            ):
                report.held_back += 1
                await self._mark([keys["id"], keys["version"], keys["slot"]])
                continue
            if item.get("isCancelled"):
                if keys["cancelled"] in already:
                    await self._mark([keys["version"]])
                    continue
                change, marks = "cancelled", [keys["id"], keys["version"], keys["cancelled"]]
            elif keys["id"] not in already:
                change, marks = "created", [keys["id"], keys["version"], keys["slot"]]
            elif keys["slot"] not in already:
                change, marks = "rescheduled", [keys["version"], keys["slot"]]
            else:
                change, marks = "updated", [keys["version"]]
            await self._emit(item, change, marks)
            report.published += 1
        return newest

    async def _removed(self, event_id: str, report: IngestReport) -> None:
        """A delta removal: deleted, cancelled, or moved out of the window -- read to know."""
        known, cancelled = f"event:{event_id}", f"event:{event_id}:cancelled"
        already = await self._seen([known, cancelled])
        if known not in already or cancelled in already:
            return
        try:
            event = await self.graph.get(f"{self._base}/events/{quote(event_id, safe='')}")
        except NotFound:
            event = {"id": event_id}
            await self._emit(event, "cancelled", [cancelled])
            report.published += 1
            return
        if event.get("isCancelled"):
            await self._emit(event, "cancelled", [cancelled])
        else:
            keys = self._keys(event)
            await self._emit(event, "rescheduled", [keys["version"], keys["slot"]])
        report.published += 1
