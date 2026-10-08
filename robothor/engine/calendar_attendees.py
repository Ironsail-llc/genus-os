"""Edit an existing event: one bounded read / merge / conditional write / readback.

Every change to an existing meeting — adding or removing guests, moving it,
renaming it, RSVPing — goes through ``_conditional_patch``: read the event,
work out the smallest patch against what is ACTUALLY there (existing guests
and their RSVPs are carried over whole), write it with ``If-Match`` on the
version that was read, then read it back once to verify. A conflicting edit
gets one fresh read and merge; an uncertain write is only ever read back,
never blindly repeated.

The calendar is reached through a workspace
:class:`~robothor.workspace.protocols.CalendarProvider` (Google by default,
whose session is one ``CalendarTransport``), never by constructing a
transport here; every rule above is the provider-neutral part.

These are direct writes. Editing a meeting is ordinary assistant work, not a
payment or an irreversible external action, so there is no draft and no
"reply yes to confirm" step. That step used to exist, and a bare "yes" from
anyone in a group chat could fire it.
"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

# The Google provider's session constructs `calendar_attendees.CalendarTransport`
# at call time, so this name stays the seam tests patch.
from robothor.engine.calendar_transport import CalendarTransport as CalendarTransport
from robothor.workspace.types import CalendarRef, as_calendar_ref

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from robothor.workspace.protocols import CalendarProvider

#: Fields a plain update may set verbatim.
TEXT_FIELDS = ("summary", "description", "location")

#: The RSVP values Google accepts from an attendee.
RESPONSES = ("accepted", "declined", "tentative")


class OperationCancellation:
    """Observe both task cancellation and the operator's live interrupt flag.

    The write runs in a worker thread, which keeps going after the awaiting
    task is cancelled. Checking this immediately before the PATCH is what stops
    a cancelled request from still landing a moment later.
    """

    def __init__(self, run_id: str) -> None:
        from threading import Event

        from robothor.engine import session_registry

        self.event = Event()
        self.session = session_registry.lookup(run_id) if run_id else None

    def set(self) -> None:
        self.event.set()

    def is_set(self) -> bool:
        return self.event.is_set() or bool(
            self.session
            and (
                getattr(self.session, "_interrupt_requested", False)
                or getattr(self.session, "was_interrupted", False)
            )
        )


def _email(entry: dict[str, Any]) -> str:
    return str(entry.get("email") or "").casefold()


def _emails(event: dict[str, Any]) -> set[str]:
    return {_email(a) for a in event.get("attendees", [])}


def _response_status(event: dict[str, Any]) -> dict[str, str]:
    return {
        _email(a): str(a.get("responseStatus") or "needsAction") for a in event.get("attendees", [])
    }


def _unique(emails: Iterable[str]) -> list[str]:
    """Casefolded, stripped, first occurrence wins, order kept."""
    seen: list[str] = []
    for raw in emails:
        email = str(raw).strip().casefold()
        if email and email not in seen:
            seen.append(email)
    return seen


def recurrence_of(event: dict[str, Any]) -> dict[str, Any] | None:
    """Whether this write touches a SERIES, and say so.

    A single guest added to a ``FREQ=WEEKLY;COUNT=52`` master mails every
    existing guest about the whole series. Nothing in the result said the
    event recurred at all, so nobody could weigh that.
    """
    if event.get("recurrence"):
        return {
            "scope": "series",
            "rules": list(event["recurrence"]),
            "series_id": event.get("id"),
            "note": "This is a recurring series; the change and its notifications cover every occurrence.",
        }
    if event.get("recurringEventId"):
        return {
            "scope": "instance",
            "rules": [],
            "series_id": event["recurringEventId"],
            "note": "This is one occurrence of a recurring series; other occurrences are unchanged.",
        }
    return None


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _default_timezone() -> str:
    """The operator's configured zone, for a naive time on an event with none."""
    from robothor.constants import DEFAULT_TIMEZONE, platform_timezone

    try:
        return platform_timezone()
    except Exception:  # noqa: BLE001 - a settings problem must not lose the edit
        return DEFAULT_TIMEZONE


def _zone(name: Any) -> Any:
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(str(name)) if name else None
    except (KeyError, ValueError):
        return None


def _instant(value: Any, zone: Any = None) -> datetime | None:
    """Parse a date-time; a naive one is read in ``zone`` when one is given."""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None and zone:
        tz = _zone(zone)
        if tz is not None:
            moment = moment.replace(tzinfo=tz)
    return moment


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _kind(value: str) -> str | None:
    """``date`` (all-day), ``dateTime``, or ``None`` for something unparseable."""
    if _DATE_RE.fullmatch(value):
        return "date" if _as_date(value) else None
    return "dateTime" if _instant(value) is not None else None


