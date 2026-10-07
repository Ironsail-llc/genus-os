"""Edit an existing event: one bounded read / merge / conditional write / readback.

Every change to an existing meeting — adding or removing guests, moving it,
renaming it, RSVPing — goes through ``_conditional_patch``: read the event,
work out the smallest patch against what is ACTUALLY there (existing guests
and their RSVPs are carried over whole), write it with ``If-Match`` on the
version that was read, then read it back once to verify. A conflicting edit
gets one fresh read and merge; an uncertain write is only ever read back,
never blindly repeated.

These are direct writes. Editing a meeting is ordinary assistant work, not a
payment or an irreversible external action, so there is no draft and no
"reply yes to confirm" step. That step used to exist, and a bare "yes" from
anyone in a group chat could fire it.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

from robothor.engine.calendar_transport import CalendarTransport

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


def _instant(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _same_time(before: Any, after: Any) -> bool:
    """Compare INSTANTS, not representations.

    Google normalises an echoed ``timeZone`` into an equivalent offset, and
    comparing the dicts turned a write that had SUCCEEDED into a reported
    failure.
    """
    if before == after:
        return True
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if "dateTime" not in before or "dateTime" not in after:
        return False
    one, two = _instant(before["dateTime"]), _instant(after["dateTime"])
    return one is not None and one == two


def _time_value(value: str, existing: Any) -> dict[str, Any]:
    """The same ``{"dateTime": ...}`` shape ``gws_calendar_create`` writes.

    The event's own ``timeZone`` is kept: it decides how the meeting displays
    and, for a recurring series, how its occurrences expand. An all-day event
    being given a time has its ``date`` cleared, or Google would see both.
    """
    out: dict[str, Any] = {"dateTime": value}
    if isinstance(existing, dict):
        if existing.get("timeZone"):
            out["timeZone"] = existing["timeZone"]
        if "date" in existing:
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


def _conditional_patch(
    calendar_id: str,
    event_id: str,
    plan: Callable[[dict[str, Any]], dict[str, Any] | Plan],
    *,
    send_updates: str,
    cancelled: Any = None,
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
    with CalendarTransport() as api:
        for attempt in range(2):
            before = api.request("GET", calendar_id, event_id)
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
            if "attendees" in body and before.get("attendeesOmitted"):
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
            written = api.request(
                "PATCH",
                calendar_id,
                event_id,
                body=body,
                etag=before["etag"],
                send_updates=send_updates,
            )
            if written.get("status_code") == 412 and attempt == 0:
                continue
            if (
                "error" in written
                and not written.get("outcome_unknown")
                and written.get("status_code", 0) < 500
            ):
                return {**written, "invitations_requested": False, "verification": "failed"}
            after = api.request("GET", calendar_id, event_id)
            shown = after if "error" not in after else before
            verified = (
                "error" not in after
                and verify(after)
                and all(
                    _same_time(body.get(k, before.get(k)), after.get(k)) for k in ("start", "end")
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
            if not verified or "error" in written:
                result["error"] = (
                    "Update outcome could not be verified; do not repeat the write. "
                    "Read the event with gws_calendar_list and report what it shows."
                )
            return result
    raise AssertionError("unreachable")


def update_event(
    calendar_id: str,
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
) -> dict[str, Any]:
    """Patch an existing event: guests, time, title, notes, place — in one write.

    Existing guests are carried over whole (RSVP, optional flag, comment);
    additions and removals match case-insensitively. Moving only the start
    keeps the meeting's length. Everyone who will be mailed about the change is
    screened against the do-not-contact list first.
    """
    to_add = _unique(add)
    to_remove = _unique(remove)
    clash = sorted(set(to_add) & set(to_remove))
    if clash:
        return {"error": f"Cannot both add and remove {', '.join(clash)}"}
    texts = {k: v for k, v in (fields or {}).items() if k in TEXT_FIELDS}
    if start is not None and _instant(start) is None:
        return {"error": "start must be an RFC3339 date-time, e.g. 2026-10-09T14:00:00-04:00"}
    if end is not None and _instant(end) is None:
        return {"error": "end must be an RFC3339 date-time, e.g. 2026-10-09T15:00:00-04:00"}
    if start is not None and end is not None:
        one, two = _instant(start), _instant(end)
        if one and two and (one.tzinfo is None) == (two.tzinfo is None) and two <= one:
            return {"error": "end must be after start"}

    def plan(before: dict[str, Any]) -> dict[str, Any] | Plan:
        existing = deepcopy(before.get("attendees", []))
        present = _emails(before)
        missing = [e for e in to_add if e not in present]
        removed = [e for e in to_remove if e in present]
        already = sorted(e for e in to_add if e in present)
        status = _response_status(before)
        declined = sorted(e for e in already if status.get(e) == "declined")

        body: dict[str, Any] = {k: v for k, v in texts.items() if before.get(k) != v}
        new_start = new_end = None
        if start is not None:
            new_start = _time_value(start, before.get("start"))
            if end is None:
                old_one = _instant((before.get("start") or {}).get("dateTime"))
                old_two = _instant((before.get("end") or {}).get("dateTime"))
                moved = _instant(start)
                if old_one is None or old_two is None or moved is None:
                    return {
                        "error": "The event has no timed end to keep the length of; pass end too",
                        "invitations_requested": False,
                    }
                new_end = _time_value((moved + (old_two - old_one)).isoformat(), before.get("end"))
        if end is not None:
            new_end = _time_value(end, before.get("end"))
            if start is None:
                old_one = _instant((before.get("start") or {}).get("dateTime"))
                two = _instant(end)
                if (
                    old_one
                    and two
                    and (old_one.tzinfo is None) == (two.tzinfo is None)
                    and two <= old_one
                ):
                    return {"error": "end must be after start", "invitations_requested": False}
        if new_start is not None and not _same_time(before.get("start"), new_start):
            body["start"] = new_start
        if new_end is not None and not _same_time(before.get("end"), new_end):
            body["end"] = new_end
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
        calendar_id, event_id, plan, send_updates=send_updates, cancelled=cancelled
    )


def add_attendees(
    calendar_id: str,
    event_id: str,
    emails: list[str],
    *,
    screen: Callable[..., dict[str, Any] | None],
    send_updates: str = "all",
    cancelled: Any = None,
) -> dict[str, Any]:
    """Add guests to an existing event — ``update_event`` with only additions."""
    return update_event(
        calendar_id,
        event_id,
        add=emails,
        screen=screen,
        send_updates=send_updates,
        cancelled=cancelled,
    )


def respond(
    calendar_id: str,
    event_id: str,
    response: str,
    *,
    screen: Callable[..., dict[str, Any] | None],
    comment: str | None = None,
    send_updates: str = "all",
    cancelled: Any = None,
) -> dict[str, Any]:
    """Set the calendar owner's own RSVP on an invitation.

    The owner's entry is the one Google flags ``self`` (the attendee entry for
    the calendar this copy of the event lives on), or failing that the one whose
    address is the calendar's. Nobody else's RSVP is touched.
    """
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

        return {"attendees": attendees}, verify, {"response": response}

    return _conditional_patch(
        calendar_id, event_id, plan, send_updates=send_updates, cancelled=cancelled
    )
