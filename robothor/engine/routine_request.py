"""Execute an explicitly confirmed calendar draft through normal tool admission.

No model is needed to reinterpret 'Go'. The stored operation supplies the
arguments; the existing permission, identity, benchmark and hook gates still
decide whether its tool may run.

A binding is DELIBERATELY narrow, because the alternative was a trap. Before
round-1 review a bare "yes" was hijacked into a replay of whatever operation id
the previous assistant turn mentioned — with no model call, no status check,
and the marker repeated on the terminal reply, so the next "yes" matched again
and the conversation could never leave the replay. A binding now requires a
stored operation that is still a DRAFT, belongs to this requester, and is
consumed exactly once.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

# The pin is an engine-set KEY, owned by ``compaction``: content is model- and
# tool-controlled, a key the engine writes on the dict is not, and
# ``llm_client`` strips it before the payload reaches a provider.
from robothor.engine.compaction import ACTIVE_REQUEST_PIN, PIN_KEY

logger = logging.getLogger(__name__)

TOOL = "gws_calendar_add_attendees"


def bound_toolset(session: Any, tool_schemas: list[Any]) -> tuple[bool, list[Any]]:
    """Is this run a bound confirmation, and what may it call?

    The flag gates the planner, the verifier and compaction for the whole run;
    the operation id is consumed on first use, so it cannot answer this. The
    permission set is still checked by ``run_tool_turn`` — narrowing the
    schemas only stops the run offering anything the confirmation did not.
    """
    if not getattr(session, "routine_operation_bound", False):
        return False, tool_schemas
    from robothor.engine.tools.schemas import get_engine_schemas

    return True, [get_engine_schemas()[TOOL]]


async def bind_confirmation(
    session: Any,
    message: str,
    history: list[dict[str, Any]] | None,
    agent_config: Any = None,
    readonly_mode: bool = False,
) -> None:
    """Bind a bare confirmation to ITS OWN still-open draft, or bind nothing.

    Scoped to this requester (``agent_config`` supplies the agent half of the
    key the operation was stored under) and skipped in plan mode, where the
    turn owes the operator a plan rather than a replayed write.
    """
    from robothor.engine.calendar_operations import confirmation_id
    from robothor.settings import get_settings

    agent_id = str(getattr(agent_config, "id", "") or "")

    if readonly_mode:
        # Plan mode owes the operator a plan. Replaying a stored write is not
        # one, and the bound path has no planner to fall back to.
        return
    if not get_settings().engine.calendar_operations_enabled or session.run.is_benchmark:
        return
    operation_id = confirmation_id(message, history or [])
    if not operation_id or TOOL not in session.run.tools_provided:
        return
    row = await _load(session, operation_id, agent_id)
    # Only a draft is outstanding work. `completed`, `blocked` and `executing`
    # are all answers already given, and replaying one is the C1 trap.
    if row is None or row.get("status") != "draft":
        return
    session.routine_operation_id = operation_id
    # Durable for the whole run: the planner, the verifier and compaction
    # are all off for a bound confirmation, and consuming the id must not
    # silently turn them back on mid-run.
    session.routine_operation_bound = True
    session.messages.append(
        {
            "role": "developer",
            PIN_KEY: ACTIVE_REQUEST_PIN,
            "content": (
                "[ACTIVE REQUEST]\nExecute only the confirmed calendar operation "
                + operation_id
                + ". Its arguments are stored durably. Report the result immediately; integration repair is separate."
            ),
        }
    )


async def _load(session: Any, operation_id: str, agent_id: str) -> dict[str, Any] | None:
    """The stored operation, scoped to this requester. Never fatal.

    A database that cannot answer is not an authorization to replay: the turn
    falls through to the ordinary model path, which is the safe direction.
    """
    from robothor.engine.calendar_operations import load_operation

    try:
        return await asyncio.to_thread(
            load_operation,
            operation_id,
            session.run.tenant_id,
            session.run.user_id,
            agent_id or session.run.agent_id,
        )
    except Exception as exc:
        logger.warning("Confirmation binding could not be verified: %s", type(exc).__name__)
        return None


def confirmed_response(session: Any) -> Any:
    """Synthesise the one tool call this binding authorizes, and consume it."""
    import litellm

    operation_id = session.routine_operation_id
    # One use. A second loop iteration must reach the model rather than replay
    # a write the first iteration already attempted.
    session.routine_operation_id = ""
    call = {
        "id": "confirmed_calendar_operation",
        "type": "function",
        "function": {
            "name": TOOL,
            "arguments": json.dumps({"operation_id": operation_id}),
        },
    }
    message = {"role": "assistant", "content": None, "tool_calls": [call]}
    session.messages.append(message)
    return litellm.ModelResponse(choices=[{"message": message, "finish_reason": "tool_calls"}])


_CALENDAR_KINDS = {
    "operator": "the operator's calendar",
    "own": "the assistant's OWN calendar",
    "other": "a third-party calendar",
}


def calendar_phrase(result: dict[str, Any]) -> str:
    """Name the account the change landed in, in every outcome.

    This instance once put the operator's itinerary on the ASSISTANT's calendar
    and reported it as done. The verifier that would catch such a claim is
    disabled on the bound path, so the sentence is produced here instead of
    being left to a model.
    """
    block = result.get("calendar")
    if not isinstance(block, dict) or not block.get("id"):
        return " The calendar it applied to was not reported."
    kind = _CALENDAR_KINDS.get(str(block.get("kind")), "an unidentified calendar")
    return f" Calendar: {kind} ({block['id']})."


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
        if result.get("repair_task_error"):
            text += (
                " No repair task could be filed, so this operation is recorded as unreconciled "
                "and does NOT block later changes to this meeting. Check the meeting before retrying."
            )
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
    text += calendar_phrase(result)
    # NO operation marker here. The marker is a confirmation offer, and this
    # reply is the end of the operation, not an offer to run it again.
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
    # Only a live draft may advertise a confirmation. Anything else is an
    # outcome, and offering "Go" against an outcome is the C1 replay trap.
    if not results or results[-1].get("status") != "draft" or results[-1].get("error"):
        return text
    operation_id = results[-1].get("operation_id")
    if not operation_id:
        return text
    marker = "Calendar operation: " + str(operation_id)
    if marker in (text or ""):
        return text
    return (text or "Calendar draft prepared. No invitations sent.") + "\n\n" + marker
