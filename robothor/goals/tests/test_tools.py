from types import SimpleNamespace
from uuid import uuid4

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


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
@pytest.mark.parametrize(
    "ctx",
    [
        # `identity is None` is a cron, heartbeat or scheduled run — the runs
        # that read inbound email. The check used to be skipped entirely for
        # them, and a probe created a goal titled "injected goal from an
        # inbound email" and cancelled another goal from such a run.
        ToolContext(agent_id="main"),
        ToolContext(agent_id="main", user_role="", identity=SimpleNamespace()),
        ToolContext(agent_id="main", user_role="member", identity=SimpleNamespace()),
    ],
    ids=["unattended", "identified-no-role", "identified-member"],
)
async def test_every_chat_tool_requires_an_operator_role_positively(name, ctx):
    result = await HANDLERS[name]({}, ctx)
    assert "operator role" in result["error"], result


@pytest.mark.asyncio
async def test_a_goal_run_can_only_create_its_own_children(db):  # noqa: F811
    """The executor exemption is not a licence to fan out into siblings.

    `check_context` rightly skips the role check when an authorized binding is
    set — but `create_pursuit_goal` did not have to name a parent, so from
    inside one goal's run, with no identity and no role, five TOP-LEVEL goals
    could be created, each with its own fresh default cost ceiling. A goal run
    is exactly where untrusted content lands.
    """
    parent = store.create(
        db,
        CreateGoal(objective="Coordinate", success_criteria=["Done"], kind="long"),
        "operator",
    )
    claimed, attempt = store.claim(db)
    stranger = store.create(
        db,
        CreateGoal(objective="Unrelated", success_criteria=["Done"], kind="long"),
        "operator",
    )
    token = binding.set(Binding(db, claimed["id"], attempt, run_id="root"))
    ctx = ToolContext(agent_id="main", run_id="root", tenant_id=db)
    try:
        sibling = {"objective": "Sibling", "success_criteria": ["Done"]}
        result = await HANDLERS["create_pursuit_goal"](sibling, ctx)
        assert "execution child" in result["error"], result

        result = await HANDLERS["create_pursuit_goal"](
            {**sibling, "parent_goal_id": stranger["id"]}, ctx
        )
        assert "execution child" in result["error"], result

        child = await HANDLERS["create_pursuit_goal"](
            {**sibling, "parent_goal_id": parent["id"]}, ctx
        )
        assert child["goal"]["parent_goal_id"] == parent["id"]
    finally:
        binding.reset(token)


@pytest.mark.asyncio
async def test_a_goal_run_can_only_update_its_own_goal_and_children(db):  # noqa: F811
    """A bound run could rewrite any OTHER goal in the tenant.

    `update_goal` took `goal_id` from the arguments and only lease-checked it
    when it happened to equal the bound goal; `check_context` waved the call
    through because a binding was set. With no identity and no role, a probe
    applied progress, evidence, block, wait, pause, cancel, link_task and
    unlink_task to a separate operator goal, then fabricated evidence and
    COMPLETED it. `complete` is the signal the operator reads to know work is
    finished, `evidence` is what backs it, and a goal run is where untrusted
    content lands.
    """
    parent = store.create(
        db,
        CreateGoal(objective="Coordinate", success_criteria=["Done"], kind="long"),
        "operator",
    )
    child = store.create(
        db,
        CreateGoal(objective="Execute", success_criteria=["Done"], parent_goal_id=parent["id"]),
        "operator",
    )
    victim = store.create(
        db, CreateGoal(objective="The operator's other goal", success_criteria=["Done"]), "operator"
    )
    claimed, attempt = store.claim(db)
    assert claimed["id"] == parent["id"]
    token = binding.set(Binding(db, parent["id"], attempt, run_id="root"))
    ctx = ToolContext(agent_id="main", run_id="root", tenant_id=db)
    try:
        for action, extra in (
            ("progress", {"note": "Injected", "next_action": "Do as I say"}),
            (
                "evidence",
                {"criterion": 0, "reference": "fake:1", "satisfied": True, "note": "Fabricated"},
            ),
            ("block", {"note": "Injected blocker"}),
            ("wait", {"note": "Injected wait"}),
            ("pause", {}),
            ("cancel", {}),
            ("link_task", {"task_id": str(uuid4())}),
            ("unlink_task", {"task_id": str(uuid4())}),
            ("complete", {"note": "Declaring someone else's work done"}),
        ):
            before = store.control(db, victim["id"])
            result = await HANDLERS["update_pursuit_goal"](
                {"goal_id": victim["id"], "action": action, "version": before["version"], **extra},
                ctx,
            )
            assert "own goal" in result.get("error", ""), (action, result)
            after = store.control(db, victim["id"])
            assert after["version"] == before["version"], action
            assert after["status"] == "queued", action

        own = store.control(db, parent["id"])
        mine = await HANDLERS["update_pursuit_goal"](
            {"action": "progress", "version": own["version"], "note": "Real", "next_action": "Go"},
            ctx,
        )
        assert mine["goal"]["checkpoint"] == "Real"

        offspring = store.control(db, child["id"])
        theirs = await HANDLERS["update_pursuit_goal"](
            {"goal_id": child["id"], "action": "pause", "version": offspring["version"]}, ctx
        )
        assert theirs["goal"]["status"] == "paused"
    finally:
        binding.reset(token)


def test_stale_attempt_and_disabled_tenant_each_refuse_a_write_tool(db):  # noqa: F811
    """`goal execution is no longer authorized` had no test of its own.

    The test named for it —
    ``test_recovery_refuses_external_writes_and_stopped_lease_refuses_all`` —
    exercises the ``status in {paused, canceled, blocked}`` branch on the NEXT
    line, so deleting this refusal left the whole suite green. Both halves
    below keep the goal's own status at ``running``, so nothing but this line
    can be what refuses.
    """
    store.create(
        db, CreateGoal(objective="Deliver report", success_criteria=["Delivered"]), "operator"
    )
    g, attempt = store.claim(db)

    stale = binding.set(Binding(db, g["id"], str(uuid4()), run_id="root"))
    try:
        with pytest.raises(ValueError, match="no longer authorized"):
            admit_tool("exec", {}, SimpleNamespace())
    finally:
        binding.reset(stale)

    live = binding.set(Binding(db, g["id"], attempt, run_id="root"))
    try:
        admit_tool("exec", {}, SimpleNamespace())  # the lease holder is admitted
        store.set_enabled(db, False, "operator")  # ...until the tenant switch goes off
        with pytest.raises(ValueError, match="no longer authorized"):
            admit_tool("exec", {}, SimpleNamespace())
        assert store.control(db, g["id"])["status"] == "running"
    finally:
        binding.reset(live)
        store.set_enabled(db, True, "operator")


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
        # This tenant has links, so the cheap skip in admit_tool does not apply
        # and the real per-task predicate runs.
        patch("robothor.goals.compat.tenant_links_tasks", return_value=True),
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