def _same_time(before: Any, after: Any, zone: Any = None) -> bool:
    """Compare INSTANTS, not representations.

    Google normalises an echoed ``timeZone`` into an equivalent offset, and it
    localises a naive time (the format the schema invites) in the event's zone
    before echoing it with an offset. Comparing representations — or a naive
    instant to an aware one — turned writes that had SUCCEEDED into reported
    failures. Each side is read in its own ``timeZone``, then ``zone``.
    """
    if before == after:
        return True
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if before.get("date") or after.get("date"):
        return (
            not before.get("dateTime")
            and not after.get("dateTime")
            and before.get("date") == after.get("date")
        )
    if not before.get("dateTime") or not after.get("dateTime"):
        return False
    one = _instant(before["dateTime"], before.get("timeZone") or zone)
    two = _instant(after["dateTime"], after.get("timeZone") or zone)
    if one is None or two is None or (one.tzinfo is None) != (two.tzinfo is None):
        return False
    return one == two


def _event_zone(event: dict[str, Any]) -> str:
    for key in ("start", "end"):
        value = event.get(key)
        if isinstance(value, dict) and value.get("timeZone"):
            return str(value["timeZone"])
    return _default_timezone()


def _time_value(value: str, existing: Any, zone: str) -> dict[str, Any]:
    """The shape ``gws_calendar_create`` writes, kept consistent with the event.

    A bare date writes ``{"date": ...}`` — an all-day event stays all-day. A
    date-time keeps the event's own ``timeZone`` (it decides how the meeting
    displays and how a series expands); a naive one on an event with no zone
    gets the operator's, because Google rejects a naive time with no zone. The
    other representation is cleared with ``null``, or Google would see both.
    """
    existing = existing if isinstance(existing, dict) else {}
    if _kind(value) == "date":
        out: dict[str, Any] = {"date": value}
        if existing.get("dateTime"):
            out["dateTime"] = None
        return out
    out = {"dateTime": value}
    tz = existing.get("timeZone")
    parsed = _instant(value)
    if not tz and parsed is not None and parsed.tzinfo is None:
        tz = zone
    if tz:
        out["timeZone"] = tz
    if existing.get("date"):
        out["date"] = None
    return out


def _attendee_view(event: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "email": str(a.get("email") or ""),
            "responseStatus": str(a.get("responseStatus") or "needsAction"),
        }
        for a in event.get("attendees", [])
    ]


def _entry_preserved(old: dict[str, Any], attendees: list[dict[str, Any]]) -> bool:
    return any(all(actual.get(k) == v for k, v in old.items()) for actual in attendees)


Plan = tuple[dict[str, Any], "Callable[[dict[str, Any]], bool]", dict[str, Any]]


def _calendar_api(provider: CalendarProvider | None) -> Any:
    """The provider's synchronous face; this module runs in a worker thread."""
    from robothor.workspace import get_workspace
    from robothor.workspace.bridge import blocking

    return blocking(provider if provider is not None else get_workspace().calendar)


