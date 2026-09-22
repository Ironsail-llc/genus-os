"""Read back interrupted calendar operations without entering the write dispatcher."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID

from psycopg2.extras import RealDictCursor

from robothor.db.connection import assert_test_database_write, connection_database_name
from robothor.engine import calendar_operations as operations

logger = logging.getLogger(__name__)


def reconcile_record(operation_id: str, auth: Any, agent_id: str) -> None:
    """Recheck scope and resource lock; rate-limit unsuccessful reads durably."""
    operation_id = str(UUID(operation_id))
    with operations.get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        assert_test_database_write(connection_database_name(conn), "calendar_operations")
        scope = (operation_id, auth.tenant_id, auth.user_id, agent_id)
        query = """SELECT * FROM calendar_operations
            WHERE id=%s AND tenant_id=%s AND user_id=%s AND agent_id=%s
              AND status='executing' AND updated_at < now() - interval '10 seconds'"""
        cur.execute(query, scope)
        row = cur.fetchone()
        if not row:
            return
        series_id = row["event_id"].split("_", 1)[0]
        resource = f"{auth.tenant_id}\0{row['calendar_id']}\0{series_id}"
        lock = int.from_bytes(hashlib.sha256(resource.encode()).digest()[:8], "big", signed=True)
        cur.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (lock,))
        if not cur.fetchone()["acquired"]:
            return
        try:
            cur.execute(query, scope)
            row = cur.fetchone()
            if not row:
                return
            # Commit the attempt time before network access. A crash still leaves
            # the original executing barrier intact and a later read can recover.
            cur.execute(
                "UPDATE calendar_operations SET updated_at=now() WHERE id=%s", (operation_id,)
            )
            conn.commit()
            from robothor.engine.calendar_transport import CalendarTransport

            with CalendarTransport() as api:
                event = api.request("GET", row["calendar_id"], row["event_id"])
            result = operations._reconcile(
                event,
                row["arguments"],
                row.get("pre_write_etag"),
                row["calendar_id"],
                (row.get("result") or {}).get("calendar", {}).get("kind", "personal"),
            )
            if (row.get("result") or {}).get("invitations_requested") is True:
                result["invitations_requested"] = True
            status = "executing" if result.get("reconciliation_pending") else "blocked"
            cur.execute(
                "UPDATE calendar_operations SET status=%s,result=%s,updated_at=now() WHERE id=%s",
                (status, json.dumps(result), operation_id),
            )
            conn.commit()
        finally:
            conn.rollback()
            cur.execute("SELECT pg_advisory_unlock(%s)", (lock,))
            conn.commit()


def reconcile_outcome(auth: Any, session_key: str, client_id: str) -> None:
    """Re-resolve the original scoped run before calendar or CRM readback."""
    from robothor.engine.chat_recovery import read_outcome

    try:
        outcome = read_outcome(auth, session_key, client_id)
        if not outcome.get("terminal") or not outcome.get("reconciliation_pending"):
            return
        for effect in outcome.get("effects", []):
            try:
                if effect["status"] == "executing":
                    reconcile_record(effect["operation_id"], auth, effect["agent_id"])
                elif effect.get("kind") == "runtime_effect" and effect.get(
                    "reconciliation_pending"
                ):
                    from robothor.engine.runtime.chat_effect_recovery import (
                        reconcile_record as recover_effect,
                    )

                    recover_effect(effect["operation_id"], auth)
            except Exception as exc:
                # An unavailable provider must not hide independent saved results.
                logger.warning("Action recovery read deferred (%s)", type(exc).__name__)
    except Exception as exc:
        # Leave the uncertainty barrier intact; the next chat poll can retry a
        # read. Do not expose provider credentials or dispatch a repair action.
        logger.warning("Action recovery read deferred (%s)", type(exc).__name__)
