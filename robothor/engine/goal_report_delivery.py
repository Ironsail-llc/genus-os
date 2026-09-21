"""Native final-report admission and delivery; ordinary tool data cannot finalize."""

from datetime import UTC, datetime

from robothor.engine.models import RunStep, StepType
from robothor.goals.report_channel import consume_report, report_turn
from robothor.goals.runtime import binding

REPORT_TOOL = "report_pursuit_goal"


def report_scope(req, names):
    run = req.session.run
    enabled = (
        names == [REPORT_TOOL]
        and run.agent_id == "main"
        and str(run.trigger_type) in {"webchat", "telegram"}
        and not run.parent_run_id
        and not run.is_benchmark
        and not req.readonly_mode
        and binding.get() is None
        and not getattr(req.session, "routine_operation_id", None)
    )
    return report_turn(run.tenant_id, run.agent_id, run.id, enabled=enabled)


def record_report_turn(state, session, errors):
    message = consume_report(state, session.run)
    session.pending_goal_report = message if not errors else None


def finish_goal_report(session):
    message = getattr(session, "pending_goal_report", None)
    session.pending_goal_report = None
    if message is None or session.has_pending_control:
        return False
    now = datetime.now(UTC)
    session._step_counter += 1
    session.run.steps.append(
        RunStep(
            run_id=session.run.id,
            step_number=session._step_counter,
            step_type=StepType.CHECKPOINT,
            tool_name=REPORT_TOOL,
            tool_output={"origin": "trusted_goal_report", "output": message},
            started_at=now,
            completed_at=now,
            duration_ms=0,
        )
    )
    session.messages.append({"role": "assistant", "content": message})
    session.goal_report_complete = True
    return True