def _conditional_patch(
    calendar: CalendarRef | str,
    event_id: str,
    plan: Callable[[dict[str, Any]], dict[str, Any] | Plan],
    *,
    send_updates: str,
    cancelled: Any = None,
    allow_omitted: bool = False,
    provider: CalendarProvider | None = None,
) -> dict[str, Any]:
    """One write; a conflict gets one fresh merge, an uncertain write only a read.

    ``plan(before)`` returns either a finished result (a refusal, or "nothing
    to change") or ``(patch body, verify(after) -> bool, extra result fields)``.
    Returning an error never authorizes an agent to remove/re-add an attendee
    or blindly resend an invitation.
    """
    if cancelled is not None and cancelled.is_set():
        return {
            "error": "Calendar operation cancelled before write",
            "invitations_requested": False,
        }
    ref = as_calendar_ref(calendar)
    with _calendar_api(provider).session() as api:
        for attempt in range(2):
            before = api.get(ref, event_id)
            if "error" in before:
                return {**before, "verification": "unavailable", "invitations_requested": False}
            if before.get("status") == "cancelled":
                return {
                    "error": "Cannot edit a cancelled event",
                    "invitations_requested": False,
                }
            planned = plan(before)
            if isinstance(planned, dict):
                return planned
            body, verify, extra = planned
            if "attendees" in body and before.get("attendeesOmitted") and not allow_omitted:
                return {
                    "error": "Calendar returned an incomplete guest list; refusing to overwrite it",
                    "invitations_requested": False,
                }
            if not before.get("etag"):
                return {"error": "Calendar omitted the version; refusing an unconditional write"}
            if cancelled is not None and cancelled.is_set():
                return {
                    "error": "Calendar operation cancelled before write",
                    "invitations_requested": False,
                }
            written = api.conditional_patch(
                ref, event_id, body, etag=before["etag"], send_updates=send_updates
            )
            if written.get("status_code") == 412 and attempt == 0:
                continue
            if (
                "error" in written
                and not written.get("outcome_unknown")
                and written.get("status_code", 0) < 500
            ):
                return {**written, "invitations_requested": False, "verification": "failed"}
            after = api.get(ref, event_id)
            shown = after if "error" not in after else before
            zone = _event_zone(before)
            verified = (
                "error" not in after
                and verify(after)
                and all(
                    _same_time(body.get(k, before.get(k)), after.get(k), zone)
                    for k in ("start", "end")
                )
                and all(
                    before.get(k) == after.get(k)
                    for k in ("organizer", "conferenceData", "hangoutLink")
                )
            )
            requested = None if "error" in written else send_updates != "none"
            result: dict[str, Any] = {
                "event_id": event_id,
                "status": "updated" if verified else "partial",
                "verification": "verified" if verified else "unverified",
                "changed": sorted(body),
                "summary": shown.get("summary"),
                "start": shown.get("start"),
                "end": shown.get("end"),
                "attendees": _attendee_view(shown),
                "recurrence": recurrence_of(before),
                "invitations_requested": requested,
                "send_updates": send_updates,
                "delivery_verified": False,
                "htmlLink": shown.get("htmlLink") or before.get("htmlLink"),
                **extra,
            }
            # Only `all` lets the handler name who Google mailed; under any
            # other setting the key is absent rather than a guess.
            if requested and send_updates == "all":
                result["attendees_notified"] = [a["email"] for a in _attendee_view(shown)]
            if "error" in after and ("error" not in written or written.get("outcome_unknown")):
                result["reconciliation_pending"] = True
            if not verified and "error" in written:
                # The write may or may not have landed and the readback could
                # not settle it: the effect ledger records this as uncertain.
                result["outcome_unknown"] = True
            if not verified or "error" in written:
                result["error"] = (
                    "Update outcome could not be verified; do not repeat the write. "
                    "Read the event with gws_calendar_list and report what it shows."
                )
            return result
    raise AssertionError("unreachable")


def _protected(event: dict[str, Any], calendar_id: str) -> set[str]:
    """Addresses an edit may not remove: the organiser and this calendar's own."""
    out = {calendar_id.casefold()}
    organizer = event.get("organizer") or {}
    if organizer.get("email"):
        out.add(str(organizer["email"]).casefold())
    for entry in event.get("attendees", []):
        if entry.get("organizer") or entry.get("self"):
            out.add(_email(entry))
    out.discard("")
    return out


def _plan_times(
    before: dict[str, Any], start: str | None, end: str | None, zone: str
) -> dict[str, Any]:
    """The new ``start``/``end`` values, or ``{"error": ...}``.

    Moving only one end keeps the meeting's length — in days for an all-day
    event, as a duration for a timed one.
    """
    old_start = before.get("start") or {}
    old_end = before.get("end") or {}
    out: dict[str, Any] = {}
    if start is not None:
        out["start"] = _time_value(start, old_start, zone)
    if end is not None:
        out["end"] = _time_value(end, old_end, zone)
    kind = _kind(start) if start is not None else _kind(end) if end is not None else None
    if kind is None:
        return out
    if start is not None and end is None:
        if kind == "date":
            first = _as_date(old_start.get("date"))
            last = _as_date(old_end.get("date"))
            days = (last - first).days if first and last and last > first else 1
            moved = _as_date(start)
            assert moved is not None
            out["end"] = _time_value((moved + timedelta(days=days)).isoformat(), old_end, zone)
        else:
            one = _instant(old_start.get("dateTime"), old_start.get("timeZone") or zone)
            two = _instant(old_end.get("dateTime"), old_end.get("timeZone") or zone)
            moved_at = _instant(start)
            if one is None or two is None or moved_at is None:
                return {"error": "The event has no timed end to keep the length of; pass end too"}
            out["end"] = _time_value((moved_at + (two - one)).isoformat(), old_end, zone)
    first_value: dict[str, Any] = out.get("start") or old_start
    last_value: dict[str, Any] = out["end"]
    if not first_value.get(kind):
        return {"error": "The event's start is the other kind (all-day vs timed); pass start too"}
    if kind == "date":
        first = _as_date(first_value.get("date"))
        last = _as_date(last_value.get("date"))
        backwards = bool(first and last and last <= first)
    else:
        one = _instant(first_value.get("dateTime"), first_value.get("timeZone") or zone)
        two = _instant(last_value.get("dateTime"), last_value.get("timeZone") or zone)
        backwards = bool(
            one and two and (one.tzinfo is None) == (two.tzinfo is None) and two <= one
        )
    if backwards:
        return {"error": "end must be after start"}
    return out


