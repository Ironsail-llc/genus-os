"""A model can select a goal, but cannot supply report facts or bypass access."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from robothor.engine.goal_report_delivery import (
    finish_goal_report,
    record_report_turn,
    report_scope,
)
from robothor.engine.models import TriggerType
from robothor.engine.session import AgentSession
from robothor.engine.tools.dispatch import ToolContext
from robothor.goals.tests.test_store import create, db, private_database  # noqa: F401
from robothor.goals.tools import HANDLERS
from robothor.identity import IdentityContext


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing_id",
        "foreign_goal",
        "viewer",
        "extra_facts",
        "noninteractive",
        "valid",
        "compound",
        "unperformed_control",
        "stale_control",
    ],
)
async def test_report_reads_authorized_goal_facts_only(db, case):  # noqa: F811
    goal = create(str(uuid4()) if case == "foreign_goal" else db)
    role = "viewer" if case == "viewer" else "owner"
    session = AgentSession(
        "main", TriggerType.CRON if case == "noninteractive" else TriggerType.WEBCHAT, tenant_id=db
    )
    session.run.task_text = "What's finished, and what's still left?"
    if case == "compound":
        session.run.task_text += " Also calculate 17 times 19."
    elif case in {"unperformed_control", "stale_control"}:
        session.run.task_text = "Pause that work."
    ctx = ToolContext(
        agent_id="main",
        run_id=session.run.id,
        tenant_id=db,
        user_id="operator",
        user_role=role,
        identity=IdentityContext(db, "webchat", "operator", True, role=role),
    )
    args = {} if case == "missing_id" else {"goal_id": goal["id"]}
    if case == "extra_facts":
        args["message"] = "Everything is complete"
    req = SimpleNamespace(session=session, readonly_mode=False)
    if case == "stale_control":
        import json

        from robothor.engine.models import RunStep, StepType

        req.assistant_msg = SimpleNamespace(
            tool_calls=[SimpleNamespace(function=SimpleNamespace(arguments=json.dumps(args)))]
        )
        session.run.steps.append(
            RunStep(
                step_type=StepType.TOOL_CALL,
                tool_name="update_pursuit_goal",
                tool_input={"action": "pause", "goal_id": goal["id"]},
                tool_output={"goal": {"id": goal["id"], "status": "paused"}},
            )
        )
    with report_scope(req, ["report_pursuit_goal"]) as state:
        result = await HANDLERS["report_pursuit_goal"](args, ctx)
    record_report_turn(state, session, [])
    if case == "valid":
        assert result["goal"]["id"] == goal["id"]
        assert result["goal"]["version"] == goal["version"]
        assert finish_goal_report(session)
        assert "The goal is not complete" in session.get_final_text()
    elif case in {"compound", "unperformed_control", "stale_control"}:
        assert result["goal"]["id"] == goal["id"]
        assert result["report_prepared"] is False
        assert result["goal"]["status"] == "queued"
        assert not finish_goal_report(session)
    else:
        assert "error" in result and "goal" not in result
        assert not finish_goal_report(session)
        assert session.messages == []
