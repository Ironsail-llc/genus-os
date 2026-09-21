"""Goal bookkeeping remains available without clearing a business-effect fence."""

from dataclasses import replace

import pytest

from robothor.engine.runtime import effects
from robothor.engine.runtime.current import active_context
from robothor.engine.tests.test_runtime_effects import (  # noqa: F401
    context,
    effect_db,
    private_database,
)
from robothor.engine.tools import dispatch
from robothor.goals import store
from robothor.goals.model import CreateGoal
from robothor.goals.runtime import Binding, binding
from robothor.goals.tools import HANDLERS


@pytest.mark.parametrize("recovering", [False, True])
@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["progress", "wait", "block", "pause", "cancel"])
async def test_bookkeeping_preserves_uncertainty_and_denies_more_business_writes(
    effect_db,
    monkeypatch,
    action,
    recovering,  # noqa: F811
):
    ctx = context()
    monkeypatch.setattr(store, "get_connection", effect_db)
    monkeypatch.setattr(dispatch, "_audit_tool_call", lambda *a, **k: None)
    store.set_enabled(ctx.tenant_id, True, ctx.principal_id)
    goal = store.create(
        ctx.tenant_id,
        CreateGoal(objective="Finish requested work", success_criteria=["Verified"]),
        ctx.principal_id,
    )
    goal, attempt = store.claim(ctx.tenant_id)
    ctx = replace(ctx, goal_id=goal["id"], attempt_id=attempt, budget_id=goal["id"])
    pending = effects.begin(ctx, "old-worker", "main", "create_note", {"body": "once"})
    effects.mark_dispatched(ctx, pending["id"], "old-worker")
    effects.finish(ctx, pending["id"], "old-worker", uncertain=True)
    with store.transaction() as cur:
        goal["recovery_required"] = recovering
        store.save(cur, ctx.tenant_id, goal)
    current = Binding(ctx.tenant_id, goal["id"], attempt, run_id="coordinator")
    runtime_token = active_context.set(ctx)
    goal_token = binding.set(current)
    writes = []

    async def write(args, tool_context):
        writes.append(args)
        return {"id": "should-never-dispatch"}

    monkeypatch.setattr(dispatch, "_get_handlers", lambda: {**HANDLERS, "create_note": write})

    async def call(name, args):
        return await dispatch._execute_tool(
            name,
            args,
            agent_id="main",
            run_id="coordinator",
            tenant_id=ctx.tenant_id,
            user_id=ctx.principal_id,
            user_role="owner",
        )

    try:
        result = await call(
            "update_pursuit_goal",
            {
                "goal_id": goal["id"],
                "version": goal["version"],
                "action": action,
                "note": "Earlier action awaits readback",
            },
        )
        assert "error" not in result, result
        updated = store.get(ctx.tenant_id, goal["id"])
        assert updated["recovery_required"] is recovering
        assert updated["status"] not in {"complete", "review"}
        assert effects.read(ctx, pending["id"])["state"] == "uncertain"
        denied = await call("create_note", {"body": "replacement"})
        assert "error" in denied and not writes
        completion = await call(
            "update_pursuit_goal",
            {
                "goal_id": goal["id"],
                "version": updated["version"],
                "action": "complete",
                "note": "Done",
            },
        )
        assert "error" in completion
        if action in {"wait", "block", "pause", "cancel"}:
            assert current.yield_requested
    finally:
        binding.reset(goal_token)
        active_context.reset(runtime_token)
