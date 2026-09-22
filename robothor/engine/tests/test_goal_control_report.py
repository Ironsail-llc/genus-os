"""Committed controls finish only when the whole request and readback agree."""

import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from robothor.engine.goal_control_report import prepare_control_report
from robothor.engine.goal_report_delivery import finish_goal_report
from robothor.engine.models import RunStep, StepType
from robothor.engine.tests.test_goal_report_delivery import setup_turn


@pytest.mark.parametrize(
    "action,status", [("pause", "paused"), ("resume", "queued"), ("cancel", "canceled")]
)
@pytest.mark.parametrize(
    "case",
    [
        "success",
        "compound",
        "failed",
        "version",
        "status",
        "foreign",
        "read_failure",
        "steer",
        "batch",
        "old_receipt",
    ],
)
async def test_committed_control_report(action, status, case, monkeypatch):
    session, req, _ = setup_turn()
    goal_id = str(uuid4())
    session.run.task_text = f"{action} goal {goal_id}"
    args = {"goal_id": goal_id, "version": 1, "action": action}
    req.assistant_msg = SimpleNamespace(
        tool_calls=[
            SimpleNamespace(id="control", function=SimpleNamespace(arguments=json.dumps(args)))
        ]
    )
    receipt = {"id": goal_id, "version": 2, "status": status}
    step = RunStep(
        step_type=StepType.TOOL_CALL,
        tool_name="update_pursuit_goal",
        tool_input=args,
        tool_output={"goal": receipt},
    )
    session.run.steps.append(step)
    goal = dict(
        receipt,
        objective="Synthetic work",
        mode="one_off",
        tasks=[{"title": "Remaining check", "status": "TODO"}],
        children=[],
        evidence=[],
    )
    if case == "compound":
        session.run.task_text += ". Then calculate 17 times 19."
    elif case == "failed":
        step.error_message = "Not authorized"
    elif case == "version":
        goal["version"] = 3
    elif case == "status":
        goal["status"] = "waiting"
    elif case == "foreign":
        goal["id"] = str(uuid4())
    read = Mock(return_value=goal)
    if case == "read_failure":
        read.side_effect = RuntimeError("Disconnected")
    if case == "steer":

        def read_with_steering(*args):
            session.steer("Also check another goal")
            return goal

        read.side_effect = read_with_steering
    monkeypatch.setattr("robothor.goals.store.get", read)
    monkeypatch.setattr("robothor.goals.store.enabled", Mock(return_value=False))
    names = ["update_pursuit_goal"] + (["get_pursuit_goal"] if case == "batch" else [])
    await prepare_control_report(req, names, [], [] if case == "old_receipt" else [step])
    assert finish_goal_report(session) is (case == "success")
    assert step.tool_output == {
        "goal": receipt
    }  # Failed readback never erases the committed result.
    if case == "success":
        text = session.messages[-1]["content"]
        assert status in text and "Remaining check" in text and "not complete" in text
        assert session.run.steps[-1].tool_output["origin"] == "trusted_goal_report"
        read.assert_called_once_with("tenant", goal_id)
