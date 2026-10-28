"""The daemon recovers persisted terminal owners, never guessing from effect age."""

from dataclasses import replace
from uuid import uuid4

import pytest

from robothor.engine.runtime import effects
from robothor.engine.runtime.effect_recovery import sweep_terminal
from robothor.engine.tests.test_runtime_effects import (  # noqa: F401
    context,
    effect_db,
    private_database,
)


@pytest.fixture
def runs(effect_db):  # noqa: F811
    with effect_db() as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS agent_runs(id UUID PRIMARY KEY, tenant_id TEXT, status TEXT)"
        )

    def insert(ctx, status):
        run = str(uuid4())
        with effect_db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO agent_runs VALUES (%s,%s,%s)", (run, ctx.tenant_id, status))
        return run

    return insert


@pytest.mark.parametrize("status", ["completed", "failed", "timeout", "cancelled"])
def test_terminal_owner_recovers_both_dispatch_boundaries(effect_db, runs, status):  # noqa: F811
    ctx = context()
    run = runs(ctx, status)
    prepared = effects.begin(ctx, run, "main", "create_note", {"body": "prepared"})
    dispatched = effects.begin(ctx, run, "main", "create_note", {"body": "dispatched"})
    assert effects.mark_dispatched(ctx, dispatched["id"], run)
    assert sweep_terminal(ctx.tenant_id) == 2
    assert sweep_terminal(ctx.tenant_id) == 0
    assert effects.read(ctx, prepared["id"])["state"] == "not_applied"
    assert not effects.mark_dispatched(ctx, prepared["id"], run)
    assert effects.read(ctx, dispatched["id"])["state"] == "uncertain"
    assert not effects.finish(ctx, dispatched["id"], run, uncertain=False)
    with pytest.raises(effects.EffectPendingError):
        effects.begin(ctx, str(uuid4()), "main", "create_note", {"body": "dispatched"})


def test_live_missing_and_foreign_owners_remain_fenced(effect_db, runs):  # noqa: F811
    ctx = context()
    records = []
    for status in ["pending", "running", None]:
        run = runs(ctx, status) if status else str(uuid4())
        row = effects.begin(ctx, run, "main", "create_note", {"body": status})
        records.append(row)
    foreign = replace(ctx, tenant_id=str(uuid4()))
    run = runs(foreign, "failed")
    row = effects.begin(ctx, run, "main", "create_note", {"body": "foreign run"})
    records.append(row)
    with effect_db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_runtime_effects SET updated_at=now()-interval '30 days' WHERE tenant_id=%s",
            (ctx.tenant_id,),
        )
    assert sweep_terminal(ctx.tenant_id) == 0
    assert all(effects.read(ctx, row["id"])["state"] == "prepared" for row in records)


def test_reaper_invokes_effect_recovery_even_without_stale_runs(monkeypatch):
    from unittest.mock import MagicMock

    from robothor.engine import daemon

    connect = MagicMock()
    connect.return_value.__enter__.return_value.cursor.return_value.fetchall.return_value = []
    monkeypatch.setattr("robothor.db.connection.get_connection", connect)
    monkeypatch.setattr(daemon, "_cleanup_stale_workflow_runs", lambda tenant: 0)
    sweep = MagicMock(return_value=0)
    monkeypatch.setattr("robothor.engine.runtime.effect_recovery.sweep_terminal", sweep)
    assert daemon._cleanup_stale_runs("synthetic-tenant") == 0
    sweep.assert_called_once_with("synthetic-tenant")


def test_sweep_is_bounded_and_does_not_starve_later_records(effect_db, runs):  # noqa: F811
    ctx = context()
    run = runs(ctx, "failed")
    for index in range(101):
        effects.begin(ctx, run, "main", "create_note", {"body": index})
    assert sweep_terminal(ctx.tenant_id) == 100
    assert sweep_terminal(ctx.tenant_id) == 1
    assert sweep_terminal(ctx.tenant_id) == 0


def test_storage_failure_does_not_claim_resolution(monkeypatch, caplog):
    def unavailable():
        raise OSError("synthetic storage outage")

    monkeypatch.setattr(effects, "get_connection", unavailable)
    assert sweep_terminal("synthetic-tenant") == 0
    assert "Terminal effect recovery deferred" in caplog.text


def _uncertain(ctx, tool, args, run="worker"):
    row = effects.begin(ctx, run, "main", tool, args)
    assert effects.mark_dispatched(ctx, row["id"], run)
    assert effects.finish(ctx, row["id"], run, uncertain=True)
    return row


def _age(effect_db, row, interval):  # noqa: F811
    with effect_db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_runtime_effects SET updated_at=now()-%s::interval WHERE id=%s",
            (interval, row["id"]),
        )


def test_sweep_expires_day_old_unverifiable_uncertainty(effect_db, runs, monkeypatch):  # noqa: F811
    """Nothing can ever read back an ``exec`` or a send; after a day the sweep
    releases the row as expired (not as an operator decision) so no tool can
    wedge indefinitely. Requests ``runs`` because ``sweep_terminal`` joins
    ``agent_runs`` first; without it this passed only after another test in
    the module had created that table."""
    from robothor.engine.runtime import note_recovery, task_recovery

    monkeypatch.setattr(note_recovery, "sweep", lambda tenant: None)
    monkeypatch.setattr(task_recovery, "sweep", lambda tenant: None)
    ctx = context()
    stale = _uncertain(ctx, "exec", {"command": "stale"})
    send = _uncertain(replace(ctx, request_id="send"), "gws_gmail_send", {"body": "stale"})
    young = _uncertain(replace(ctx, request_id="young"), "exec", {"command": "young"})
    task = _uncertain(replace(ctx, request_id="task"), "create_task", {"title": "t"})
    goal_ctx = replace(ctx, request_id="goal", goal_id="g", attempt_id="a")
    goal = _uncertain(goal_ctx, "exec", {"command": "goal"})
    for row in (stale, send, task, goal):
        _age(effect_db, row, "25 hours")
    _age(effect_db, young, "23 hours")

    sweep_terminal(ctx.tenant_id)

    for row in (stale, send):
        settled = effects.read(ctx, row["id"])
        assert settled["state"] == "finished"
        assert settled["resolution"]["source"] == "expired"
        assert settled["resolution"]["actor"] == "system:effect-sweep"
        assert settled["resolution"]["verified"] is False
    assert effects.read(ctx, young["id"])["state"] == "uncertain"
    assert effects.read(ctx, task["id"])["state"] == "uncertain"
    assert effects.read(ctx, goal["id"])["state"] == "uncertain"
    retry = replace(ctx, request_id="retry")
    assert effects.begin(retry, "w", "main", "exec", {"command": "stale"})["state"] == "prepared"
    assert effects.begin(retry, "w", "main", "gws_gmail_send", {"body": "stale"})["state"] == (
        "prepared"
    )


def test_expire_is_not_an_operator_attestation(effect_db):  # noqa: F811
    ctx = context()
    row = _uncertain(ctx, "gws_gmail_send", {"body": "one"})
    assert effects.expire(ctx, row["id"], actor="system:effect-sweep", note="window lapsed")
    assert effects.read(ctx, row["id"])["resolution"]["source"] == "expired"
    assert not effects.expire(ctx, row["id"], actor="system:effect-sweep", note="again")
    goal_ctx = replace(ctx, request_id="goal", goal_id="g", attempt_id="a")
    goal = _uncertain(goal_ctx, "gws_gmail_send", {"body": "goal"})
    assert not effects.expire(ctx, goal["id"], actor="system:effect-sweep", note="goal-owned")
