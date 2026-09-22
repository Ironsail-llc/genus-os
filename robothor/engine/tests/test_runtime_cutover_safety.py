"""Cutover regressions: uncertain non-CRM writes cannot be silently retried."""

from dataclasses import replace

import pytest

from robothor.engine.runtime import effects
from robothor.engine.tests.test_runtime_effects import (  # noqa: F401
    context,
    effect_db,
    private_database,
)


def test_rephrased_interactive_write_waits_for_reconciliation(effect_db):  # noqa: F811
    ctx = context()
    row = effects.begin(ctx, "worker", "main", "gws_gmail_send", {"body": "first wording"})
    effects.mark_dispatched(ctx, row["id"], "worker")
    effects.finish(ctx, row["id"], "worker", uncertain=True)
    with pytest.raises(effects.EffectPendingError):
        effects.begin(
            replace(ctx, request_id="rephrased"),
            "next-worker",
            "main",
            "gws_gmail_send",
            {"body": "same instruction rephrased"},
        )


def test_operator_attestation_releases_unknown_without_claiming_verification(effect_db):  # noqa: F811
    ctx = context()
    row = effects.begin(ctx, "worker", "main", "gws_gmail_send", {"body": "one"})
    effects.mark_dispatched(ctx, row["id"], "worker")
    effects.abandon_run(ctx, "worker")
    assert effects.attest(ctx, row["id"], actor="operator", note="Provider history reviewed")
    saved = effects.read(ctx, row["id"])
    assert saved["state"] == "finished"
    assert saved["resolution"]["verified"] is False
    assert saved["resolution"]["source"] == "reconciled"
    assert not effects.attest(ctx, row["id"], actor="operator", note="Repeated")
    assert effects.begin(replace(ctx, request_id="new"), "next", "main", "other", {})


def test_attestation_cannot_cross_principal_or_settle_active_dispatch(effect_db):  # noqa: F811
    ctx = context()
    row = effects.begin(ctx, "worker", "main", "gws_gmail_send", {})
    effects.mark_dispatched(ctx, row["id"], "worker")
    assert not effects.attest(ctx, row["id"], actor="operator", note="Still running")
    effects.abandon_run(ctx, "worker")
    assert not effects.attest(
        replace(ctx, principal_id="another"), row["id"], actor="operator", note="Wrong scope"
    )


def test_goal_reconciliation_unwedges_non_crm_write_without_false_verification(
    effect_db,  # noqa: F811
    monkeypatch,
):
    from robothor.goals import store
    from robothor.goals.model import CreateGoal, GoalUpdate
    from robothor.goals.tests.test_store import register_tenant

    monkeypatch.setattr(store, "get_connection", effect_db)
    ctx = context()
    register_tenant(ctx.tenant_id)
    goal = store.create(
        ctx.tenant_id,
        CreateGoal(objective="Prepare work", success_criteria=["Reviewed"]),
        "operator",
    )
    ctx = replace(ctx, goal_id=goal["id"], attempt_id="attempt", budget_id=goal["id"])
    row = effects.begin(ctx, "worker", "main", "gws_gmail_send", {})
    effects.mark_dispatched(ctx, row["id"], "worker")
    effects.abandon_run(ctx, "worker")
    with pytest.raises(ValueError, match="readback"):
        store.update(
            ctx.tenant_id,
            goal["id"],
            GoalUpdate(action="complete", version=goal["version"], note="Done"),
            "operator",
            operator=True,
        )
    with pytest.raises(ValueError, match="operator"):
        store.update(
            ctx.tenant_id,
            goal["id"],
            GoalUpdate(
                action="reconciled", version=goal["version"], note="Reviewed provider history"
            ),
            "agent",
        )
    goal = store.update(
        ctx.tenant_id,
        goal["id"],
        GoalUpdate(
            action="reconciled",
            version=goal["version"],
            note="Reviewed provider history; outcome remains unverified",
        ),
        "operator",
        operator=True,
    )
    assert effects.read(ctx, row["id"])["resolution"]["verified"] is False
    assert not goal["recovery_required"]
    assert effects.begin(replace(ctx, request_id="new"), "next", "main", "other", {})


def test_effect_connections_bind_request_tenant_and_restore_outer_scope(effect_db, monkeypatch):  # noqa: F811
    from contextlib import contextmanager

    from robothor.db.connection import current_tenant_scope, tenant_scope

    ctx = context()

    @contextmanager
    def checked_connection():
        assert current_tenant_scope() == ctx.tenant_id
        with effect_db() as connection:
            yield connection

    monkeypatch.setattr(effects, "get_connection", checked_connection)
    with tenant_scope("different-instance-tenant"):
        row = effects.begin(ctx, "worker", "main", "write", {})
        assert effects.mark_dispatched(ctx, row["id"], "worker")
        assert effects.finish(ctx, row["id"], "worker", uncertain=True)
        assert effects.read(ctx, row["id"])
        assert effects.attest(ctx, row["id"], actor="operator", note="Checked provider")
        assert current_tenant_scope() == "different-instance-tenant"