def update_event(
    calendar: CalendarRef | str,
    event_id: str,
    *,
    screen: Callable[..., dict[str, Any] | None],
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
    start: str | None = None,
    end: str | None = None,
    fields: dict[str, Any] | None = None,
    send_updates: str = "all",
    cancelled: Any = None,
    provider: CalendarProvider | None = None,
) -> dict[str, Any]:
    """Patch an existing event: guests, time, title, notes, place — in one write.

    Existing guests are carried over whole (RSVP, optional flag, comment);
    additions and removals match case-insensitively. Moving only the start
    keeps the meeting's length. Everyone who will be mailed about the change is
    screened against the do-not-contact list first.
    """
    calendar_id = as_calendar_ref(calendar).calendar_id
    to_add = _unique(add)
    to_remove = _unique(remove)
    clash = sorted(set(to_add) & set(to_remove))
    if clash:
        return {"error": f"Cannot both add and remove {', '.join(clash)}"}
    texts = {k: v for k, v in (fields or {}).items() if k in TEXT_FIELDS}
    hint = "an RFC3339 date-time (2026-10-09T14:00:00-04:00) or, for all-day, a date (2026-10-09)"
    start_kind = _kind(start) if start is not None else None
    end_kind = _kind(end) if end is not None else None
    if start is not None and start_kind is None:
        return {"error": f"start must be {hint}"}
    if end is not None and end_kind is None:
        return {"error": f"end must be {hint}"}
    if start_kind and end_kind and start_kind != end_kind:
        return {"error": "start and end must both be dates (all-day) or both date-times"}

    def plan(before: dict[str, Any]) -> dict[str, Any] | Plan:
        existing = deepcopy(before.get("attendees", []))
        present = _emails(before)
        missing = [e for e in to_add if e not in present]
        removed = [e for e in to_remove if e in present]
        already = sorted(e for e in to_add if e in present)
        status = _response_status(before)
        declined = sorted(e for e in already if status.get(e) == "declined")

        protected = _protected(before, calendar_id)
        refused = sorted(e for e in to_remove if e in protected)
        if refused:
            return {
                "error": (
                    f"Cannot remove {', '.join(refused)}: the organiser and this calendar's own "
                    "entry stay on the meeting. To leave it, use gws_calendar_respond with "
                    "response='declined'; to cancel it, gws_calendar_delete."
                ),
                "invitations_requested": False,
            }

        body: dict[str, Any] = {k: v for k, v in texts.items() if before.get(k) != v}
        zone = _event_zone(before)
        times = _plan_times(before, start, end, zone)
        if "error" in times:
            return {**times, "invitations_requested": False}
        for key in ("start", "end"):
            value = times.get(key)
            if value is not None and not _same_time(before.get(key), value, zone):
                body[key] = value
        if missing or removed:
            body["attendees"] = [a for a in existing if _email(a) not in set(removed)] + [
                {"email": e} for e in missing
            ]

        extra = {
            "added": missing,
            "removed": removed,
            "already_present": already,
            "declined": declined,
        }
        if not body:
            note = "Nothing to change; the event already matches. No notification was requested."
            if declined:
                # Re-adding a declined guest is a no-op at Google: they stay
                # declined and no new invitation goes out. Saying "already
                # invited" reports a refusal as a success.
                note = (
                    "Already on the invitation, but "
                    + ", ".join(declined)
                    + " declined. Re-adding a declined guest does not re-invite them and no "
                    "notification was requested; ask the operator how to proceed."
                )
            only_adds = bool(to_add) and not (to_remove or texts or start or end)
            return {
                "event_id": event_id,
                "status": "already_present" if only_adds else "unchanged",
                "verification": "verified",
                **extra,
                "response_status": {e: status.get(e, "needsAction") for e in already},
                "note": note,
                "summary": before.get("summary"),
                "start": before.get("start"),
                "end": before.get("end"),
                "attendees": _attendee_view(before),
                "recurrence": recurrence_of(before),
                "invitations_requested": False,
                "htmlLink": before.get("htmlLink"),
            }

        # Who will hear about this: everyone left on the invitation when
        # Google is asked to mail, and the newly invited in every case — an
        # added guest gets the event on their calendar whether mailed or not.
        remaining = [_email(a) for a in body.get("attendees", existing)]
        recipients = set(missing) | (set(remaining) if send_updates != "none" else set())
        recipients -= set(removed)
        if recipients:
            refusal = screen(*sorted(recipients))
            if refusal:
                return refusal

        def verify(after: dict[str, Any]) -> bool:
            got = _emails(after)
            if not set(missing) <= got or set(removed) & got:
                return False
            kept = [a for a in existing if _email(a) not in set(removed)]
            if not all(_entry_preserved(old, after.get("attendees", [])) for old in kept):
                return False
            return all(after.get(k) == v for k, v in body.items() if k in TEXT_FIELDS)

        return body, verify, extra

    return _conditional_patch(
        calendar,
        event_id,
        plan,
        send_updates=send_updates,
        cancelled=cancelled,
        provider=provider,
    )


