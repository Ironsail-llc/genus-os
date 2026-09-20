"""A bounded read/merge/conditional-write/readback operation."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from robothor.engine.calendar_transport import CalendarTransport


def _emails(event: dict[str, Any]) -> set[str]:
    return {a.get("email", "").casefold() for a in event.get("attendees", [])}


def _response_status(event: dict[str, Any]) -> dict[str, str]:
    return {
        a.get("email", "").casefold(): str(a.get("responseStatus") or "needsAction")
        for a in event.get("attendees", [])
    }


def recurrence_of(event: dict[str, Any]) -> dict[str, Any] | None:
    """Whether this write touches a SERIES, and say so before it happens.

    A single guest added to a ``FREQ=WEEKLY;COUNT=52`` master mails every
    existing guest about the whole series. Nothing in the result said the
    event recurred at all, so nobody could weigh that before confirming.
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


def _instant(value: Any) -> Any:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _same_time(before: Any, after: Any) -> bool:
    """Compare INSTANTS, not representations.

    Google normalises an echoed ``timeZone`` into an equivalent offset, and
    comparing the dicts turned a write that had SUCCEEDED into a blocked
    operation needing human reconciliation.
    """
    if before == after:
        return True
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if "dateTime" not in before or "dateTime" not in after:
        return False
    one, two = _instant(before["dateTime"]), _instant(after["dateTime"])
    return one is not None and one == two


def add_attendees(
    calendar_id: str,
    event_id: str,
    emails: list[str],
    *,
    screen: Callable[..., dict[str, Any] | None],
    expected_event: dict[str, Any] | None = None,
    cancelled: Any = None,
    prefetched: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One write; a conflict gets one fresh merge, an uncertain write only a read.

    Returning an error never authorizes an agent to remove/re-add an attendee
    or blindly resend an invitation.
    """
    requested = set(emails)
    if cancelled is not None and cancelled.is_set():
        return {
            "error": "Calendar operation cancelled before write",
            "invitations_requested": False,
        }
    with CalendarTransport() as api:
        # A caller that already read this event (to record the pre-write
        # version) hands the read in rather than paying for a second one.
        before = (
            deepcopy(prefetched)
            if prefetched is not None
            else api.request("GET", calendar_id, event_id)
        )
        for attempt in range(2):
            if "error" in before:
                return {**before, "verification": "unavailable", "invitations_requested": False}
            if before.get("attendeesOmitted") or before.get("status") == "cancelled":
                return {
                    "error": "Cannot edit an incomplete or cancelled event",
                    "invitations_requested": False,
                }
            if expected_event and any(before.get(k) != v for k, v in expected_event.items()):
                return {
                    "error": "The meeting details changed since the draft; prepare a new draft",
                    "invitations_requested": False,
                }
            missing = sorted(requested - _emails(before))
            if not missing:
                status = _response_status(before)
                declined = sorted(e for e in requested if status.get(e) == "declined")
                note = "Already on the invitation; no notification was requested."
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
                return {
                    "event_id": event_id,
                    "status": "already_present",
                    "verification": "verified",
                    "added": [],
                    "already_present": sorted(requested),
                    "declined": declined,
                    "response_status": {e: status.get(e, "needsAction") for e in sorted(requested)},
                    "note": note,
                    "recurrence": recurrence_of(before),
                    "invitations_requested": False,
                    "htmlLink": before.get("htmlLink"),
                }
            refusal = screen(*sorted(_emails(before) | requested))
            if refusal:
                return refusal
            if not before.get("etag"):
                return {"error": "Calendar omitted the version; refusing an unconditional write"}
            attendees = deepcopy(before.get("attendees", []))
            attendees.extend({"email": email} for email in missing)
            if cancelled is not None and cancelled.is_set():
                return {
                    "error": "Calendar operation cancelled before write",
                    "invitations_requested": False,
                }
            written = api.request(
                "PATCH",
                calendar_id,
                event_id,
                body={"attendees": attendees},
                etag=before["etag"],
            )
            if written.get("status_code") == 412 and attempt == 0:
                before = api.request("GET", calendar_id, event_id)
                continue
            if (
                "error" in written
                and not written.get("outcome_unknown")
                and written.get("status_code", 0) < 500
            ):
                return {**written, "invitations_requested": False, "verification": "failed"}
            after = api.request("GET", calendar_id, event_id)
            present = "error" not in after and requested <= _emails(after)
            preserved = (
                present
                and all(
                    any(
                        all(actual.get(k) == v for k, v in old.items())
                        for actual in after.get("attendees", [])
                    )
                    for old in before.get("attendees", [])
                )
                and all(_same_time(before.get(k), after.get(k)) for k in ("start", "end"))
                and all(
                    before.get(k) == after.get(k)
                    for k in ("organizer", "conferenceData", "hangoutLink")
                )
            )
            result = {
                "event_id": event_id,
                "added": missing if present else [],
                "already_present": sorted(requested & _emails(before)),
                "recurrence": recurrence_of(before),
                "status": "updated" if preserved else "partial",
                "verification": "verified" if preserved else "unverified",
                "invitations_requested": None if "error" in written else True,
                "send_updates": "all",
                "delivery_verified": False,
                "htmlLink": after.get("htmlLink") or before.get("htmlLink"),
            }
            if not preserved or "error" in written:
                result["error"] = (
                    "Update outcome requires reconciliation; do not repeat the write. "
                    "Report the partial result and record a separate integration repair task."
                )
            return result
    raise AssertionError("unreachable")
