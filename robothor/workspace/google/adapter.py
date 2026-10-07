"""Google mail and calendar behind the workspace protocols -- a pass-through.

Every method issues exactly the call the ``gws_*`` handler issued inline before
the provider seam existed: the same gws CLI argv (JSON key order included),
the same timeout, the same ``CalendarTransport`` request, and it returns the
result untouched. The characterization goldens
(``robothor/engine/tests/test_gws_goldens.py``) hold it to that byte for byte.

Two lookups are deliberately late-bound, because they are the seams tests and
the goldens patch:

* the CLI is called as ``gws._run_gws`` (the handler module's attribute);
* conditional calendar requests construct
  ``robothor.engine.calendar_attendees.CalendarTransport``.

:class:`GoogleMailSync` / :class:`GoogleCalendarSync` do the work
synchronously; :class:`GoogleMail` / :class:`GoogleCalendar` implement the
async protocols by running them in a thread (``asyncio.to_thread``), and expose
the synchronous twin as ``.blocking`` for the handlers, which already run in a
worker thread.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal

from robothor.workspace.google import gmail_parse
from robothor.workspace.types import GOOGLE_CAPABILITIES, CalendarRef, Capabilities

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from robothor.engine.calendar_transport import CalendarTransport
    from robothor.workspace.types import (
        EventQuery,
        MailQuery,
    )

__all__ = ["GoogleCalendar", "GoogleCalendarSync", "GoogleMail", "GoogleMailSync"]

#: The assistant's own calendar, in Google's words: whichever account gws is
#: signed in as. Named once, here; the handlers say ``CalendarRef``.
GOOGLE_OWN_CALENDAR = "primary"

#: EventQuery key -> Calendar v3 parameter. The caller's key ORDER is kept.
_EVENT_QUERY_PARAMS = {
    "time_min": "timeMin",
    "time_max": "timeMax",
    "single_events": "singleEvents",
    "order_by": "orderBy",
    "max_results": "maxResults",
}


def _gws(args: list[str], timeout: int | None = None) -> dict[str, Any]:
    """The handler module's ``_run_gws``, looked up at call time."""
    from robothor.engine.tools.handlers import gws

    if timeout is None:
        return gws._run_gws(args)
    return gws._run_gws(args, timeout=timeout)


