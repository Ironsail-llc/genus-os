"""Settle calendar operation records left by the retired draft-and-confirm flow.

Attendee changes used to be a two-step durable operation: a draft row, then a
"yes"/"go" in chat that bound to it and executed it. That gate is gone — editing
a meeting is a direct write through ``gws_calendar_update`` (see
``calendar_attendees``) — but rows it left in ``executing`` may still be on an
instance, so the recovery worker and chat readback can settle them from
evidence. Nothing here writes to a calendar.
"""

from __future__ import annotations

from typing import Any

from robothor.db.connection import (
    get_connection as get_connection,
)


def _reconcile(
    event: dict[str, Any],
    stored: dict[str, Any],
    pre_write_etag: str | None,
    calendar_id: str,
    kind: str,
) -> dict[str, Any]:
    """Settle an interrupted write from evidence, not from assumption.

    An unchanged event version with none of the requested attendees on it is
    proof the request never reached the calendar. Reporting that as an unknown
    outcome armed a barrier that refused every later draft AND every direct
    write for the meeting, with nothing able to clear it.
    """
    calendar = {"kind": kind, "id": calendar_id}
    attendees = event.get("attendees", []) if isinstance(event, dict) else None
    if (
        not isinstance(event, dict)
        or "error" in event
        or event.get("id") != stored["event_id"]
        or not isinstance(attendees, list)
        or any(
            not isinstance(a, dict) or not isinstance(a.get("email", ""), str) for a in attendees
        )
    ):
        return {
            "error": "Provider read unavailable or invalid; reconciliation remains pending",
            "event_id": stored["event_id"],
            "reconciliation_pending": True,
            "invitations_requested": None,
            "verification": "unverified",
            "calendar": calendar,
        }
    present = {a.get("email", "").casefold() for a in event.get("attendees", [])}
    touched = sorted(present & set(stored["attendees"]))
    unchanged = bool(pre_write_etag) and str(event.get("etag") or "") == str(pre_write_etag)
    if "error" not in event and unchanged and not touched:
        return {
            "error": (
                "The interrupted write never reached the calendar — the event is unchanged and "
                "no invitation was requested. Make the change again with gws_calendar_update."
            ),
            "event_id": stored["event_id"],
            "attendees_present": [],
            "invitations_requested": False,
            "verification": "verified",
            # Proof, not a guess: there is nothing for a human to reconcile,
            # so no repair task is filed and the barrier does not arm.
            "no_write_confirmed": True,
            "calendar": calendar,
        }
    return {
        "error": "Interrupted write reconciled without retry; notification outcome unknown",
        "event_id": stored["event_id"],
        "attendees_present": touched,
        "invitations_requested": None,
        "verification": "unverified",
        "calendar": calendar,
    }
