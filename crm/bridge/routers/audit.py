"""Audit API — programmatic access to audit logs and guardrail events.

Provides REST endpoints for querying the audit_log and agent_guardrail_events
tables. Protected by Cloudflare Access + RBAC middleware.

``/events.csv`` is the same query as ``/events`` and a different contract,
because an export is read by two things JSON is not — a spreadsheet, and a
person who will believe what the spreadsheet shows them:

* **Cells are neutralised before they are written.** ``details`` is
  attacker-influenced by construction (it is what the appliance logged about
  somebody's request), and a cell opening ``=``, ``+``, ``-``, ``@``, tab or CR
  is a formula the moment the file is opened in Excel, Numbers or Sheets. Each
  such cell is prefixed with an apostrophe, the one neutralisation every
  spreadsheet honours. Every cell also goes through
  :func:`robothor.secrets.redaction.redact` and
  :func:`robothor.sanitize.sanitize_log`, so a credential-shaped detail does
  not leave the appliance and a newline inside one cannot forge a row.
* **A failure is a 500, not an empty file.** ``/events`` swallows every
  exception into a 200 with ``{"error": "internal error"}`` — the Helm's Audit
  page is built on that shape, so it stays. The CSV route must not: a zero-row
  file that downloads cleanly is an audit trail the operator has no reason to
  doubt and every reason to.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

# Module level, not function level, so tests can stand in for the audit store
# at ONE seam rather than patching a lazy import inside each handler.
from robothor.audit.logger import query_log
from robothor.sanitize import sanitize_log
from robothor.secrets.redaction import redact
from routers._operator import require_audit_reader

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audit", tags=["audit"])

#: The export's columns, in order. Same fields ``query_log`` returns, named the
#: same way, so a spreadsheet column and a JSON key never disagree.
CSV_COLUMNS = (
    "id",
    "timestamp",
    "event_type",
    "category",
    "actor",
    "action",
    "target",
    "status",
    "source_channel",
    "session_key",
    "user_id",
    "details",
)

#: A bigger ceiling than ``/events`` (500): an export exists to be opened
#: somewhere else, and a page-sized one would be useless for the review it is
#: for. Still bounded — the whole file is built in memory before it is sent.
MAX_CSV_ROWS = 5000

#: What a spreadsheet treats as the start of a formula. ``-`` is on the list
#: because ``-2+3`` is arithmetic, and ``\t``/``\r`` because leading whitespace
#: is stripped by some importers before the first character is classified.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

#: Filename-safe tenant. The tenant reaches a ``Content-Disposition`` header,
#: and a header is exactly where an unfiltered value becomes a header-injection
#: question rather than a cosmetic one.
_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


@router.get("/events")
def get_audit_events(
    since: str | None = Query(None, description="ISO timestamp lower bound"),
    until: str | None = Query(None, description="ISO timestamp upper bound"),
    event_type: str | None = Query(None, description="Filter by event_type"),
    actor: str | None = Query(None, description="Filter by actor (agent_id)"),
    user_id: str | None = Query(None, description="Filter by user_id"),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """Query audit log events with filters."""
    try:
        events = query_log(
            limit=limit,
            event_type=event_type,
            actor=actor,
            since=since,
            user_id=user_id,
        )

        # Apply 'until' filter in Python (query_log doesn't support it natively)
        if until:
            events = [e for e in events if e.get("timestamp", "") <= until]

        return {"events": events, "count": len(events)}
    except Exception as e:
        logger.error("Audit events query failed: %s", e)
        return {"events": [], "count": 0, "error": "internal error"}


def _cell(value: Any) -> str:
    """One audit field as a CSV cell that is safe to open and safe to read.

    1. A ``dict``/``list`` becomes compact JSON, so a nested ``details`` is one
       cell rather than a shape the reader has to guess at.
    2. ``redact`` takes out anything credential-shaped. ``routers/_audit.py``
       says details carry identifiers only and the routes that use it obey, but
       ``log_event`` has callers across the whole platform and THIS is the
       route whose output leaves the appliance — into a downloads folder, an
       email, a ticket. The JSON ``/events`` route has no such pass; that
       asymmetry is deliberate, not an oversight.
    3. ``sanitize_log`` escapes every C0/C1 control character. That is what
       stops a newline inside a detail from forging a second CSV row, and what
       stops a CR from repositioning the cursor in a terminal that ``cat``s the
       file. It runs AFTER the redactor, whose shapes are written against
       ordinary text and would have to learn ``\\x3d`` to keep working on
       escaped input.
    4. The leading character is classified against BOTH spellings — what
       arrived and what redaction/sanitisation left — and either one being a
       formula opener earns the apostrophe.

    Checking both is the part worth stating. ``sanitize_log`` rewrites a
    leading tab or CR into the visible text ``\\x09`` / ``\\x0d``, so a cell
    that arrived as ``\\tSUM(...)`` no longer *looks* like a formula opener
    afterwards — and an importer that strips leading whitespace from the raw
    file before classifying would see the original again. Testing only the
    sanitised text would therefore pass a payload the spec names; testing only
    the raw text would miss nothing today but would depend on sanitisation
    never introducing a leading character of its own.

    The apostrophe is not stripped back out by ``csv``: it is part of the
    value, which is the point — a spreadsheet shows the text and evaluates
    nothing.
    """
    if isinstance(value, dict | list):
        value = json.dumps(value, default=str, separators=(",", ":"))
    raw = "" if value is None else str(value)
    text = sanitize_log(redact(raw))
    dangerous = raw.startswith(_FORMULA_PREFIXES) or text.startswith(_FORMULA_PREFIXES)
    return f"'{text}" if dangerous else text


def _filename(tenant_id: str) -> str:
    stamp = datetime.now(UTC).date().isoformat()
    tenant = _UNSAFE_IN_FILENAME.sub("-", tenant_id or "unknown")[:64] or "unknown"
    return f"audit-{tenant}-{stamp}.csv"


@router.get("/events.csv")
def export_audit_events_csv(
    request: Request,
    since: str | None = Query(None, description="ISO timestamp lower bound"),
    until: str | None = Query(None, description="ISO timestamp upper bound"),
    event_type: str | None = Query(None, description="Filter by event_type"),
    actor: str | None = Query(None, description="Filter by actor (agent_id)"),
    user_id: str | None = Query(None, description="Filter by user_id"),
    limit: int = Query(MAX_CSV_ROWS, ge=1, le=MAX_CSV_ROWS),
) -> Response:
    """The same rows as ``/events``, as a downloadable spreadsheet.

    Operator or auditor: an export leaves the appliance, so an agent's service
    token is refused here even when it carries ``audit:read`` and the
    middleware has already let it past.
    """
    require_audit_reader(request)
    from deps import get_tenant_id

    try:
        events = query_log(
            limit=limit,
            event_type=event_type,
            actor=actor,
            since=since,
            user_id=user_id,
        )
        if until:
            events = [e for e in events if e.get("timestamp", "") <= until]

        buffer = io.StringIO()
        # QUOTE_MINIMAL with \r\n is RFC 4180: a field containing a comma or a
        # quote is quoted, an embedded quote doubled, and nothing else changes.
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(CSV_COLUMNS)
        for event in events:
            writer.writerow([_cell(event.get(column)) for column in CSV_COLUMNS])
        body = buffer.getvalue()
    except Exception as e:  # noqa: BLE001 - deliberately a 500, see the module header
        logger.error("Audit CSV export failed: %s", sanitize_log(e))
        return JSONResponse({"error": "internal error"}, status_code=500)

    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{_filename(get_tenant_id(request))}"'
        },
    )


@router.get("/guardrails")
def get_guardrail_events(
    since: str | None = Query(None, description="ISO timestamp lower bound"),
    until: str | None = Query(None, description="ISO timestamp upper bound"),
    policy: str | None = Query(None, description="Filter by guardrail_name"),
    action: str | None = Query(None, description="Filter by action (blocked/warned/allowed)"),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """Query guardrail events."""
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()

            query = (
                "SELECT id, run_id, step_number, guardrail_name, action, "
                "tool_name, reason, created_at "
                "FROM agent_guardrail_events WHERE 1=1"
            )
            params: list[Any] = []

            if since:
                query += " AND created_at >= %s"
                params.append(since)
            if until:
                query += " AND created_at <= %s"
                params.append(until)
            if policy:
                query += " AND guardrail_name = %s"
                params.append(policy)
            if action:
                query += " AND action = %s"
                params.append(action)

            query += " ORDER BY created_at DESC LIMIT %s"
            params.append(limit)

            cur.execute(query, params)
            rows = cur.fetchall()

        events = [
            {
                "id": str(r[0]),
                "run_id": str(r[1]) if r[1] else None,
                "step_number": r[2],
                "guardrail_name": r[3],
                "action": r[4],
                "tool_name": r[5],
                "reason": r[6],
                "created_at": r[7].isoformat() if r[7] else None,
            }
            for r in rows
        ]

        return {"events": events, "count": len(events)}
    except Exception as e:
        logger.error("Guardrail events query failed: %s", e)
        return {"events": [], "count": 0, "error": "internal error"}


@router.get("/stats")
def get_audit_stats(
    hours: int = Query(24, ge=1, le=720, description="Rolling window in hours"),
) -> dict[str, Any]:
    """Aggregated audit statistics for the given time window."""
    try:
        from robothor.audit.logger import stats as audit_stats

        base_stats = audit_stats()

        # Add guardrail-specific stats
        guardrail_stats = _get_guardrail_stats(hours)

        return {
            "audit_log": base_stats,
            "guardrails": guardrail_stats,
            "window_hours": hours,
        }
    except Exception as e:
        logger.error("Audit stats failed: %s", e)
        return {"error": "internal error"}


def _get_guardrail_stats(hours: int) -> dict[str, Any]:
    """Get guardrail event statistics for the given time window."""
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()

            cur.execute(
                """
                SELECT
                    guardrail_name,
                    action,
                    COUNT(*) as count
                FROM agent_guardrail_events
                WHERE created_at >= now() - interval '%s hours'
                GROUP BY guardrail_name, action
                ORDER BY count DESC
                """,
                (hours,),
            )
            rows = cur.fetchall()

        by_policy: dict[str, dict[str, int]] = {}
        total = 0
        for name, action, count in rows:
            by_policy.setdefault(name, {})[action] = count
            total += count

        return {
            "total_events": total,
            "by_policy": by_policy,
        }
    except Exception as e:
        logger.warning("Guardrail stats failed: %s", e)
        return {"total_events": 0, "error": str(e)}
