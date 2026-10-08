"""Graph delta queries behind :class:`~robothor.workspace.tests.fake_graph.FakeGraphTenant`.

:func:`install_ingest` registers the two delta routes the ingest worker uses on
top of the mail fake (:func:`~robothor.workspace.tests.fake_graph_mail.install_mail`)
and returns a :class:`FakeDelta` for seeding and inspecting state:

* ``GET /users/{mailbox}/mailFolders/inbox/messages/delta`` -- the inbox's
  changes since a token. A first call (no token) answers every inbox message
  (``$filter=receivedDateTime ge ...`` honoured), paged by
  ``Prefer: odata.maxpagesize``; the last page carries an
  ``@odata.deltaLink`` whose ``$deltatoken`` is the change sequence it covers.
  A later call with that token answers only what changed after it, deletions
  as ``{"id", "@removed": {"reason": "deleted"}}``.
* ``GET /users/{mailbox}/calendarView/delta?startDateTime=&endDateTime=`` --
  the same over the events overlapping the window, which the token remembers.
* ``GET /users/{mailbox}/events/{id}`` -- one event, 404 once deleted.

:meth:`FakeDelta.expire_tokens` makes every token issued so far answer
``410 SyncStateNotFound``, which is how Exchange reports a delta it no longer
holds -- the replay-safety tests are built on it.

Every change goes through :class:`FakeDelta` (``deliver``, ``mark_read``,
``remove_message``, ``add_event``, ``update_event``, ``cancel_event``,
``delete_event``), which stamps it with the next change sequence.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import httpx

from robothor.workspace.tests.fake_graph import GRAPH_ORIGIN, FakeGraphTenant, graph_error

if TYPE_CHECKING:
    from robothor.workspace.tests.fake_graph_mail import FakeExchangeMail

__all__ = ["FakeDelta", "install_ingest"]

_DEFAULT_PAGE = 10


def _addr(address: str) -> dict[str, Any]:
    return {"emailAddress": {"name": address, "address": address}, "type": "required"}


def _when(stamp: str) -> datetime:
    when = datetime.fromisoformat(stamp)
    return when if when.tzinfo else when.replace(tzinfo=UTC)


@dataclass
class FakeDelta:
    """Change sequences, tokens and events for the delta routes."""

    tenant: FakeGraphTenant
    mail: FakeExchangeMail
    seq: Any = field(default_factory=lambda: itertools.count(1))
    #: (mailbox, id) -> the change sequence of its last change.
    changed: dict[tuple[str, str], int] = field(default_factory=dict)
    #: (mailbox, id) -> the sequence at which it was deleted.
    removed: dict[tuple[str, str], int] = field(default_factory=dict)
    #: Tokens carry the generation they were issued in; an older one answers 410.
    generation: int = 0
    last: int = 0
    _event_ids: Any = field(default_factory=lambda: itertools.count(1))

    def _bump(self, mailbox: str, item_id: str) -> int:
        self.last = next(self.seq)
        self.changed[(mailbox.lower(), item_id)] = self.last
        return self.last

    # ── mail ────────────────────────────────────────────────────────────

    def deliver(self, mailbox: str, **kwargs: Any) -> dict[str, Any]:
        message = self.mail.deliver(mailbox, **kwargs)
        self._bump(mailbox, message["id"])
        return message

    def mark_read(self, mailbox: str, message_id: str) -> None:
        self.mail.message(mailbox, message_id)["isRead"] = True
        self._bump(mailbox, message_id)

    def remove_message(self, mailbox: str, message_id: str) -> None:
        box = self.mail.box(mailbox)
        box.messages[:] = [m for m in box.messages if m["id"] != message_id]
        self.removed[(mailbox.lower(), message_id)] = self._bump(mailbox, message_id)

    def expire_tokens(self) -> None:
        """Every token issued so far now answers 410 SyncStateNotFound."""
        self.generation += 1

    # ── calendar ────────────────────────────────────────────────────────

    def events(self, mailbox: str) -> list[dict[str, Any]]:
        return self.tenant.mailbox(mailbox).collections["events"]

    def event(self, mailbox: str, event_id: str) -> dict[str, Any]:
        for event in self.events(mailbox):
            if event["id"] == event_id:
                return event
        raise KeyError(event_id)

    def add_event(
        self,
        mailbox: str,
        *,
        subject: str = "Meeting",
        start: str,
        end: str,
        attendees: tuple[str, ...] = (),
        join_url: str = "",
    ) -> dict[str, Any]:
        stamp = self.mail.tick()
        n = next(self._event_ids)
        event: dict[str, Any] = {
            "id": f"AAMkEvt-{n:04d}+Zz/q==",
            "subject": subject,
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
            "attendees": [_addr(a) for a in attendees],
            "isCancelled": False,
            "changeKey": f"ck-{n}-1",
            "createdDateTime": stamp,
            "lastModifiedDateTime": stamp,
            "onlineMeeting": {"joinUrl": join_url} if join_url else None,
            "type": "singleInstance",
        }
        self.events(mailbox).append(event)
        self._bump(mailbox, event["id"])
        return event

    def update_event(self, mailbox: str, event_id: str, **changes: Any) -> dict[str, Any]:
        event = self.event(mailbox, event_id)
        for key, value in changes.items():
            if key in ("start", "end"):
                event[key] = {"dateTime": value, "timeZone": "UTC"}
            else:
                event[key] = value
        version = int(event["changeKey"].rsplit("-", 1)[1]) + 1
        event["changeKey"] = event["changeKey"].rsplit("-", 1)[0] + f"-{version}"
        event["lastModifiedDateTime"] = self.mail.tick()
        self._bump(mailbox, event_id)
        return event

    def cancel_event(self, mailbox: str, event_id: str) -> dict[str, Any]:
        return self.update_event(mailbox, event_id, isCancelled=True)

    def delete_event(self, mailbox: str, event_id: str) -> None:
        box = self.events(mailbox)
        box[:] = [e for e in box if e["id"] != event_id]
        self.removed[(mailbox.lower(), event_id)] = self._bump(mailbox, event_id)

    # ── inspection ──────────────────────────────────────────────────────

    def delta_requests(self, resource: str = "mail") -> list[httpx.Request]:
        suffix = (
            "/mailFolders/inbox/messages/delta" if resource == "mail" else "/calendarView/delta"
        )
        return [r for r in self.tenant.requests if r.url.path.endswith(suffix)]


def _page_size(request: httpx.Request) -> int:
    prefer = request.headers.get("prefer", "")
    for item in prefer.split(","):
        name, _, value = item.strip().partition("=")
        if name.strip().lower() == "odata.maxpagesize":
            return int(value.strip().strip('"'))
    return _DEFAULT_PAGE


def install_ingest(tenant: FakeGraphTenant, mail: FakeExchangeMail) -> FakeDelta:
    """Register the delta routes on ``tenant``; return the state helper."""
    state = FakeDelta(tenant, mail)
    mailbox_rx = r"/users/(?P<mailbox>[^/]+)"

    def cursor(request: httpx.Request) -> tuple[int | None, int, int] | httpx.Response:
        """``(since, upto, offset)`` for this request, or the 410 for an expired token.

        ``since`` is None on a first call. A nextLink carries all three in its
        ``$skiptoken`` so every page of one round answers the same snapshot.
        """
        params = request.url.params
        skip = params.get("$skiptoken")
        if skip is not None:
            since_s, upto_s, offset_s = skip.split(":")
            return (None if since_s == "-" else int(since_s)), int(upto_s), int(offset_s)
        token = params.get("$deltatoken")
        if token is None:
            return None, state.last, 0
        generation, _, seq = token.partition(".")
        if int(generation) != state.generation:
            return graph_error(410, "SyncStateNotFound", "The sync state generation is not found.")
        return int(seq), state.last, 0

    def answer(
        request: httpx.Request,
        items: list[tuple[str, dict[str, Any] | None]],
        position: tuple[int | None, int, int],
        base_path: str,
        carry: dict[str, str],
    ) -> httpx.Response:
        """Page ``items`` ((id, item-or-None-for-removed)) with skip/delta links."""
        since, upto, offset = position
        size = _page_size(request)
        page = items[offset : offset + size]
        value: list[dict[str, Any]] = []
        for item_id, item in page:
            if item is None:
                value.append({"id": item_id, "@removed": {"reason": "deleted"}})
            else:
                value.append(json.loads(json.dumps(item)))
        body: dict[str, Any] = {"value": value}
        link = httpx.URL(f"{GRAPH_ORIGIN}/v1.0{base_path}")
        if offset + size < len(items):
            marker = "-" if since is None else str(since)
            nxt = link.copy_merge_params(
                {**carry, "$skiptoken": f"{marker}:{upto}:{offset + size}"}
            )
            body["@odata.nextLink"] = str(nxt)
        else:
            token = f"{state.generation}.{upto}"
            body["@odata.deltaLink"] = str(link.copy_merge_params({**carry, "$deltatoken": token}))
        return httpx.Response(200, json=body)

    @tenant.route("GET", mailbox_rx + r"/mailFolders/inbox/messages/delta")
    async def mail_delta(tenant_, request, match):
        address = unquote(match["mailbox"]).lower()
        position = cursor(request)
        if isinstance(position, httpx.Response):
            return position
        start, upto, _offset = position
        params = request.url.params
        inbox = mail.folder_id(address, "inbox")
        floor = None
        flt = params.get("$filter")
        if flt:
            field_name, op, value = flt.split(" ", 2)
            if field_name != "receivedDateTime" or op not in ("ge", "gt"):
                return graph_error(400, "BadRequest", "Unsupported filter on delta.")
            floor = _when(value)
        select = params.get("$select")
        items: list[tuple[str, dict[str, Any] | None]] = []
        for message in mail.box(address).messages:
            seq = state.changed.get((address, message["id"]), 0)
            if message["parentFolderId"] != inbox or seq > upto:
                continue
            if start is not None and seq <= start:
                continue
            if floor is not None and _when(message["receivedDateTime"]) < floor:
                continue
            item = dict(message)
            item.pop("attachments", None)
            item.pop("body", None)
            if select:
                keep = {s.strip() for s in select.split(",")} | {"id"}
                item = {k: v for k, v in item.items() if k in keep}
            items.append((message["id"], item))
        if start is not None:
            for (box, item_id), seq in state.removed.items():
                if box == address and start < seq <= upto:
                    items.append((item_id, None))
        carry = {k: v for k, v in params.items() if k in ("$select", "$filter")}
        path = f"/users/{match['mailbox']}/mailFolders/inbox/messages/delta"
        return answer(request, items, position, path, carry)

    @tenant.route("GET", mailbox_rx + r"/calendarView/delta")
    async def calendar_delta(tenant_, request, match):
        address = unquote(match["mailbox"]).lower()
        position = cursor(request)
        if isinstance(position, httpx.Response):
            return position
        start, upto, _offset = position
        params = request.url.params
        window_start, window_end = params.get("startDateTime"), params.get("endDateTime")
        if start is None and (not window_start or not window_end):
            return graph_error(400, "BadRequest", "startDateTime and endDateTime are required.")
        lo, hi = _when(window_start or ""), _when(window_end or "")
        items: list[tuple[str, dict[str, Any] | None]] = []
        for event in state.events(address):
            seq = state.changed.get((address, event["id"]), 0)
            if seq > upto or (start is not None and seq <= start):
                continue
            begins, ends = _when(event["start"]["dateTime"]), _when(event["end"]["dateTime"])
            if ends <= lo or begins >= hi:
                # Out of the window: a change that moved it out reads as removed.
                if start is not None:
                    items.append((event["id"], None))
                continue
            items.append((event["id"], event))
        if start is not None:
            for (box, item_id), seq in state.removed.items():
                if box == address and start < seq <= upto:
                    items.append((item_id, None))
        # The token remembers the window, as Graph's does.
        carry = {"startDateTime": window_start or "", "endDateTime": window_end or ""}
        path = f"/users/{match['mailbox']}/calendarView/delta"
        return answer(request, items, position, path, carry)

    @tenant.route("GET", mailbox_rx + r"/events/(?P<id>[^/]+)")
    async def get_event(tenant_, request, match):
        address = unquote(match["mailbox"]).lower()
        try:
            return httpx.Response(200, json=state.event(address, unquote(match["id"])))
        except KeyError:
            return graph_error(404, "ErrorItemNotFound", "The specified object was not found.")

    return state
