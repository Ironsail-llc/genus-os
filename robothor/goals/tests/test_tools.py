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


@pytest.mark.asyncio
async def test_adapter_tools_obey_goal_admission(db):  # noqa: F811
    from unittest.mock import AsyncMock, patch

    from robothor.engine.tools.dispatch import _execute_tool

    g = store.create(
        db, CreateGoal(objective="Deliver report", success_criteria=["Delivered"]), "operator"
    )
    g, attempt = store.claim(db)
    store.update(db, g["id"], GoalUpdate(action="pause", version=g["version"]), "operator")
    session = SimpleNamespace(call_tool=AsyncMock(return_value={"ok": True}))
    pool = SimpleNamespace(get_session=AsyncMock(return_value=session))
    token = binding.set(Binding(db, g["id"], attempt, run_id="root"))
    try:
        with (
            patch("robothor.engine.permissions.check_tool_permission", return_value=None),
            patch("robothor.engine.tools.get_registry") as registry,
            patch("robothor.engine.mcp_client.get_mcp_client_pool", return_value=pool),
            patch("robothor.engine.tools.dispatch._audit_tool_call"),
        ):
            registry.return_value.get_adapter_route.return_value = "adapter"
            result = await _execute_tool("external_write", {}, tenant_id=db, run_id="root")
        assert "error" in result
        session.call_tool.assert_not_awaited()
    finally:
        binding.reset(token)


def test_recovery_allows_deferred_reads_but_not_writes(db):  # noqa: F811
    store.create(db, CreateGoal(objective="Report", success_criteria=["Delivered"]), "operator")
    g, attempt = store.claim(db)
    with store.transaction() as cur:
        g["recovery_required"] = True
        store.save(cur, db, g)
    token = binding.set(Binding(db, g["id"], attempt, run_id="root"))
    try:
        admit_tool("tool_call", {"name": "get_task", "arguments": {}}, ToolContext())
        with pytest.raises(ValueError, match="inspect previous"):
            admit_tool("tool_call", {"name": "create_task", "arguments": {}}, ToolContext())
    finally:
        binding.reset(token)


def test_pursuit_tools_advertised_with_deferred_discovery():
    from unittest.mock import patch

    from robothor.engine.models import AgentConfig
    from robothor.engine.tools.registry import ToolRegistry

    registry = ToolRegistry()
    with patch.object(registry, "should_defer", return_value=True):
        advertised = registry.build_for_agent(
            AgentConfig(id="main", name="Main", tools_allowed=["read_file"])
        )
    assert {schema["function"]["name"] for schema in advertised} >= TOOL_NAMES


@pytest.mark.asyncio
async def test_durable_wait_ends_coordinator_iterations(db):  # noqa: F811
    from robothor.engine.models import AgentRun
    from robothor.goals.runtime import stop_pursuit

    store.create(db, CreateGoal(objective="Report", success_criteria=["Delivered"]), "operator")
    g, attempt = store.claim(db)
    token = binding.set(Binding(db, g["id"], attempt, run_id="root"))
    try:
        result = await HANDLERS["update_pursuit_goal"](
            {"action": "wait", "version": g["version"], "note": "Await external response"},
            ToolContext(agent_id="main", run_id="root", tenant_id=db),
        )
        assert result["goal"]["status"] == "waiting"
        session = SimpleNamespace(run=AgentRun(id="root"), record_error=lambda note: None)
        assert stop_pursuit(session)
        assert not session.run.budget_exhausted
        from robothor.engine.models import AgentConfig
        from robothor.engine.verifier import should_verify

        assert not should_verify(
            AgentConfig(id="main", name="Main", verification_enabled=True),
            SimpleNamespace(verification=True),
            session,
        )
    finally:
        binding.reset(token)


def test_worker_cannot_override_its_paused_task_with_another_parent():
    from unittest.mock import patch

    session = SimpleNamespace(run=SimpleNamespace(task_id="paused-owned-task"))
    with (
        patch("robothor.engine.session_registry.lookup", return_value=session),
        patch(
            "robothor.goals.runtime.task_runnable",
            side_effect=lambda task, tenant: task != "paused-owned-task",
        ),
        pytest.raises(ValueError, match="inactive"),
    ):
        admit_tool(
            "create_task",
            {"parent_task_id": "unrelated-active-task"},
            ToolContext(run_id="worker", tenant_id="tenant"),
        )
