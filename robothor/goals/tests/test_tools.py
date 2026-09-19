from types import SimpleNamespace

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.goals import store
from robothor.goals.model import CreateGoal, GoalUpdate
from robothor.goals.runtime import Binding, admit_tool, binding
from robothor.goals.tests.test_store import db, private_database  # noqa: F401
from robothor.goals.tools import HANDLERS, TOOL_NAMES, schemas


def test_schemas_handlers_and_model_contract_agree():
    assert set(schemas()) == set(HANDLERS) == TOOL_NAMES
    assert "version" in schemas()["update_pursuit_goal"]["function"]["parameters"]["required"]


@pytest.mark.asyncio
async def test_unbound_implicit_goal_and_workers_are_refused():
    result = await HANDLERS["get_pursuit_goal"]({}, ToolContext(agent_id="main", user_role="owner"))
    assert "goal_id is required" in result["error"]
    result = await HANDLERS["list_pursuit_goals"]({}, ToolContext(agent_id="worker"))
    assert "main agent" in result["error"]
    result = await HANDLERS["list_pursuit_goals"](
        {}, ToolContext(agent_id="main", is_benchmark=True)
    )
    assert "benchmarks" in result["error"]


def test_recovery_refuses_external_writes_and_stopped_lease_refuses_all(db):  # noqa: F811
    g = store.create(
        db, CreateGoal(objective="Deliver report", success_criteria=["Delivered"]), "operator"
    )
    g, attempt = store.claim(db)
    token = binding.set(Binding(db, g["id"], attempt, run_id="root"))
    try:
        with store.transaction() as cur:
            g["recovery_required"] = True
            store.save(cur, db, g)
        with pytest.raises(ValueError, match="inspect previous"):
            admit_tool("exec", {}, SimpleNamespace())
        admit_tool("get_pursuit_goal", {}, SimpleNamespace())
        admit_tool("update_pursuit_goal", {"action": "reconciled"}, SimpleNamespace())
        store.update(db, g["id"], GoalUpdate(action="pause", version=g["version"]), "operator")
        with pytest.raises(ValueError, match="stopped"):
            admit_tool("exec", {}, SimpleNamespace())
    finally:
        binding.reset(token)


@pytest.mark.asyncio
async def test_delegated_context_cannot_mutate_goal():
    token = binding.set(Binding("t", "g", "a", run_id="parent"))
    try:
        result = await HANDLERS["get_pursuit_goal"](
            {}, ToolContext(agent_id="main", run_id="child")
        )
        assert "delegated run" in result["error"]
    finally:
        binding.reset(token)