class GoogleMailSync:
    """Gmail through the gws CLI, synchronously."""

    def search(self, query: MailQuery, *, max_results: int) -> dict[str, Any]:
        # The ORIGINAL string: Google parses its own syntax better than we do,
        # and keeps receiving exactly what the agent wrote.
        params = {"userId": "me", "q": query.original, "maxResults": max_results}
        return _gws(["gmail", "users", "messages", "list", "--params", json.dumps(params)])

    def get_message(self, message_id: str, *, fmt: str) -> dict[str, Any]:
        params = {"userId": "me", "id": message_id, "format": fmt}
        return _gws(["gmail", "users", "messages", "get", "--params", json.dumps(params)])

    def get_thread(self, thread_id: str, *, fmt: str) -> dict[str, Any]:
        # NOTE: never `metadataHeaders` -- gws CLI v0.8.0 does not serialize
        # array query params, and the API then returns no headers at all.
        params = {"userId": "me", "id": thread_id, "format": fmt}
        return _gws(["gmail", "users", "threads", "get", "--params", json.dumps(params)])

    def send(self, raw: str, *, thread_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        return _gws(
            [
                "gmail",
                "users",
                "messages",
                "send",
                "--params",
                '{"userId":"me"}',
                "--json",
                json.dumps(body),
            ],
            timeout=30,
        )

    def reply(self, raw: str, *, thread_id: str) -> dict[str, Any]:
        # Gmail threads a reply by threadId plus the In-Reply-To/References
        # headers the handler put in the message; the send itself is the same.
        return self.send(raw, thread_id=thread_id)

    def modify(self, message_id: str, *, add_labels: Any, remove_labels: Any) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if add_labels:
            body["addLabelIds"] = add_labels
        if remove_labels:
            body["removeLabelIds"] = remove_labels
        return _gws(
            [
                "gmail",
                "users",
                "messages",
                "modify",
                "--params",
                json.dumps({"userId": "me", "id": message_id}),
                "--json",
                json.dumps(body),
            ]
        )

    def shape_envelope(
        self, raw: dict[str, Any], *, max_header_chars: int | None = None
    ) -> dict[str, Any]:
        return gmail_parse._shape_envelope(raw, max_header_chars=max_header_chars)

    def shape_message(self, raw: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
        return gmail_parse._shape_message(raw, max_chars=max_chars)


class _GoogleCalendarSession:
    """One ``CalendarTransport`` (one token) for a read / write / read-back."""

    def __init__(self, api: CalendarTransport) -> None:
        self._api = api

    def get(self, ref: CalendarRef, event_id: str) -> dict[str, Any]:
        return self._api.request("GET", ref.calendar_id, event_id)

    def conditional_patch(
        self,
        ref: CalendarRef,
        event_id: str,
        body: dict[str, Any],
        *,
        etag: str,
        send_updates: str,
    ) -> dict[str, Any]:
        return self._api.request(
            "PATCH",
            ref.calendar_id,
            event_id,
            body=body,
            etag=etag,
            send_updates=send_updates,
        )


class GoogleCalendarSync:
    """Google Calendar: the gws CLI for list/insert/delete, conditional HTTP for edits."""

    capabilities: Capabilities = GOOGLE_CAPABILITIES

    def resolve(self, kind: Literal["own", "operator"], *, address: str = "") -> CalendarRef:
        if kind == "own":
            return CalendarRef(GOOGLE_OWN_CALENDAR, "own")
        if kind == "operator" and address:
            return CalendarRef(address, "operator", mailbox=address)
        raise ValueError(f"cannot resolve calendar kind {kind!r} without an address")

    def list(self, ref: CalendarRef, query: EventQuery) -> dict[str, Any]:
        params: dict[str, Any] = {"calendarId": ref.calendar_id}
        for key, value in query.items():
            params[_EVENT_QUERY_PARAMS[key]] = value
        return _gws(["calendar", "events", "list", "--params", json.dumps(params)])

    def create(
        self,
        ref: CalendarRef,
        event: dict[str, Any],
        *,
        conference: bool,
        send_updates: str | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"calendarId": ref.calendar_id}
        if conference:
            params["conferenceDataVersion"] = 1
        if send_updates is not None:
            params["sendUpdates"] = send_updates
        return _gws(
            [
                "calendar",
                "events",
                "insert",
                "--params",
                json.dumps(params),
                "--json",
                json.dumps(event),
            ]
        )

    def delete(self, ref: CalendarRef, event_id: str, *, send_updates: str) -> dict[str, Any]:
        params = {"calendarId": ref.calendar_id, "eventId": event_id, "sendUpdates": send_updates}
        return _gws(["calendar", "events", "delete", "--params", json.dumps(params)])

    @contextmanager
    def session(self) -> Iterator[_GoogleCalendarSession]:
        from robothor.engine import calendar_attendees

        with calendar_attendees.CalendarTransport() as api:
            yield _GoogleCalendarSession(api)

    def get(self, ref: CalendarRef, event_id: str) -> dict[str, Any]:
        with self.session() as api:
            return api.get(ref, event_id)

    def conditional_patch(
        self,
        ref: CalendarRef,
        event_id: str,
        body: dict[str, Any],
        *,
        etag: str,
        send_updates: str,
    ) -> dict[str, Any]:
        with self.session() as api:
            return api.conditional_patch(ref, event_id, body, etag=etag, send_updates=send_updates)


class GoogleMail:
    """:class:`~robothor.workspace.protocols.MailProvider` over :class:`GoogleMailSync`."""

    def __init__(self) -> None:
        self.blocking = GoogleMailSync()

    async def search(self, query: MailQuery, *, max_results: int) -> dict[str, Any]:
        return await asyncio.to_thread(self.blocking.search, query, max_results=max_results)

    async def get_message(self, message_id: str, *, fmt: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.blocking.get_message, message_id, fmt=fmt)

    async def get_thread(self, thread_id: str, *, fmt: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.blocking.get_thread, thread_id, fmt=fmt)

    async def send(self, raw: str, *, thread_id: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self.blocking.send, raw, thread_id=thread_id)

    async def reply(self, raw: str, *, thread_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.blocking.reply, raw, thread_id=thread_id)

    async def modify(
        self, message_id: str, *, add_labels: Any, remove_labels: Any
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.blocking.modify, message_id, add_labels=add_labels, remove_labels=remove_labels
        )

    def shape_envelope(
        self, raw: dict[str, Any], *, max_header_chars: int | None = None
    ) -> dict[str, Any]:
        return self.blocking.shape_envelope(raw, max_header_chars=max_header_chars)

    def shape_message(self, raw: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
        return self.blocking.shape_message(raw, max_chars=max_chars)


class GoogleCalendar:
    """:class:`~robothor.workspace.protocols.CalendarProvider` over :class:`GoogleCalendarSync`."""

    capabilities: Capabilities = GOOGLE_CAPABILITIES

    def __init__(self) -> None:
        self.blocking = GoogleCalendarSync()

    def resolve(self, kind: Literal["own", "operator"], *, address: str = "") -> CalendarRef:
        return self.blocking.resolve(kind, address=address)

    async def list(self, ref: CalendarRef, query: EventQuery) -> dict[str, Any]:
        return await asyncio.to_thread(self.blocking.list, ref, query)

    async def get(self, ref: CalendarRef, event_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.blocking.get, ref, event_id)

    async def create(
        self,
        ref: CalendarRef,
        event: dict[str, Any],
        *,
        conference: bool,
        send_updates: str | None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.blocking.create, ref, event, conference=conference, send_updates=send_updates
        )

    async def conditional_patch(
        self,
        ref: CalendarRef,
        event_id: str,
        body: dict[str, Any],
        *,
        etag: str,
        send_updates: str,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.blocking.conditional_patch,
            ref,
            event_id,
            body,
            etag=etag,
            send_updates=send_updates,
        )

    async def respond(
        self,
        ref: CalendarRef,
        event_id: str,
        response: str,
        *,
        screen: Callable[..., dict[str, Any] | None],
        comment: str | None = None,
        send_updates: str = "all",
        cancelled: Any = None,
    ) -> dict[str, Any]:
        # A Google RSVP is a conditional PATCH of the self attendee entry;
        # the guarded read/merge/write lives in calendar_attendees.
        from robothor.engine import calendar_attendees

        return await asyncio.to_thread(
            calendar_attendees.respond,
            ref,
            event_id,
            response,
            screen=screen,
            comment=comment,
            send_updates=send_updates,
            cancelled=cancelled,
            provider=self,
        )

    async def delete(self, ref: CalendarRef, event_id: str, *, send_updates: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.blocking.delete, ref, event_id, send_updates=send_updates
        )
