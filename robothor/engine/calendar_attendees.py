"""A bounded read/merge/conditional-write/readback operation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from robothor.engine.calendar_transport import CalendarTransport


def _emails(event: dict[str, Any]) -> set[str]:
    return {a.get("email", "").casefold() for a in event.get("attendees", [])}


def add_attendees(
    calendar_id: str,
    event_id: str,
    emails: list[str],
    *,
    screen: Any,
    expected_event: dict[str, Any] | None = None,
    cancelled: Any = None,
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
        before = api.request("GET", calendar_id, event_id)
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
                return {
                    "event_id": event_id,
                    "status": "already_present",
                    "verification": "verified",
                    "added": [],
                    "already_present": sorted(requested),
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
                and all(
                    before.get(k) == after.get(k)
                    for k in ("start", "end", "organizer", "conferenceData", "hangoutLink")
                )
            )
            result = {
                "event_id": event_id,
                "added": missing if present else [],
                "already_present": sorted(requested & _emails(before)),
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
