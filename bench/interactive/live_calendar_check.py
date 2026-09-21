"""Explicit operator-only integration check using one NEW disposable event.

Only --send-test-invitation creates an event or sends invitations. This is not an
agent benchmark: production benchmark tools remain unable to access Google.
Recipient and organizer details belong in a private output file, never in git.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from robothor.engine.calendar_transport import CalendarTransport
from robothor.engine.tools.handlers.gws import _handle_gws_tool


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--send-test-invitation", action="store_true")
    args = parser.parse_args()
    if not args.send_test_invitation:
        parser.error("No action taken: --send-test-invitation is required")
    from robothor.owner_config import load_owner_config

    owner_config = load_owner_config()
    if owner_config is None:
        parser.error("Configure an operator identity before running this check")
    tenant_id = owner_config.tenant_id
    with CalendarTransport() as api:
        api._authenticate()
        headers = {"Authorization": f"Bearer {api.token}"}
        base = "https://www.googleapis.com/calendar/v3/calendars/primary"
        owner = api.client.get(base, headers=headers)
        owner.raise_for_status()
        organizer = owner.json()["id"]
        if args.recipient.casefold() == organizer.casefold():
            parser.error("Recipient must differ from the organizer for this test")
        start = datetime.now(UTC) + timedelta(hours=1)
        created = api.client.post(
            base + "/events",
            headers=headers,
            params={"sendUpdates": "none"},
            json={
                "summary": "Robothor engine validation — test invitation",
                "description": "Disposable engine integration test. No existing meeting was changed.",
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": (start + timedelta(minutes=10)).isoformat()},
                "attendees": [{"email": organizer, "responseStatus": "accepted"}],
            },
        )
        created.raise_for_status()
        event_id = created.json()["id"]
        # Record identity before the write so a failed check can still be cleaned up.
        report = {"event_id": event_id, "calendar": "primary", "created": True, "passed": False}
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        before = api.request("GET", "primary", event_id)
        started = time.perf_counter()
        result = _handle_gws_tool(
            "gws_calendar_add_attendees",
            {
                "calendar_id": "primary",
                "event_id": event_id,
                "attendees": [args.recipient],
            },
            tenant_id=tenant_id,
        )
        duration_ms = (time.perf_counter() - started) * 1000
        after = api.request("GET", "primary", event_id)
        checks = {
            "verified": result.get("verification") == "verified" and not result.get("error"),
            "only_test_participants": {a["email"].casefold() for a in after.get("attendees", [])}
            == {organizer.casefold(), args.recipient.casefold()},
            "existing_rsvp_preserved": all(
                a in after.get("attendees", []) for a in before.get("attendees", [])
            ),
            "time_preserved": all(before.get(k) == after.get(k) for k in ("start", "end")),
            "notifications_requested": result.get("invitations_requested") is True,
        }
        checks["no_duplicate_invitation"] = False
        if all(checks[k] for k in checks if k != "no_duplicate_invitation"):
            replay = _handle_gws_tool(
                "gws_calendar_add_attendees",
                {
                    "calendar_id": "primary",
                    "event_id": event_id,
                    "attendees": [args.recipient],
                },
                tenant_id=tenant_id,
            )
            checks["no_duplicate_invitation"] = (
                replay.get("invitations_requested") is False
                and replay.get("status") == "already_present"
            )
        report.update(
            {
                "passed": all(checks.values()),
                "checks": checks,
                "operation_ms": round(duration_ms),
                "result": result,
                "delivery_verified": False,
            }
        )
        # Only remove the event this process created. No cancellation email is sent.
        removed = api.client.delete(
            base + "/events/" + quote(event_id, safe=""),
            headers=headers,
            params={"sendUpdates": "none"},
        )
        report["test_event_removed"] = removed.status_code == 204
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {
                    k: report[k]
                    for k in (
                        "passed",
                        "checks",
                        "operation_ms",
                        "test_event_removed",
                        "delivery_verified",
                    )
                },
                indent=2,
            )
        )
        return 0 if report["passed"] and report["test_event_removed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
