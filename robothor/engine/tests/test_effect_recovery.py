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
