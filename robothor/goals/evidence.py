"""Independent checks for supported evidence references, without another model call.

Two calendar references are understood:

* ``calendar-effect:<uuid>`` — a direct calendar edit (``gws_calendar_update``,
  ``gws_calendar_add_attendees``, ``gws_calendar_respond``) journalled in the
  runtime effect ledger. The tool result names it as ``evidence_reference``.
* ``calendar-operation:<uuid>`` — a record from the retired attendee draft flow,
  kept so goals that already cite one still verify.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from robothor.engine.tools.constants import CALENDAR_WRITE_TOOLS

#: Effect-ledger tools whose verified result is calendar evidence.
CALENDAR_EFFECT_TOOLS = CALENDAR_WRITE_TOOLS


def _operation_row(cur: Any, tenant: str, identifier: str) -> Any:
    cur.execute(
        """SELECT status,result,updated_at FROM calendar_operations
                   WHERE tenant_id=%s AND id=%s""",
        (tenant, identifier),
    )
    row = cur.fetchone()
    if (
        not row
        or row["status"] != "completed"
        or (row["result"] or {}).get("verification") != "verified"
    ):
        raise ValueError(
            "calendar evidence requires a completed, verified operation in this tenant"
        )
    return row


def _effect_row(cur: Any, tenant: str, identifier: str) -> Any:
    cur.execute(
        """SELECT tool_name,state,resolution,updated_at FROM agent_runtime_effects
                   WHERE tenant_id=%s AND id=%s""",
        (tenant, identifier),
    )
    row = cur.fetchone()
    resolution = (row or {}).get("resolution") or {}
    result = resolution.get("result") if isinstance(resolution, dict) else None
    if (
        not row
        or row["tool_name"] not in CALENDAR_EFFECT_TOOLS
        or row["state"] not in ("finished", "confirmed")
        or not isinstance(result, dict)
        or result.get("error")
        or result.get("verification") != "verified"
    ):
        raise ValueError("calendar evidence requires a verified calendar edit in this tenant")
    return row


def verify(
    cur: Any, tenant: str, goal: dict[str, Any], reference: str, criterion: int | None = None
) -> dict[str, Any]:
    kind, _, identifier = reference.partition(":")
    if kind == "calendar-operation":
        row = _operation_row(cur, tenant, str(UUID(identifier)))
        method = "calendar_operation_receipt"
    elif kind == "calendar-effect":
        row = _effect_row(cur, tenant, str(UUID(identifier)))
        method = "calendar_effect_receipt"
    else:
        return {"method": "agent_assessment", "independent": False}
    since = max(
        datetime.fromisoformat(value)
        for value in (
            goal["created_at"],
            goal.get("criteria_revised_at", goal["created_at"]),
            (goal.get("assessment") or {}).get("at", goal["created_at"]),
        )
    )
    if row["updated_at"] < since:
        raise ValueError("evidence predates this goal, criteria revision or assessment period")
    return {
        "method": method,
        "independent": True,
        # A receipt proves this change completed, not an arbitrary business claim.
        # Only an explicitly bound criterion has an independently checked outcome.
        "criterion_verified": (
            criterion is not None
            and 0 <= criterion < len(goal.get("success_criteria", []))
            and goal["success_criteria"][criterion].strip() == reference
        ),
        "observed_at": row["updated_at"].isoformat(),
    }
