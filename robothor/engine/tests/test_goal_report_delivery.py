"""Final reports cannot bypass controls, identity, batch boundaries or validators."""

from types import SimpleNamespace

import pytest

from robothor.engine.goal_report_delivery import (
    finish_goal_report,
    record_report_turn,
    report_scope,
)
from robothor.engine.models import TriggerType
from robothor.engine.output_validation import output_validation_scope, validated_completion
from robothor.engine.session import AgentSession
from robothor.engine.tools.dispatch import ToolContext
from robothor.goals.report_channel import publish_report


def setup_turn():
    session = AgentSession("main", TriggerType.WEBCHAT, tenant_id="tenant")
    req = SimpleNamespace(session=session, readonly_mode=False)
    ctx = ToolContext(agent_id="main", tenant_id="tenant", run_id=session.run.id)
    return session, req, ctx


@pytest.mark.parametrize(
    "case", ["batch", "ordinary", "delegated", "background", "readonly", "benchmark"]
)
def test_report_capability_is_not_available_in_other_tool_turns(case):
    session, req, ctx = setup_turn()
    names = ["report_pursuit_goal"]
    if case == "batch":
        names.append("update_pursuit_goal")
    elif case == "ordinary":
        names = ["get_pursuit_goal"]
    elif case == "delegated":
        session.run.parent_run_id = "parent"
    elif case == "background":
        session.run.trigger_type = TriggerType.CRON
    elif case == "readonly":
        req.readonly_mode = True
    else:
        session.run.is_benchmark = True
    with report_scope(req, names) as state:
        with pytest.raises(ValueError, match="unavailable"):
            publish_report(ctx, "Unadmitted final response")
    record_report_turn(state, session, [])
    assert not finish_goal_report(session)
    assert session.messages == [] and session.run.steps == []


@pytest.mark.parametrize("control", ["steer", "interrupt", "error"])
def test_late_controls_and_tool_errors_prevent_final_report(control):
    session, req, ctx = setup_turn()
    with report_scope(req, ["report_pursuit_goal"]) as state:
        publish_report(ctx, "Earlier report")
        if control == "steer":
            session.steer("Also check the other goal")
        elif control == "interrupt":
            session.interrupt("Stop")
    record_report_turn(
        state, session, [("report_pursuit_goal", "failed", None)] if control == "error" else []
    )
    assert not finish_goal_report(session)
    assert session.messages == [] and session.run.steps == []
    if control == "steer":
        assert session.consume_pending_steer() == "Also check the other goal"
    elif control == "interrupt":
        assert session.consume_interrupt() == "Stop"
    assert not finish_goal_report(session)  # discarded reports never reappear later


def test_report_records_host_origin_and_retains_final_output_validation():
    session, req, ctx = setup_turn()
    with report_scope(req, ["report_pursuit_goal"]) as state:
        publish_report(ctx, "The goal is paused; one task remains open.")
    record_report_turn(state, session, [])
    assert finish_goal_report(session)
    assert session.run.steps[-1].tool_output["origin"] == "trusted_goal_report"
    assert session.run.steps[-1].input_tokens is None
    assert not finish_goal_report(session)
    with output_validation_scope(lambda run, text: "Other required outcome is missing"):
        run = validated_completion(session, session.get_final_text())
    assert str(run.status) == "failed"
    assert "Other required outcome is missing" in run.error_message
