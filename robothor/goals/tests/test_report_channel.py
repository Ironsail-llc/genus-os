import asyncio
from types import SimpleNamespace

import pytest

from robothor.goals.report_channel import consume_report, publish_report, report_turn


def context(tenant="tenant", run="run"):
    return SimpleNamespace(tenant_id=tenant, agent_id="main", run_id=run, id=run)


def test_report_is_only_consumed_after_tool_turn_and_once():
    ctx = context()
    with report_turn(ctx.tenant_id, ctx.agent_id, ctx.run_id, enabled=True) as state:
        publish_report(ctx, "The goal is paused.")
        with pytest.raises(ValueError, match="tool turn must finish"):
            consume_report(state, ctx)
        with pytest.raises(ValueError, match="Only one"):
            publish_report(ctx, "A replacement report")
    assert consume_report(state, ctx) == "The goal is paused."
    assert consume_report(state, ctx) is None


@pytest.mark.parametrize("field", ["tenant_id", "agent_id", "run_id"])
def test_foreign_identity_cannot_publish(field):
    ctx = context()
    with report_turn(ctx.tenant_id, ctx.agent_id, ctx.run_id, enabled=True) as state:
        foreign = context()
        setattr(foreign, field, "foreign")
        with pytest.raises(ValueError, match="unavailable"):
            publish_report(foreign, "Forged report")
    assert consume_report(state, ctx) is None


def test_ordinary_tool_context_and_error_exit_do_not_finalize_reports():
    ctx = context()
    with pytest.raises(ValueError, match="unavailable"):
        publish_report(ctx, "No report capability")
    with report_turn(ctx.tenant_id, ctx.agent_id, ctx.run_id, enabled=False) as disabled:
        with pytest.raises(ValueError, match="unavailable"):
            publish_report(ctx, "Ordinary tool result")
    assert consume_report(disabled, ctx) is None
    with (
        pytest.raises(RuntimeError, match="interrupted turn"),
        report_turn(ctx.tenant_id, ctx.agent_id, ctx.run_id, enabled=True) as interrupted,
    ):
        publish_report(ctx, "Incomplete turn")
        raise RuntimeError("interrupted turn")
    assert consume_report(interrupted, ctx) is None


@pytest.mark.asyncio
async def test_stale_inherited_context_cannot_publish():
    ctx = context()
    release = asyncio.Event()

    async def late():
        await release.wait()
        with pytest.raises(ValueError, match="unavailable"):
            publish_report(ctx, "Late report")

    with report_turn(ctx.tenant_id, ctx.agent_id, ctx.run_id, enabled=True) as state:
        task = asyncio.create_task(late())
    release.set()
    await task
    assert consume_report(state, ctx) is None


@pytest.mark.asyncio
async def test_concurrent_reports_stay_with_their_tenants_and_runs():
    async def report(index):
        ctx = context(tenant=f"tenant-{index}", run=f"run-{index}")
        with report_turn(ctx.tenant_id, ctx.agent_id, ctx.run_id, enabled=True) as state:
            await asyncio.sleep(0)
            publish_report(ctx, f"Report {index}")
        with pytest.raises(ValueError, match="another run"):
            consume_report(state, context())
        return consume_report(state, ctx)

    assert await asyncio.gather(*(report(i) for i in range(20))) == [
        f"Report {i}" for i in range(20)
    ]
