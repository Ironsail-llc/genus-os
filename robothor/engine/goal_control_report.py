"""Read back an authorized standalone goal control before finishing its reply."""

import asyncio
import json
import logging

from robothor.engine.goal_report_delivery import REPORT_TOOL, record_report_turn, report_scope
from robothor.engine.tools.dispatch import ToolContext
from robothor.goals import store
from robothor.goals.presentation import render_goal_progress
from robothor.goals.report_channel import publish_report

logger = logging.getLogger(__name__)


async def prepare_control_report(req, names, errors, recorded_steps):
    """No new business action: only the current committed control can admit this read."""
    session = req.session
    if names != ["update_pursuit_goal"] or errors or session.has_pending_control:
        return
    call = req.assistant_msg.tool_calls[0]
    try:
        args = json.loads(call.function.arguments)
    except (TypeError, ValueError):
        return
    if not isinstance(args, dict):
        return
    if args.get("action") not in {"pause", "resume", "cancel"}:
        return
    steps = [step for step in recorded_steps if str(step.step_type) == "tool_call"]
    if len(steps) != 1:
        return
    step = steps[0]
    if (
        step is None
        or step.tool_name != "update_pursuit_goal"
        or step.error_message
        or step.tool_input != args
        or not isinstance(step.tool_output, dict)
        or step.tool_output.get("error")
    ):
        return
    receipt = step.tool_output.get("goal") or {}
    if receipt.get("id") != args.get("goal_id") or not isinstance(receipt.get("version"), int):
        return
    with report_scope(req, [REPORT_TOOL]) as state:
        if not state.enabled or not state.finalizes or not state.allowed_goal_statuses:
            return
        try:
            goal = await asyncio.to_thread(store.get, session.run.tenant_id, receipt["id"])
            enabled = await asyncio.to_thread(store.enabled, session.run.tenant_id)
            if (
                goal["id"] != receipt["id"]
                or goal["version"] != receipt["version"]
                or goal["status"] != receipt["status"]
                or goal["status"] not in state.allowed_goal_statuses
                or session.has_pending_control
            ):
                return
            publish_report(
                ToolContext(
                    tenant_id=session.run.tenant_id,
                    agent_id=session.run.agent_id,
                    run_id=session.run.id,
                ),
                render_goal_progress(goal, execution_enabled=enabled),
            )
        except Exception:
            # The control is already committed. Leave its receipt intact and let
            # ordinary delivery/recovery handle a failed read; never retry the write.
            logger.warning("Could not prepare committed goal control report", exc_info=True)
            return
    record_report_turn(state, session, [])
