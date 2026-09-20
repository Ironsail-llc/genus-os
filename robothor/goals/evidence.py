"""Independent checks for supported evidence references, without another model call."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID


def verify(
    cur: Any, tenant: str, goal: dict, reference: str, criterion: int | None = None
) -> dict[str, Any]:
    kind, _, identifier = reference.partition(":")
    if kind != "calendar-operation":
        return {"method": "agent_assessment", "independent": False}
    identifier = str(UUID(identifier))
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
        "method": "calendar_operation_receipt",
        "independent": True,
        # A receipt proves this operation completed, not an arbitrary business claim.
        # Only an explicitly bound criterion has an independently checked outcome.
        "criterion_verified": (
            criterion is not None
            and 0 <= criterion < len(goal.get("success_criteria", []))
            and goal["success_criteria"][criterion].strip() == reference
        ),
        "observed_at": row["updated_at"].isoformat(),
    }
