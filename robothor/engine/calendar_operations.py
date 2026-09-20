"""Durable drafts and write-ahead state for attendee changes.

A session advisory lock serializes the resource across processes. Committing
the executing marker BEFORE the Google request lets a later process reconcile
a crash without replaying a potentially successful write.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg2.extras import RealDictCursor

from robothor.db.connection import (
    assert_test_database_write,
    connection_database_name,
    get_connection,
)

OPERATION_MARKER = "Calendar operation: "


class OperationCancellation:
    """Observe both task cancellation and the operator's live interrupt flag.

    The worker may read the flag, but only the runner consumes the interrupt.
    """

    def __init__(self, run_id: str) -> None:
        from threading import Event

        from robothor.engine import session_registry

        self.event = Event()
        self.session = session_registry.lookup(run_id)

    def set(self) -> None:
        self.event.set()

    def is_set(self) -> bool:
        return self.event.is_set() or bool(
            self.session and (self.session._interrupt_requested or self.session.was_interrupted)
        )


def confirmation_id(message: str, history: list[dict[str, Any]]) -> str | None:
    """Only a plain confirmation of the immediately preceding assistant draft."""
    if message.strip().casefold().rstrip(".! ") not in {"go", "yes", "confirm", "go ahead"}:
        return None
    if not history or history[-1].get("role") != "assistant":
        return None
    text = history[-1].get("content", "")
    if not isinstance(text, str):
        return None
    ids = re.findall(r"Calendar operation: ([0-9a-f-]{36})", text)
    if len(ids) != 1:
        return None
    try:
        return str(UUID(ids[0]))
    except ValueError:
        return None


def load_operation(operation_id: str, tenant: str, user: str, agent: str) -> dict[str, Any] | None:
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM calendar_operations WHERE id=%s AND tenant_id=%s AND user_id=%s AND agent_id=%s",
            (operation_id, tenant, user, agent),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def _reconcile_interrupted(calendar_id: str, event_id: str, stored: dict) -> dict:
    from robothor.engine.calendar_transport import CalendarTransport

    with CalendarTransport() as api:
        event = api.request("GET", calendar_id, event_id)
    attendees = event.get("attendees", []) if isinstance(event, dict) else None
    if (
        not isinstance(event, dict)
        or "error" in event
        or event.get("id") != event_id
        or not isinstance(attendees, list)
        or any(
            not isinstance(a, dict) or not isinstance(a.get("email", ""), str) for a in attendees
        )
    ):
        return {
            "error": "Provider read unavailable or invalid; reconciliation remains pending",
            "event_id": event_id,
            "reconciliation_pending": True,
            "invitations_requested": None,
            "verification": "unverified",
        }
    present = {a.get("email", "").casefold() for a in attendees}
    return {
        "error": "Interrupted write reconciled without retry; notification outcome unknown",
        "event_id": event_id,
        "attendees_present": sorted(present & set(stored["attendees"])),
        "invitations_requested": None,
        "verification": "unverified",
    }


def _perform_locked(args: dict[str, Any], ctx: Any, *, cancelled: Any = None) -> dict[str, Any]:
    from robothor.engine.calendar_transport import CalendarTransport
    from robothor.engine.tools.handlers.gws import _handle_gws_tool, _resolve_calendar

    if not ctx.user_id or not ctx.tenant_id:
        return {"error": "A verified requester and tenant are required"}
    operation_id = args.get("operation_id")
    if operation_id and args.get("draft"):
        return {
            "error": "To prepare a draft, pass event_id and attendees without operation_id",
            "invitations_requested": False,
        }
    if operation_id:
        try:
            operation_id = str(UUID(str(operation_id)))
        except ValueError:
            return {"error": "Invalid calendar operation id"}
        row = load_operation(operation_id, ctx.tenant_id, ctx.user_id, ctx.agent_id)
        if row is None:
            return {"error": "Calendar operation not found for this requester"}
        supplied = {k: v for k, v in args.items() if k not in {"operation_id", "draft"}}
        if supplied and supplied != row["arguments"]:
            return {"error": "Confirmed operation arguments cannot be changed"}
        args = dict(row["arguments"])
    else:
        row = None
    try:
        calendar_id, _ = _resolve_calendar(args)
    except ValueError:
        return {"error": "Invalid calendar selector"}
    event_id = args.get("event_id")
    emails = args.get("attendees")
    if not isinstance(event_id, str) or not event_id or not isinstance(emails, list) or not emails:
        return {"error": "event_id and attendees are required"}
    if any(
        not isinstance(e, str) or not re.fullmatch(r"[^\s@,<>]+@[^\s@,<>]+\.[^\s@,<>]+", e.strip())
        for e in emails
    ):
        return {"error": "Invalid attendee email"}
    stored = {
        "calendar_id": calendar_id,
        "event_id": event_id,
        "attendees": sorted({e.strip().casefold() for e in emails}),
    }
    # All writers for this resource share a key regardless of requester.
    digest = hashlib.sha256(f"{ctx.tenant_id}\0{calendar_id}\0{event_id}".encode()).digest()
    lock = int.from_bytes(digest[:8], "big", signed=True)
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        assert_test_database_write(connection_database_name(conn), "calendar_operations")
        cur.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (lock,))
        if not cur.fetchone()["acquired"]:
            return {"error": "Another operation is updating this meeting; no write attempted"}
        try:
            if operation_id:
                # Re-read AFTER the resource lock; another confirmation may
                # have completed between the first read and lock acquisition.
                cur.execute(
                    "SELECT * FROM calendar_operations WHERE id=%s AND tenant_id=%s AND user_id=%s AND agent_id=%s",
                    (operation_id, ctx.tenant_id, ctx.user_id, ctx.agent_id),
                )
                row = cur.fetchone()
                if row is None:
                    return {"error": "Calendar operation no longer exists"}
                if row["status"] in {"completed", "blocked"}:
                    return {**(row["result"] or {}), "operation_id": operation_id, "replayed": True}
                if row["status"] == "draft" and not row.get("draft_event"):
                    return {"error": "Draft was not prepared successfully; prepare a new draft"}
                if row["status"] == "draft" and datetime.now(UTC) - row["created_at"] > timedelta(
                    hours=24
                ):
                    return {
                        "error": "The draft expired; prepare a new draft before changing the event"
                    }
            else:
                cur.execute(
                    "INSERT INTO calendar_operations (tenant_id,user_id,agent_id,calendar_id,event_id,arguments,status) VALUES (%s,%s,%s,%s,%s,%s::jsonb,'draft') RETURNING id",
                    (
                        ctx.tenant_id,
                        ctx.user_id,
                        ctx.agent_id,
                        calendar_id,
                        event_id,
                        json.dumps(stored),
                    ),
                )
                operation_id = str(cur.fetchone()["id"])
                conn.commit()
            # Existing drafts are subject to the same uncertainty barrier as new requests.
            cur.execute(
                "SELECT id FROM calendar_operations WHERE tenant_id=%s AND calendar_id=%s AND event_id=%s AND id<>%s AND (status='executing' OR (status='blocked' AND result->'invitations_requested' IS DISTINCT FROM 'false'::jsonb)) LIMIT 1",
                (ctx.tenant_id, calendar_id, event_id, operation_id),
            )
            if cur.fetchone():
                return {"error": "A prior operation needs reconciliation; no new write attempted"}
            if args.get("draft"):
                with CalendarTransport() as api:
                    event = api.request("GET", calendar_id, event_id)
                if "error" in event:
                    return event
                snapshot = {k: event.get(k) for k in ("summary", "start", "end", "organizer")}
                cur.execute(
                    "UPDATE calendar_operations SET draft_event=%s::jsonb WHERE id=%s",
                    (json.dumps(snapshot), operation_id),
                )
                conn.commit()
                return {
                    "status": "draft",
                    "operation_id": operation_id,
                    "event_id": event_id,
                    "summary": event.get("summary"),
                    "start": event.get("start"),
                    "attendees_to_add": stored["attendees"],
                    "invitations_requested": False,
                    "instruction": "Show the draft and include this exact line: "
                    + OPERATION_MARKER
                    + operation_id,
                }
            if row and row["status"] == "executing":
                result = _reconcile_interrupted(calendar_id, event_id, stored)
            else:
                cur.execute(
                    "UPDATE calendar_operations SET status='executing',updated_at=now() WHERE id=%s",
                    (operation_id,),
                )
                conn.commit()
                if row and row.get("draft_event"):
                    stored["_expected_event"] = row["draft_event"]
                stored["_cancel_event"] = cancelled
                result = _handle_gws_tool(
                    "gws_calendar_add_attendees", stored, run_id=ctx.run_id, tenant_id=ctx.tenant_id
                )
            status = (
                "executing"
                if result.get("reconciliation_pending")
                else "blocked"
                if result.get("error")
                else "completed"
            )
            cur.execute(
                "UPDATE calendar_operations SET status=%s,result=%s::jsonb,updated_at=now() WHERE id=%s",
                (status, json.dumps(result), operation_id),
            )
            conn.commit()
            return {**result, "operation_id": operation_id}
        finally:
            conn.rollback()
            cur.execute("SELECT pg_advisory_unlock(%s)", (lock,))
            conn.commit()


def perform(args: dict[str, Any], ctx: Any, *, cancelled: Any = None) -> dict[str, Any]:
    """Complete the durable operation before optional repair-task bookkeeping.

    Never wait for a second pooled connection while holding the event lock and
    first connection. Concurrent failures must not exhaust the pool in a cycle.
    """
    result = _perform_locked(args, ctx, cancelled=cancelled)
    if (
        not result.get("error")
        or not result.get("operation_id")
        or result.get("replayed")
        or result.get("reconciliation_pending")
    ):
        return result
    from robothor.engine.calendar_repair import attach_repair_task

    attach_repair_task(result, ctx)
    with get_connection() as conn, conn.cursor() as cur:
        assert_test_database_write(connection_database_name(conn), "calendar_operations")
        cur.execute(
            "UPDATE calendar_operations SET result=%s::jsonb,updated_at=now() WHERE id=%s AND tenant_id=%s AND user_id=%s",
            (json.dumps(result), result["operation_id"], ctx.tenant_id, ctx.user_id),
        )
    return result
