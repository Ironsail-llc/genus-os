"""Read supported business receipts referenced by the authenticated run's tool audit."""

from uuid import UUID


def _reference(value):
    if value is None:
        return None
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return "invalid-reference"


def calendar_receipts(cur, run, auth):
    cur.execute(
        """SELECT tool_name,tool_input,tool_output FROM agent_run_steps
           WHERE run_id=%s AND tool_name IN ('gws_calendar_add_attendees','tool_call')
           ORDER BY step_number DESC""",
        (run["id"],),
    )
    identifiers, conflicts = set(), set()
    for step in cur.fetchall():
        inputs = step["tool_input"] if isinstance(step["tool_input"], dict) else {}
        if step["tool_name"] == "tool_call":
            # The deferred dispatcher unwraps arguments but returns the tool's
            # result directly. Only its explicit calendar target is evidence.
            if str(inputs.get("name", "")).strip() != "gws_calendar_add_attendees":
                continue
            inputs = inputs.get("arguments", {})
            if not isinstance(inputs, dict):
                continue
        outputs = step["tool_output"] if isinstance(step["tool_output"], dict) else {}
        before, after = (
            _reference(inputs.get("operation_id")),
            _reference(outputs.get("operation_id")),
        )
        if before and after and before != after:
            if isinstance(before, str):
                identifiers.add(before)
                conflicts.add(before)
            continue
        identifier = before or after
        if isinstance(identifier, str):
            identifiers.add(identifier)
    if not identifiers:
        return []
    cur.execute(
        """SELECT id,status,result FROM calendar_operations
           WHERE tenant_id=%s AND user_id=%s AND agent_id=%s AND id::text=ANY(%s)
           ORDER BY created_at,id""",
        (auth.tenant_id, auth.user_id, run["agent_id"], sorted(identifiers - conflicts)),
    )
    receipts = []
    for row in cur.fetchall():
        result = row["result"] if isinstance(row["result"], dict) else {}
        verified = (
            row["status"] == "completed"
            and result.get("verification") == "verified"
            and not result.get("error")
        )
        receipts.append(
            {
                "operation_id": str(row["id"]),
                "kind": "calendar_attendees",
                "status": row["status"],
                "verified": verified,
                "attendees_present": [
                    email for email in result.get("attendees_present", []) if isinstance(email, str)
                ]
                if isinstance(result.get("attendees_present"), list)
                else [],
                "invitations_requested": result.get("invitations_requested")
                if isinstance(result.get("invitations_requested"), bool)
                else None,
            }
        )
    matched = {receipt["operation_id"] for receipt in receipts}
    receipts.extend(
        {
            "operation_id": identifier,
            "kind": "calendar_attendees",
            "status": "unmatched",
            "verified": False,
            "invitations_requested": None,
        }
        for identifier in sorted(identifiers - matched)
    )
    return receipts


def receipt_summary(receipts):
    lines = []
    for receipt in receipts:
        if receipt["verified"]:
            finding = "The calendar change is recorded as complete and verified."
            if receipt["invitations_requested"] is True:
                finding += " Notifications were requested; delivery is not verified."
        elif receipt["status"] == "draft":
            finding = "Only a calendar draft is recorded."
        else:
            finding = "The calendar change is not fully verified in its operation record."
        if receipt.get("attendees_present"):
            finding += (
                " Readback found these requested attendees: "
                + ", ".join(receipt["attendees_present"])
                + "."
            )
            if receipt["invitations_requested"] is None:
                finding += " Whether notifications were sent remains unknown."
        lines.append(f"{finding} (Operation {receipt['operation_id']})")
    return "\n\n".join(lines)
