"""Goal reports and completion use the same durable family uncertainty."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from robothor.engine.runtime import effects
from robothor.engine.tests.test_runtime_effects import (  # noqa: F401
    begin,
    context,
    effect_db,
    private_database,
)
from robothor.goals import store
from robothor.goals.model import CreateGoal, GoalUpdate
from robothor.goals.presentation import render_goal_progress


@pytest.fixture
def family(effect_db, monkeypatch):  # noqa: F811
    ctx = context()
    monkeypatch.setattr(store, "get_connection", effect_db)
    from robothor.goals.tests.test_store import register_tenant

    register_tenant(ctx.tenant_id)
    store.set_enabled(ctx.tenant_id, True, ctx.principal_id)
    parent = store.create(
        ctx.tenant_id,
        CreateGoal(objective="Deliver report", success_criteria=["Verified"], kind="long"),
        ctx.principal_id,
    )
    child = store.create(
        ctx.tenant_id,
        CreateGoal(
            objective="Check delivery", success_criteria=["Checked"], parent_goal_id=parent["id"]
        ),
        ctx.principal_id,
    )
    return ctx, parent, child


def test_report_reads_child_uncertainty_and_later_verified_receipt(family):
    ctx, parent, child = family
    ctx = replace(ctx, goal_id=child["id"], attempt_id="synthetic-attempt")
    row = begin(ctx)
    effects.finish(ctx, row["id"], "worker", uncertain=True)
    snapshot = store.get(ctx.tenant_id, parent["id"])
    assert snapshot["action_evidence"] == {"pending": 1, "confirmed": 0}
    listed = {g["id"]: g for g in store.list_goals(ctx.tenant_id)}
    assert listed[parent["id"]]["action_evidence"] == snapshot["action_evidence"]
    assert listed[child["id"]]["action_evidence"] == snapshot["action_evidence"]
    text = render_goal_progress(snapshot, execution_enabled=True)
    assert "still need verification" in text and "The goal is not complete" in text
    assert effects.resolve(
        ctx,
        row["id"],
        lambda _: effects.Verification("applied", True, "synthetic", {"id": "checked"}),
    )
    refreshed = store.get(ctx.tenant_id, parent["id"])
    assert refreshed["action_evidence"] == {"pending": 0, "confirmed": 1}
    assert refreshed["status"] == parent["status"]
    listed = {g["id"]: g for g in store.list_goals(ctx.tenant_id)}
    assert listed[parent["id"]]["action_evidence"] == refreshed["action_evidence"]
    assert listed[parent["id"]]["version"] == parent["version"]
    assert "still need verification" not in render_goal_progress(refreshed, execution_enabled=True)


@pytest.mark.parametrize("action", ["complete", "approve", "assess"])
def test_goal_decisions_cannot_hide_unresolved_child_effects(family, action):
    ctx, parent, child = family
    row = begin(replace(ctx, goal_id=child["id"], attempt_id="synthetic-attempt"))
    effects.finish(ctx, row["id"], "worker", uncertain=True)
    with pytest.raises(
        ValueError,
        match=(
            "outstanding execution children"
            if action == "complete"
            else "actions still require audit readback"
        ),
    ):
        store.update(
            ctx.tenant_id,
            parent["id"],
            GoalUpdate(action=action, version=parent["version"], note="Cannot ignore the action"),
            ctx.principal_id,
            operator=True,
        )
    assert store.get(ctx.tenant_id, parent["id"])["version"] == parent["version"]


def test_foreign_tenant_or_unrelated_goal_does_not_pollute_report(family):
    ctx, parent, child = family
    begin(replace(context(), goal_id=child["id"], attempt_id="synthetic-attempt"))
    other = store.create(
        ctx.tenant_id,
        CreateGoal(objective="Unrelated", success_criteria=["Checked"]),
        ctx.principal_id,
    )
    begin(replace(ctx, goal_id=other["id"], attempt_id="synthetic-attempt"))
    assert store.get(ctx.tenant_id, parent["id"])["action_evidence"]["pending"] == 0


def test_admission_waits_for_goal_decision_transaction(family):
    ctx, parent, child = family
    entered = Event()

    def admit():
        entered.set()
        return begin(replace(ctx, goal_id=child["id"], attempt_id="synthetic-attempt"))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.transaction() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("goals:" + ctx.tenant_id,))
            future = pool.submit(admit)
            assert entered.wait(2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
        assert future.result(timeout=2)["state"] == "dispatching"
