from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from robothor.engine.models import AgentRun, RunStatus
from robothor.goals import store
from robothor.goals.controller import GoalController
from robothor.goals.model import CreateGoal, GoalUpdate
from robothor.goals.runtime import attach_run, binding, budget_hit, stop_at_budget
from robothor.goals.tests.test_store import db, private_database  # noqa: F401


@pytest.mark.asyncio
async def test_actual_controller_continues_then_verifies_completion(db):  # noqa: F811
    g = store.create(
        db, CreateGoal(objective="Deliver report", success_criteria=["Delivered"]), "operator"
    )
    prompts = []

    async def execute(**kwargs):
        prompts.append(kwargs["message"])
        run = AgentRun(
            id=str(uuid4()),
            agent_id="main",
            tenant_id=db,
            trigger_detail=kwargs["trigger_detail"],
            status=RunStatus.COMPLETED,
            input_tokens=5,
            output_tokens=3,
            output_text="Progress saved",
        )
        attach_run(run)
        current = store.control(db, g["id"])
        if len(prompts) == 1:
            store.update(
                db,
                g["id"],
                GoalUpdate(
                    action="progress",
                    version=current["version"],
                    note="Draft saved",
                    next_action="Deliver it",
                ),
                "main",
            )
        else:
            current = store.update(
                db,
                g["id"],
                GoalUpdate(
                    action="evidence",
                    version=current["version"],
                    criterion=0,
                    reference="receipt:report",
                    satisfied=True,
                    note="Checked receipt",
                ),
                "main",
            )
            store.update(
                db,
                g["id"],
                GoalUpdate(action="complete", version=current["version"], note="Delivered"),
                "main",
            )
        return run

    controller = GoalController(
        SimpleNamespace(execute=execute), SimpleNamespace(tenant_id=db, manifest_dir="unused")
    )
    with (
        patch("robothor.engine.config.load_agent_config_or_broken", return_value=SimpleNamespace()),
        patch("robothor.goals.events.capture"),
    ):
        await controller.tick()
    result = store.get(db, g["id"])
    assert result["status"] == "complete" and result["tokens_used"] == 16
    assert len(prompts) == 2 and "Draft saved" in prompts[1]
    assert binding.get() is None


@pytest.mark.asyncio
async def test_waiting_and_disabled_goals_do_not_call_runner(db):  # noqa: F811
    g = store.create(
        db,
        CreateGoal(
            objective="Wait for response", success_criteria=["Response received"], kind="long"
        ),
        "operator",
    )
    store.update(
        db, g["id"], GoalUpdate(action="wait", version=g["version"], note="Check tomorrow"), "main"
    )
    runner = SimpleNamespace(execute=AsyncMock())
    controller = GoalController(runner, SimpleNamespace(tenant_id=db, manifest_dir="unused"))
    with patch("robothor.goals.events.capture"):
        await controller.tick()
        store.set_enabled(db, False, "operator")
        await controller.tick()
    runner.execute.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_budget_stops_across_spawned_runs(db):  # noqa: F811
    g = store.create(
        db,
        CreateGoal(objective="Deliver report", success_criteria=["Delivered"], token_budget=10),
        "operator",
    )

    async def execute(**kwargs):
        run = AgentRun(
            agent_id="main",
            tenant_id=db,
            trigger_detail=kwargs["trigger_detail"],
            input_tokens=3,
            output_tokens=2,
            status=RunStatus.COMPLETED,
        )
        attach_run(run)
        child = AgentRun(agent_id="worker", tenant_id=db, input_tokens=4, output_tokens=2)
        attach_run(child)
        assert budget_hit()
        session = SimpleNamespace(run=run, record_error=lambda note: None)
        assert stop_at_budget(session)
        return run

    controller = GoalController(
        SimpleNamespace(execute=execute), SimpleNamespace(tenant_id=db, manifest_dir="unused")
    )
    with (
        patch("robothor.engine.config.load_agent_config_or_broken", return_value=SimpleNamespace()),
        patch("robothor.goals.events.capture"),
    ):
        await controller.tick()
    result = store.get(db, g["id"])
    assert result["status"] == "blocked" and result["tokens_used"] == 11


@pytest.mark.asyncio
async def test_missing_progress_stops_after_three_runs(db):  # noqa: F811
    g = store.create(
        db, CreateGoal(objective="Deliver report", success_criteria=["Delivered"]), "operator"
    )
    runner = SimpleNamespace(execute=AsyncMock(return_value=AgentRun(status=RunStatus.COMPLETED)))
    controller = GoalController(runner, SimpleNamespace(tenant_id=db, manifest_dir="unused"))
    with (
        patch("robothor.engine.config.load_agent_config_or_broken", return_value=SimpleNamespace()),
        patch("robothor.goals.events.capture"),
    ):
        await controller.tick()
    assert runner.execute.call_count == 3
    assert store.get(db, g["id"])["status"] == "blocked"
