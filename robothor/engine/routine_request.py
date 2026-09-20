"""Execute an explicitly confirmed calendar draft through normal tool admission.

No model is needed to reinterpret 'Go'. The stored operation supplies the
arguments; the existing permission, identity, benchmark and hook gates still
decide whether its tool may run.
"""

from __future__ import annotations

import json
from typing import Any

TOOL = "gws_calendar_add_attendees"


async def bind_confirmation(session: Any, message: str, history: list[dict[str, Any]]) -> None:
    from robothor.engine.calendar_operations import confirmation_id
    from robothor.settings import get_settings

    if not get_settings().engine.calendar_operations_enabled or session.run.is_benchmark:
        return
    operation_id = confirmation_id(message, history)
    if not operation_id or TOOL not in session.run.tools_provided:
        return
    session.routine_operation_id = operation_id
    session.messages.append(
        {
            "role": "developer",
            "content": (
                "[ACTIVE REQUEST]\nExecute only the confirmed calendar operation "
                + operation_id
                + ". Its arguments are stored durably. Report the result immediately; integration repair is separate."
            ),
        }
    )


def confirmed_response(session: Any) -> Any:
    import litellm

    call = {
        "id": "confirmed_calendar_operation",
        "type": "function",
        "function": {
            "name": TOOL,
            "arguments": json.dumps({"operation_id": session.routine_operation_id}),
        },
    }
    message = {"role": "assistant", "content": None, "tool_calls": [call]}
    session.messages.append(message)
    return litellm.ModelResponse(choices=[{"message": message, "finish_reason": "tool_calls"}])


async def finish_confirmation(session: Any, on_content: Any) -> None:
    from robothor.engine.loop_guards import _interrupted

    if _interrupted(session):
        return
    steps = [s for s in session.run.steps if s.tool_name == TOOL and s.tool_output]
    result = steps[-1].tool_output if steps else {"error": "The operation did not execute"}
    if not isinstance(result, dict):
        result = {"error": "The operation returned no structured result"}
    if result.get("error"):
        text = "The calendar update could not be fully verified. " + str(result["error"])
        # Do not start a second autonomous agent or grant it permission to
        # re-send the invite. A repair task requires explicit reconciliation.
        text += " No further calendar changes will be attempted in this request."
        if result.get("operation_id"):
            text += " Repair record: " + str(result["operation_id"])
        if result.get("repair_task_id"):
            text += " Separate repair task: " + str(result["repair_task_id"])
    elif result.get("replayed"):
        text = "This operation was completed previously. No new calendar changes or invitations were requested."
    elif result.get("status") == "already_present":
        text = "The requested attendees are already on the meeting. No duplicate invitations were requested."
    elif result.get("verification") == "verified" and result.get("invitations_requested") is True:
        text = (
            "Added "
            + ", ".join(result.get("added", []))
            + " to the meeting. Calendar notifications requested."
        )
    else:
        text = "The calendar operation returned no verified completion. No further changes will be attempted."
    text += "\n\nCalendar operation: " + session.routine_operation_id
    session.messages.append({"role": "assistant", "content": text})
    if on_content:
        await on_content(text)


def attach_draft_reference(session: Any, text: str | None) -> str | None:
    """The confirmation binding must survive an LLM omitting the draft marker."""
    from robothor.engine.run_verification import resolve_tool_name

    results = [
        s.tool_output
        for s in session.run.steps
        if resolve_tool_name(s) == TOOL and isinstance(s.tool_output, dict)
    ]
    if not results or results[-1].get("status") != "draft" or results[-1].get("error"):
        return text
    operation_id = results[-1].get("operation_id")
    if not operation_id:
        return text
    marker = "Calendar operation: " + str(operation_id)
    if marker in (text or ""):
        return text
    return (text or "Calendar draft prepared. No invitations sent.") + "\n\n" + marker
