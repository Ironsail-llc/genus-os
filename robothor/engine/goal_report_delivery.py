"""Native final-report admission and delivery; ordinary tool data cannot finalize."""

from datetime import UTC, datetime

from robothor.engine.models import RunStep, StepType
from robothor.goals.report_channel import consume_report, report_turn
from robothor.goals.runtime import binding

REPORT_TOOL = "report_pursuit_goal"


def report_scope(req, names):
    from robothor.engine.goal_report_intent import admission
    from robothor.engine.runtime.task_report import requested

    finalizes, statuses = admission(req) if names == [REPORT_TOOL] else (False, None)
    run = req.session.run
    task_report = requested(req, names)
    enabled = (
        (names == [REPORT_TOOL] or task_report)
        and run.agent_id == "main"
        and str(run.trigger_type) in {"webchat", "telegram"}
        and not run.parent_run_id
        and not run.is_benchmark
        and not req.readonly_mode
        and binding.get() is None
        and not getattr(req.session, "routine_operation_id", None)
    )
    return report_turn(
        run.tenant_id,
        run.agent_id,
        run.id,
        enabled=enabled,
        tool_name="create_task" if task_report else REPORT_TOOL,
        finalizes=task_report or finalizes,
        allowed_goal_statuses=statuses,
    )


def record_report_turn(state, session, errors):
    message = consume_report(state, session.run)
    session.pending_goal_report = message if not errors and state.finalizes else None
    session.pending_goal_report_tool = state.tool_name


def finish_goal_report(session):
    message = getattr(session, "pending_goal_report", None)
    session.pending_goal_report = None
    if message is None or session.has_pending_control:
        return False
    tool = getattr(session, "pending_goal_report_tool", REPORT_TOOL)
    now = datetime.now(UTC)
    session._step_counter += 1
    session.run.steps.append(
        RunStep(
            run_id=session.run.id,
            step_number=session._step_counter,
            step_type=StepType.CHECKPOINT,
            tool_name=tool,
            tool_output={
                "origin": "trusted_task_report" if tool == "create_task" else "trusted_goal_report",
                "output": message,
            },
            started_at=now,
            completed_at=now,
            duration_ms=0,
        )
    )
    session.messages.append({"role": "assistant", "content": message})
    session.goal_report_complete = True
    return True