def add_attendees(
    calendar: CalendarRef | str,
    event_id: str,
    emails: list[str],
    *,
    screen: Callable[..., dict[str, Any] | None],
    send_updates: str = "all",
    cancelled: Any = None,
    provider: CalendarProvider | None = None,
) -> dict[str, Any]:
    """Add guests to an existing event — ``update_event`` with only additions."""
    return update_event(
        calendar,
        event_id,
        add=emails,
        screen=screen,
        send_updates=send_updates,
        cancelled=cancelled,
        provider=provider,
    )


def respond(
    calendar: CalendarRef | str,
    event_id: str,
    response: str,
    *,
    screen: Callable[..., dict[str, Any] | None],
    comment: str | None = None,
    send_updates: str = "all",
    cancelled: Any = None,
    provider: CalendarProvider | None = None,
) -> dict[str, Any]:
    """Set the calendar owner's own RSVP on an invitation.

    The owner's entry is the one Google flags ``self`` (the attendee entry for
    the calendar this copy of the event lives on), or failing that the one whose
    address is the calendar's. Nobody else's RSVP is touched.
    """
    calendar_id = as_calendar_ref(calendar).calendar_id
    if response not in RESPONSES:
        return {"error": f"response must be one of {', '.join(RESPONSES)}"}

    def plan(before: dict[str, Any]) -> dict[str, Any] | Plan:
        attendees = deepcopy(before.get("attendees", []))
        index = next((i for i, a in enumerate(attendees) if a.get("self")), None)
        if index is None:
            index = next(
                (i for i, a in enumerate(attendees) if _email(a) == calendar_id.casefold()), None
            )
        if index is None:
            organizer = before.get("organizer") or {}
            if organizer.get("self") or str(organizer.get("email") or "").casefold() == (
                calendar_id.casefold()
            ):
                why = "This calendar organises the meeting; an organiser has no RSVP to send."
            else:
                why = "This calendar is not on the meeting's guest list, so it has no RSVP to send."
            return {"error": why, "invitations_requested": False}
        mine = attendees[index]
        if mine.get("responseStatus") == response and (
            comment is None or mine.get("comment") == comment
        ):
            return {
                "event_id": event_id,
                "status": "unchanged",
                "verification": "verified",
                "response": response,
                "note": f"Already {response}; nothing was sent.",
                "summary": before.get("summary"),
                "start": before.get("start"),
                "end": before.get("end"),
                "invitations_requested": False,
                "htmlLink": before.get("htmlLink"),
            }
        mine["responseStatus"] = response
        if comment is not None:
            mine["comment"] = comment
        organizer = str((before.get("organizer") or {}).get("email") or "")
        if organizer and send_updates != "none":
            refusal = screen(organizer)
            if refusal:
                return refusal
        target = _email(mine)

        def verify(after: dict[str, Any]) -> bool:
            entry = next(
                (a for a in after.get("attendees", []) if a.get("self") or _email(a) == target),
                None,
            )
            others = [a for i, a in enumerate(before.get("attendees", [])) if i != index]
            return (
                entry is not None
                and entry.get("responseStatus") == response
                and all(_entry_preserved(old, after.get("attendees", [])) for old in others)
            )

        # A guest's copy with a hidden guest list carries `attendeesOmitted`
        # and (usually) only the self entry. Google's documented RSVP for that
        # copy is a PATCH carrying just the self entry.
        patch = [mine] if before.get("attendeesOmitted") else attendees
        return {"attendees": patch}, verify, {"response": response}

    return _conditional_patch(
        calendar,
        event_id,
        plan,
        send_updates=send_updates,
        cancelled=cancelled,
        allow_omitted=True,
        provider=provider,
    )
