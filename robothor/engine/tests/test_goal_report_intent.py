"""Final goal reports must cover the whole recognized request."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from robothor.engine.goal_report_delivery import (
    finish_goal_report,
    record_report_turn,
    report_scope,
)
from robothor.engine.goal_report_intent import admission
from robothor.engine.tests.test_goal_report_delivery import setup_turn
from robothor.goals.report_channel import publish_report


@pytest.mark.parametrize(
    "text",
    [
        "What's finished, and what's still left? Also calculate 17 times 19.",
        "Pause that work. Then email Sam.",
        "Report goal progress in Spanish.",
        "Tell me everything about all goals.",
        "",
    ],
)
def test_extra_or_ambiguous_work_gets_facts_without_early_completion(text):
    session, req, ctx = setup_turn()
    session.run.task_text = text
    with report_scope(req, ["report_pursuit_goal"]) as state:
        assert state.enabled  # The read/report tool remains available.
        publish_report(ctx, "The goal is waiting. One task remains open.")
    record_report_turn(state, session, [])
    assert not finish_goal_report(session)
    assert session.messages == []


@pytest.mark.parametrize(
    "text,statuses",
    [
        ("What's finished, and what's still left?", None),
        ("Pause that work.", ("paused",)),
        ("Resume this goal.", ("queued", "running")),
        ("Cancel my goal.", ("canceled",)),
    ],
)
def test_recognized_scope_records_required_control_result(text, statuses):
    session, req, _ = setup_turn()
    session.run.task_text = text
    if statuses:
        from robothor.engine.models import RunStep, StepType

        goal = str(uuid4())
        action = text.split()[0].lower()
        req.assistant_msg = SimpleNamespace(
            tool_calls=[
                SimpleNamespace(function=SimpleNamespace(arguments=json.dumps({"goal_id": goal})))
            ]
        )
        assert admission(req) == (False, None)
        session.run.steps.append(
            RunStep(
                step_type=StepType.TOOL_CALL,
                tool_name="update_pursuit_goal",
                tool_input={"action": action, "goal_id": goal},
                tool_output={"goal": {"id": goal}},
            )
        )
    assert admission(req) == (True, statuses)


@pytest.mark.parametrize("prefix", ["Report progress for goal", "What is the status of goal"])
def test_explicit_goal_identity_must_match(prefix):
    session, req, _ = setup_turn()
    goal = str(uuid4())
    session.run.task_text = f"{prefix} {goal}."
    function = SimpleNamespace(arguments=json.dumps({"goal_id": str(uuid4())}))
    req.assistant_msg = SimpleNamespace(tool_calls=[SimpleNamespace(function=function)])
    assert admission(req) == (False, None)
    function.arguments = json.dumps({"goal_id": goal})
    assert admission(req) == (True, None)


def test_consumed_extra_steering_still_prevents_early_completion():
    from robothor.engine.task_context import install_context, make_context

    session, req, _ = setup_turn()
    session.messages = [{"role": "system", "content": "Synthetic"}]
    install_context(session.messages, make_context(session.run.task_text, []))
    session.steer("Also calculate 17 times 19")
    session.consume_pending_steer()
    assert not session.has_pending_control
    assert admission(req) == (False, None)


@pytest.mark.parametrize("case", ["resumed", "approved", "skill", "prior_tool", "todo"])
def test_other_work_cannot_be_hidden_by_a_goal_report(case):
    from robothor.engine.models import RunStep, StepType
    from robothor.engine.skill_contract import SKILL_TEXT_ATTR

    session, req, _ = setup_turn()
    if case == "resumed":
        session.run.resume_from_run_id = "prior"
    elif case == "approved":
        session.run.trigger_detail = "plan-exec:chat"
    elif case == "skill":
        setattr(session, SKILL_TEXT_ATTR, {"skill": "Also perform another action"})
    elif case == "prior_tool":
        session.run.steps.append(RunStep(step_type=StepType.TOOL_CALL, tool_name="create_task"))
    else:
        session.todo_list = SimpleNamespace(items=[SimpleNamespace(status="pending")])
    assert admission(req) == (False, None)
